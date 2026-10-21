# -*- coding: utf-8 -*-
"""Ember 多实例注册表（2026-10-17）：一颗心一套记忆库。

与 heart 的多实例结构对齐（scripts/run_server.py：default 实例数据住
data/ 根目录，其余实例住 data/instances/<名字>/）：每个实例一份
EmberStore + EmberService，记忆库目录挂在对应 heart 实例的数据目录下——

    <root>/ember                     # default 实例
    <root>/instances/<名字>/ember    # 其余实例

root 由 EMBER_DATA_ROOT 指定（heart 的 data/ 根目录）；未设置时回落
EMBER_DATA_DIR（旧变量）；再回落 <项目>/data。
旧扁平库兼容：default 实例在 <root>/ember 不存在而 <root>/events.jsonl
存在时，直接把 <root> 本身当记忆库用——已跑起来的库零迁移。

实例清单来自 EMBER_INSTANCES（逗号分隔，缺省 "default"）。清单外的
实例名一律拒绝——与 heart 的未知实例 404 同款：杜绝 typo 静默建出
空库、记忆无声写丢。

不做：实例增删的生命周期管理（heart 同款，暂由环境变量静态声明）；
跨实例查询/聚合——实例之间结构性隔离，不共享任何文件。
"""
from __future__ import annotations

import os
import threading

from .llm import MockLLMClient
from .service import EmberService
from .store import EmberStore
from .generations import GenerationStore

LEGACY_SENTINEL = "events.jsonl"  # 旧扁平库的标志文件（store.EVENTS_FILE）


def resolve_root() -> str:
    """记忆库根目录：EMBER_DATA_ROOT > EMBER_DATA_DIR > <项目>/data。"""

    root = (os.environ.get("EMBER_DATA_ROOT", "").strip()
            or os.environ.get("EMBER_DATA_DIR", "").strip())
    if root:
        return root
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "data")


def parse_instances(raw: str) -> list[str]:
    """实例清单：逗号分隔 → 去空去重；非法名当场拒绝（宁拒启不裸奔）。"""

    names: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if not name or name in names:
            continue
        if name in (".", "..") or "/" in name or "\\" in name:
            raise ValueError(f"非法实例名：{name!r}（不能含路径分隔符）")
        names.append(name)
    return names or ["default"]


def ember_dir(root: str, name: str) -> str:
    """实例 name 的记忆库目录（挂在该实例的 heart 数据目录下）。

    default 例外：root 下已是旧扁平库（有 events.jsonl 且无 ember/
    子目录）时原样沿用——已部署的库零迁移。
    """

    if name == "default":
        nested = os.path.join(root, "ember")
        legacy = os.path.join(root, LEGACY_SENTINEL)
        if os.path.exists(nested) or not os.path.exists(legacy):
            return nested
        return root
    return os.path.join(root, "instances", name, "ember")


class InstanceRegistry:
    """{实例名: EmberService} 惰性注册表。

    每实例独立的 EmberStore——事件/向量/账本/睡眠状态等全部派生文件
    都以 store.root 为根，实例之间不共享任何可变状态（隔离是结构性
    的，不靠约定）。service 惰性构造：未被调用的实例零开销。
    """

    def __init__(self, root: str, names: list[str]) -> None:
        self.root = root
        self.names = list(names)
        self._dirs = {n: ember_dir(root, n) for n in names}
        self._services: dict[str, EmberService] = {}
        self._lock = threading.Lock()

    def has(self, name: str) -> bool:
        return name in self._dirs

    def dir_of(self, name: str) -> str:
        return self._dirs[name]

    def service(self, name: str) -> EmberService | None:
        """取实例的 service；清单外的名字返回 None（由调用方 404）。"""

        if not self.has(name):
            return None
        with self._lock:
            svc = self._services.get(name)
            if svc is None:
                svc = EmberService(
                    EmberStore(self._dirs[name]),
                    MockLLMClient(),  # 旁路形态不调 LLM，占位即可
                    model="sidecar",
                )
                svc.source_instance = name
                self._services[name] = svc
            return svc

    def expire_recall_snapshots(self) -> int:
        """Clean known databases without loading services or recovering writers."""
        with self._lock:
            roots = list(self._dirs.values())
        return sum(GenerationStore(root, recover=False).expire_snapshots() for root in roots)
