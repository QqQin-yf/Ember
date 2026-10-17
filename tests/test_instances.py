# -*- coding: utf-8 -*-
"""多实例注册表（ember/instances.py，2026-10-17）— 验收测试。

不启 HTTP、不依赖 mcp SDK：直接测目录映射、实例清单解析与注册表
隔离语义（一颗心一套记忆库，实例之间不共享任何可变状态）。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ember.instances import (InstanceRegistry, ember_dir, parse_instances,
                             resolve_root)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def main():
    print("== I1 实例清单解析 ==")
    check("缺省 default", parse_instances("") == ["default"])
    check("逗号分隔+去空+去重",
          parse_instances(" alpha ,beta,,alpha ") == ["alpha", "beta"])
    try:
        parse_instances("a/../b")
        bad = False
    except ValueError:
        bad = True
    check("路径分隔符当场拒绝", bad)

    print("== I2 根目录解析优先级 ==")
    saved = {k: os.environ.get(k)
             for k in ("EMBER_DATA_ROOT", "EMBER_DATA_DIR")}
    tmp_env = tempfile.mkdtemp(prefix="ember_inst_env_")
    try:
        os.environ["EMBER_DATA_ROOT"] = tmp_env
        os.environ.pop("EMBER_DATA_DIR", None)
        check("EMBER_DATA_ROOT 优先", resolve_root() == tmp_env)
        os.environ.pop("EMBER_DATA_ROOT")
        os.environ["EMBER_DATA_DIR"] = tmp_env
        check("回落 EMBER_DATA_DIR", resolve_root() == tmp_env)
        os.environ.pop("EMBER_DATA_DIR")
        check("再回落 <项目>/data",
              os.path.basename(resolve_root()) == "data")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print("== I3 目录映射（记忆库挂到实例数据目录下） ==")
    root = tempfile.mkdtemp(prefix="ember_inst_root_")
    check("default → <root>/ember",
          ember_dir(root, "default") == os.path.join(root, "ember"))
    check("其余 → <root>/instances/<名>/ember",
          ember_dir(root, "second")
          == os.path.join(root, "instances", "second", "ember"))
    legacy = tempfile.mkdtemp(prefix="ember_inst_legacy_")
    with open(os.path.join(legacy, "events.jsonl"), "w", encoding="utf-8"):
        pass
    check("旧扁平库 default 零迁移（原样沿用）",
          ember_dir(legacy, "default") == legacy)
    check("旧扁平库其余实例仍分目录",
          ember_dir(legacy, "second")
          == os.path.join(legacy, "instances", "second", "ember"))

    print("== I4 注册表：清单外 404 语义 + 实例隔离 ==")
    try:
        reg = InstanceRegistry(root, ["default", "second"])
        check("清单外返回 None（unknown instance → 404）",
              reg.service("ghost") is None and not reg.has("ghost"))
        da = reg.service("default")
        db = reg.service("second")
        check("同实例复用同一 service", reg.service("default") is da)
        # live turn 隔离：A 开着轮，B 收轮如实拒绝（各实例各槽）
        ra = da.hook_turn_start("s1", "今天我们聊了心实例的隔离。")
        check("A 开轮成功", ra["turn"] == 1, str(ra)[:80])
        eb = db.hook_turn_end("s1", "B 的收轮")
        check("B 无进行中轮 → 如实拒绝",
              eb["ok"] is False and "no live turn" in eb.get("reason", ""),
              str(eb))
        ea = da.hook_turn_end("s1", "A 的收轮")
        check("A 自己收轮成功", ea["ok"] is True, str(ea))
        # 文件隔离：A 的账本落 A 的目录，B 的目录零文件
        files_a = os.listdir(reg.dir_of("default"))
        files_b = os.listdir(reg.dir_of("second"))
        check("A 的账本落在 A 自己的目录", len(files_a) > 0, str(files_a))
        check("B 的目录零文件（结构性零共享）", len(files_b) == 0,
              str(files_b))
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(legacy, ignore_errors=True)
        shutil.rmtree(tmp_env, ignore_errors=True)

    print(f"\n==== instances 验收：PASS={PASS} FAIL={FAIL} ====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
