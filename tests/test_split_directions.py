# -*- coding: utf-8 -*-
"""split_directions 核心函数测试（原 test_closing_split_pipeline 的 split 段）。

背景：closing_write.py / pipeline.py 已随"Sleep 运行模式重构"删除
（写入=AI 调工具实时写），本文件原为三脚本合测，closing/pipeline 两段
随之失效；split 段的校验+落盘测试仍有效，保留于此。
locate_turn / spans_from_quotes 的覆盖在 tests/test_sleep_run.py。
"""
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import split_directions as sd  # noqa: E402
from ember.entity import Entity, EntityStore  # noqa: E402


def test_split():
    with tempfile.TemporaryDirectory() as root:
        sd.DATA = root
        sd.EVENTS_FILE = os.path.join(root, "events.jsonl")
        # 8 条事件属 ent_x
        events = {}
        for i in range(1, 9):
            eid = f"EVT-{i:03d}-aaaa"
            events[eid] = {"event_id": eid, "summary": f"架构讨论{i}",
                           "entity_refs": ["ent_x"]}
        with open(os.path.join(root, "events.jsonl"), "w", encoding="utf-8") as f:
            for d in events.values():
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        estore = EntityStore(root)
        estore.upsert(Entity(entity_id="ent_x", name="X记忆系统"))
        estore.save()

        verdict = {"directions": [
            {"description": "它的架构与机制如何设计、为什么这样设计"},
            {"description": "它如何认识自己"},
        ], "reason": "两条主线"}
        assert not sd.validate(verdict)
        assert sd.validate({"directions": [], "reason": "x"})
        assert sd.validate({"directions": [{"description": ""}], "reason": "x"})

        # 模拟 main 的落盘段
        entity = estore.get("ent_x")
        for i, d in enumerate(verdict["directions"]):
            entity.directions.append(
                sd.Direction(direction_id=f"dir_x_{i+1}",
                             description=d["description"]))
        estore.save()
        estore2 = EntityStore(root)
        assert len(estore2.get("ent_x").directions) == 2
        assert estore2.get("ent_x").directions[0].direction_id == "dir_x_1"
        print("ok  split 校验+落盘（dir id 生成/持久化）")


def main():
    test_split()
    print("\nsplit_directions 核心测试全部通过")


if __name__ == "__main__":
    main()
