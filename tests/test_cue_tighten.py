# -*- coding: utf-8 -*-
"""A5 旧 Cue 收紧测试。

覆盖四个分支：
1. Entity open → 活跃豁免，不收
2. closed 但静置不足一周 → 不收
3. closed 且静置满一周、Cue 0 次使用 → retire
4. closed 且静置满一周、Cue 有使用 → 保留
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ember.entity import Entity, EntityStore
from ember.models import Event, RecallRoute
from ember.recall_ledger import RecallLedger
from ember.store import EmberStore
from ember.cue_tighten import CueTightener


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def make_store(root):
    # EntityStore + EmberStore 共享 root；Event 走 side-table entity_refs
    estore = EntityStore(root)
    store = EmberStore(root)
    now = datetime.now(timezone.utc).astimezone()
    week_ago = _iso(now - timedelta(days=8))

    ent = Entity(entity_id="ent_ember", name="Ember")
    estore.upsert(ent)
    estore.save()

    ev = Event(
        event_id="EVT-100", version_id="v1",
        source_ref="test", raw_ref="test", event_time=week_ago,
        ingest_time=week_ago)
    store.write_event(ev)
    # 侧表 entity_refs
    ev_path = os.path.join(root, "events.jsonl")
    with open(ev_path, "r+", encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    lines[0]["entity_refs"] = ["ent_ember"]
    with open(ev_path, "w", encoding="utf-8") as f:
        for d in lines:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")

    # 三条 cue：unused（0 次）、used（3 次）
    r1 = RecallRoute(route_id="RT-1", event_id="EVT-100", matcher="lexical",
                     cue_type="lexical", cue="堂姐结婚")
    r2 = RecallRoute(route_id="RT-2", event_id="EVT-100", matcher="lexical",
                     cue_type="lexical", cue="Ember")
    store.write_route(r1)
    store.write_route(r2)
    return estore, store


def main():
    with tempfile.TemporaryDirectory() as root:
        now = datetime.now(timezone.utc).astimezone()
        estore, store = make_store(root)
        ledger = RecallLedger(root)

        # 记账：Ember cue 用了 3 次，堂姐结婚 0 次
        for i in range(3):
            ledger.record("s1", i, route="cue", event_ids=["EVT-100"],
                          cue="Ember")

        t = CueTightener(store, ledger,
                         os.path.join(root, "events.jsonl"),
                         entity_store=estore)

        # 分支 1：open 豁免
        r = t.tighten(now=now)
        assert r["skipped"] and r["skipped"][0]["reason"] == "open", r
        assert not r["retired"]
        print("分支1 open 豁免 ok")

        # 分支 2：closed 但静置 1 天
        estore.close("ent_ember", _iso(now - timedelta(days=1)))
        r = t.tighten(now=now)
        assert any("静置" in s["reason"] for s in r["skipped"]), r
        assert not r["retired"]
        print("分支2 静置不足 ok")

        # 分支 3+4：静置 8 天 → unused retire，used 保留
        estore.reopen("ent_ember")
        estore.close("ent_ember", _iso(now - timedelta(days=8)))
        r = t.tighten(now=now)
        assert len(r["retired"]) == 1 and r["retired"][0]["cue"] == "堂姐结婚", r
        assert r["kept"] and r["kept"][0]["cue"] == "Ember", r
        # 落盘验证：status=retired、索引剔除、重载仍生效
        assert store.get_route("RT-1").status == "retired"
        assert store.get_route("RT-2").status == "active"
        assert store.index_snapshot().get("ember") == ["RT-2"]
        store2 = EmberStore(root)
        assert store2.get_route("RT-1").status == "retired"
        print("分支3/4 超期收紧 ok")

        print("A5 全部通过")


if __name__ == "__main__":
    main()
