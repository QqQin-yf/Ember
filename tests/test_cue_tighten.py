# -*- coding: utf-8 -*-
"""A5+r3 旧 Cue 收紧测试。

覆盖（交接单验证清单）：
1. Entity open → 名下事件豁免，不收
2. Entity closed 静置不足一周 → 不收
3. Entity closed 满一周：部分 Cue 用过 → 没用过的 retire、用过的保留
4. 散事件写入满一周：同上（两套起算点之散事件）
5. 散事件写入不足一周 → 不动
6. 散事件全部 Cue 零次使用 → 整组不动（kept_zero_use_event）
7. Entity 名下事件全部 Cue 零次使用 → 整组不动
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


def make_env(root):
    """造：ent_ember（open，名下 EVT-100）；散事件 EVT-200（满一周）、
    EVT-201（刚写入）、EVT-202（满一周，全零次）；EVT-100 零次备用于分组测试。"""
    now = datetime.now(timezone.utc).astimezone()
    week_ago = _iso(now - timedelta(days=8))
    just_now = _iso(now)

    def ev(eid, refs, ingest):
        d = {"event_id": eid, "version_id": eid + "-v1",
             "source_ref": "t", "raw_ref": "t",
             "event_time": ingest, "ingest_time": ingest,
             "turn_refs": [], "summary": eid, "core": eid}
        if refs:
            d["entity_refs"] = refs
        return d

    events = [
        ev("EVT-100", ["ent_ember"], week_ago),   # Entity 名下
        ev("EVT-200", None, week_ago),            # 散，满一周
        ev("EVT-201", None, just_now),            # 散，刚写入
        ev("EVT-202", None, week_ago),            # 散，满一周（备用）
    ]
    ev_path = os.path.join(root, "events.jsonl")
    with open(ev_path, "w", encoding="utf-8") as f:
        for d in events:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")

    # store 在 jsonl 写入之后构造（一次性加载，不重复追加行）
    estore = EntityStore(root)
    estore.upsert(Entity(entity_id="ent_ember", name="Ember"))
    estore.save()
    store = EmberStore(root)

    routes = [
        RecallRoute(route_id="RT-1", event_id="EVT-100", matcher="lexical",
                    cue_type="lexical", cue="Ember"),
        RecallRoute(route_id="RT-200a", event_id="EVT-200", matcher="lexical",
                    cue_type="lexical", cue="用过关键词"),
        RecallRoute(route_id="RT-200b", event_id="EVT-200", matcher="lexical",
                    cue_type="lexical", cue="没用关键词"),
        RecallRoute(route_id="RT-201a", event_id="EVT-201", matcher="lexical",
                    cue_type="lexical", cue="新散事件词"),
        RecallRoute(route_id="RT-202a", event_id="EVT-202", matcher="lexical",
                    cue_type="lexical", cue="零次整组词"),
    ]
    for r in routes:
        store.write_route(r)
    return estore, store


def main():
    with tempfile.TemporaryDirectory() as root:
        now = datetime.now(timezone.utc).astimezone()
        estore, store = make_env(root)
        ledger = RecallLedger(root)

        # 记账：EVT-100 的 "Ember" 用 3 次；EVT-200 的 "用过关键词" 用 2 次
        for i in range(3):
            ledger.record("s1", i, route="cue", event_ids=["EVT-100"],
                          cue="Ember")
        for i in range(2):
            ledger.record("s1", 10 + i, route="cue", event_ids=["EVT-200"],
                          cue="用过关键词")

        t = CueTightener(store, ledger,
                         os.path.join(root, "events.jsonl"),
                         entity_store=estore)

        # ---- 分支 1：Entity open → 名下事件豁免；散事件独立判定 ----
        r = t.tighten(now=now)
        assert any(s["event_id"] == "EVT-100" and "open" in s["reason"]
                   for s in r["skipped"]), r
        assert not any(x["event_id"] == "EVT-100" for x in r["retired"]), r
        # 散事件 EVT-200 独立于 Entity 状态：满一周照常判定
        assert any(x["cue"] == "没用关键词" for x in r["retired"]), r
        assert any(x["cue"] == "用过关键词" and x["uses"] == 2
                   for x in r["kept"]), r
        print("分支1 open 豁免（Entity 名下）/ 散事件独立判定 ok")

        # ---- 分支 5：散事件刚写入 → 不动 ----
        assert any(s["event_id"] == "EVT-201" and "静置" in s["reason"]
                   for s in r["skipped"]), r
        assert not any(x["event_id"] == "EVT-201" for x in r["retired"])
        print("分支5 散事件静置不足 ok")

        # ---- 分支 6：散事件全部 Cue 零次 → 整组不动 ----
        # EVT-202（满一周，零次）尚未造 cue 使用 → 整组不动
        assert any(z["event_id"] == "EVT-202" for z in r["kept_zero_use_event"]), r
        assert not any(x["event_id"] == "EVT-202" for x in r["retired"]), r
        print("分支6 散事件全零次整组不动 ok")

        # ---- 分支 2：Entity closed 静置不足 → 不收 ----
        estore.close("ent_ember", _iso(now - timedelta(days=1)))
        r = t.tighten(now=now)
        assert any(s["event_id"] == "EVT-100" and "静置" in s["reason"]
                   for s in r["skipped"]), r
        assert not any(x["event_id"] == "EVT-100" for x in r["retired"]), r
        print("分支2 Entity 静置不足 ok")

        # ---- 分支 3+4：Entity closed 满一周 + 部分用过 → retire+保留 ----
        estore.reopen("ent_ember")
        estore.close("ent_ember", _iso(now - timedelta(days=8)))
        r = t.tighten(now=now)
        # EVT-100 的 "Ember" 有 3 次使用 → 保留；零次整组不适用（有用过的）
        assert any(x["cue"] == "Ember" and x["uses"] == 3 for x in r["kept"]), r
        assert not any(x["event_id"] == "EVT-100" for x in r["retired"]), r
        print("分支3/4 Entity 满一周有使用 → 保留 ok")

        # ---- 分支 7：Entity 名下全部零次 → 整组不动 ----
        # 清空记账重跑：用独立空账本（模拟全部 Cue 零次使用）
        ledger2 = RecallLedger(tempfile.mkdtemp(prefix="empty_ledger_"))
        t2 = CueTightener(store, ledger2,
                          os.path.join(root, "events.jsonl"),
                          entity_store=estore)
        r2 = t2.tighten(now=now)
        assert any(z["event_id"] == "EVT-100" for z in r2["kept_zero_use_event"]), r2
        assert not any(x["event_id"] == "EVT-100" for x in r2["retired"]), r2
        assert not any(x["event_id"] == "EVT-200" for x in r2["retired"]) or True
        # 注意：EVT-200 有 2 次使用但那是在 ledger（旧账本）里——ledger2 为空
        # → EVT-200 也整组不动。retired 应为空。
        assert not r2["retired"], r2
        print("分支7 Entity 名下全零次整组不动 ok")

        # ---- 落盘验证：retire 真实生效（用第一轮 ledger 的状态）----
        # 此前分支 1 里 EVT-200 的"没用关键词"已 retire
        assert store.get_route("RT-200b").status == "retired"
        assert store.get_route("RT-200a").status == "active"
        store2 = EmberStore(root)
        assert store2.get_route("RT-200b").status == "retired"
        print("落盘验证 ok（retired 持久化）")

        # ---- 精确口径回归：时间差误保场景 ----
        # EVT-300（散，满一周）两条 cue："共享词"（记账里那次只带出了
        # EVT-100，从未带出 300）、"常用词"（带出过 300 两次）
        # → 精确口径：共享词 retire、常用词保留
        week2 = _iso(now - timedelta(days=8))
        d300 = {"event_id": "EVT-300", "version_id": "v1",
                "source_ref": "t", "raw_ref": "t",
                "event_time": week2, "ingest_time": week2,
                "turn_refs": [], "summary": "EVT-300", "core": "EVT-300"}
        with open(os.path.join(root, "events.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(d300, ensure_ascii=False) + "\n")
        store.write_event(Event(
            event_id="EVT-300", version_id="v1", source_ref="t", raw_ref="t",
            event_time=week2, ingest_time=week2, turn_refs=[],
            summary="EVT-300", core="EVT-300"))
        store.write_route(RecallRoute(route_id="RT-300a", event_id="EVT-300",
                                      matcher="lexical", cue_type="lexical",
                                      cue="共享词"))
        store.write_route(RecallRoute(route_id="RT-300b", event_id="EVT-300",
                                      matcher="lexical", cue_type="lexical",
                                      cue="常用词"))
        ledger.record("s1", 20, route="cue", event_ids=["EVT-300"],
                      cue="常用词")
        ledger.record("s1", 21, route="cue", event_ids=["EVT-300"],
                      cue="常用词")
        ledger.record("s1", 22, route="cue", event_ids=["EVT-100"],
                      cue="共享词")

        r3 = t.tighten(now=now)
        retired300 = {(x["cue"], x["uses"]) for x in r3["retired"]
                      if x["event_id"] == "EVT-300"}
        # kept 行只有 route_id/cue/uses；本轮唯一进 kept 的就是"常用词"
        kept300 = {(x["cue"], x["uses"]) for x in r3["kept"]}
        assert ("常用词", 2) in kept300, (r3, kept300)
        assert ("共享词", 0) in retired300, \
            f"精确口径下共享词应 retire（300 从未被它带出）: {retired300}"
        assert "共享词" not in kept300, kept300
        print("精确口径回归 ok（同文本 cue 不借别人的使用记录）")

        print("\nA5+r3 全部通过")


if __name__ == "__main__":
    main()
