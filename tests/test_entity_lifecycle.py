# -*- coding: utf-8 -*-
"""Entity open/close 生命周期测试（架构文档 §2.1，2026-10-08 定稿）。

覆盖：默认 open、close/reopen 往返 + 落盘、状态校验、closed_at 起算。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ember.entity import Entity, Direction, EntityStore


def test_lifecycle():
    with tempfile.TemporaryDirectory() as root:
        st = EntityStore(root)
        st.upsert(Entity(entity_id="ent_1", name="测试",
                         directions=[Direction(direction_id="d1", description="x")]))
        st.save()
        # 默认 open，无 closed_at
        e = EntityStore(root).get("ent_1")
        assert e.status == "open" and e.closed_at == ""
        # close → 落盘 → 重载仍 closed
        st.close("ent_1", "2026-10-08T21:00:00+08:00")
        e2 = EntityStore(root).get("ent_1")
        assert e2.status == "closed"
        assert e2.closed_at == "2026-10-08T21:00:00+08:00"
        # reopen → 起算点清空
        st.reopen("ent_1")
        e3 = EntityStore(root).get("ent_1")
        assert e3.status == "open" and e3.closed_at == ""
        # 重复 close 拒绝
        st.close("ent_1", "2026-10-08T22:00:00+08:00")
        try:
            st.close("ent_1", "2026-10-08T23:00:00+08:00")
            assert False, "重复 close 应拒绝"
        except ValueError:
            pass
        print("ok  test_lifecycle")


def test_validation():
    # 非法 status 拒绝
    try:
        Entity(entity_id="x", name="x", status="zombie")
        assert False
    except ValueError:
        pass
    # open 但带 closed_at 拒绝
    try:
        Entity(entity_id="x", name="x", status="open",
               closed_at="2026-10-08T21:00:00+08:00")
        assert False
    except ValueError:
        pass
    # 旧数据无 status 字段（v1 行）→ 默认 open
    e = Entity.from_dict({"entity_id": "old", "name": "旧实体"})
    assert e.status == "open" and e.closed_at == ""
    print("ok  test_validation")


if __name__ == "__main__":
    test_lifecycle()
    test_validation()
    print("entity lifecycle tests: all passed")
