# -*- coding: utf-8 -*-
"""ember 梦通道真 HTTP 端到端验收（2026-10-14，与 heart 侧 test_dream_http 对称）。

与 test_dream_channel.py（fake pusher / urlopen mock）互补——这里：
- 推送目标 = 本地假 heart（真 HTTP），DreamPusher 真发请求；
- 不依赖真 heart、不依赖 chat，事件数据全部捏造；
- 三场景（与 heart 侧验收对齐）：
  1. 无梦之夜：空库选材 → events=[] → dream_id=null → 账本 ok；
  2. 最小梦：捏造事件 → 选材 → 推送体（text/turn_span + heart 坐标）
     → dream_id 落账本 + 已入梦标记；
  3. 验收拒绝：heart 400 带原因 → DreamPushError 即抛（4xx 不重试）
     → 账本 failed 不计入已入梦 → heart 恢复后再推成功。
另验鉴权（Bearer token + X-Heart-Instance 路由头）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from ember.store import EmberStore  # noqa: E402
from ember.models import Event  # noqa: E402
from ember.turn_ledger import TurnLedger  # noqa: E402
from ember.turn_span import TurnSpanStore  # noqa: E402
from ember.dream_ledger import DreamLedger  # noqa: E402
from ember.service import EmberService  # noqa: E402
from ember.llm import MockLLMClient  # noqa: E402
from ember.dream_push import DreamPusher, DreamPushError  # noqa: E402


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _mk_event(eid: str, core: str, sid: str = "s1", turn: int = 1) -> Event:
    return Event(event_id=eid, version_id=eid + "-v1",
                 source_ref=f"conversation/session-{sid}",
                 raw_ref="r", event_time=_now_iso(), ingest_time=_now_iso(),
                 summary=core[:10], core=core,
                 turn_refs=[f"turn-{turn}"])


class _FakeHeart(BaseHTTPRequestHandler):
    """本地假 heart：可配置状态码与响应体，记录全部请求。"""

    status = 200
    payload = {"ok": True, "dream_id": "night-0001"}
    received: list[dict] = []
    require_token: str | None = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).received.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "instance": self.headers.get("X-Heart-Instance"),
            "body": body,
        })
        if type(self).require_token is not None:
            expect = f"Bearer {type(self).require_token}"
            if self.headers.get("Authorization") != expect:
                self._respond(401, {"error": "unauthorized"})
                return
        # 对齐真 heart 语义：空数组 = 无梦之夜 → dream_id=null
        events = (body or {}).get("events")
        if isinstance(events, list) and not events:
            self._respond(type(self).status,
                          {**type(self).payload, "dream_id": None})
            return
        self._respond(type(self).status, type(self).payload)

    def _respond(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class DreamPushHttpTests(unittest.TestCase):
    """三场景 + 鉴权：ember 真推送 → 假 heart。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        _FakeHeart.status = 200
        _FakeHeart.payload = {"ok": True, "dream_id": "night-0001"}
        _FakeHeart.received = []
        _FakeHeart.require_token = None
        self.heart = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHeart)
        threading.Thread(target=self.heart.serve_forever,
                         daemon=True).start()
        url = f"http://127.0.0.1:{self.heart.server_address[1]}"
        self.pusher = DreamPusher(base_url=url, auth_token="dream-secret",
                                  instance="loism")
        # 捏造记忆库：两个带 span 的事件 + heart 坐标映射
        self.svc = self._make_svc([("EVT-001-aaaa", "事件一。", 1),
                                   ("EVT-002-bbbb", "事件二。", 2)])

    def tearDown(self) -> None:
        self.heart.shutdown()
        self._tmp.cleanup()

    def _make_svc(self, specs: list[tuple[str, str, int]],
                  root: Path | None = None) -> EmberService:
        root = root or self.tmp
        store = EmberStore(str(root))
        tl = TurnLedger(str(root))
        ts = TurnSpanStore(str(root))
        dl = DreamLedger(str(root))
        for i, (eid, core, turn) in enumerate(specs, start=1):
            store.write_event(_mk_event(eid, core, turn=turn))
            tl.record("s1", turn, f"t-{turn:06d}")
            ts.fill(eid, [(turn, turn)], filled_by="test")
        svc = EmberService.__new__(EmberService)
        EmberService.__init__(svc, store, MockLLMClient(), model="mock")
        return svc

    def _received(self) -> list[dict]:
        return _FakeHeart.received

    def test_1_dreamless_night(self) -> None:
        # 场景①：空库（独立目录，真正的空）→ 选材空 → 推空数组
        # → dream_id=null → 账本 ok
        with tempfile.TemporaryDirectory() as empty_root:
            empty_svc = self._make_svc([], root=Path(empty_root))
            r = empty_svc.push_dream_night(pusher=self.pusher)
            self.assertTrue(r["ok"])
            self.assertIsNone(r["dream_id"])
            self.assertEqual(len(self._received()), 1)
            self.assertEqual(self._received()[0]["body"], {"events": []})
            night = empty_svc.dream_ledger.nights()[-1]
            self.assertEqual(night["status"], "ok")
            self.assertIsNone(night["dream_id"])

    def test_2_minimal_dream_end_to_end(self) -> None:
        # 场景②：捏造两事件 → 选材 → 推送 → dream_id 落账本 + 已入梦标记
        r = self.svc.push_dream_night(pusher=self.pusher)
        self.assertTrue(r["ok"])
        self.assertEqual(r["dream_id"], "night-0001")
        self.assertEqual(r["pushed_ids"], ["EVT-001-aaaa", "EVT-002-bbbb"])
        req = self._received()[-1]
        # 推送体契约：events=[{text, turn_span}]，坐标已换算成 heart 的 t-XXXXXX
        self.assertEqual(req["path"], "/api/dream/material")
        events = req["body"]["events"]
        self.assertEqual(len(events), 2)
        for e in events:
            self.assertEqual(set(e.keys()), {"text", "turn_span"})
        self.assertEqual(events[0]["turn_span"], ["t-000001", "t-000001"])
        # 鉴权与实例路由头
        self.assertEqual(req["auth"], "Bearer dream-secret")
        self.assertEqual(req["instance"], "loism")
        # 账本：ok 夜 + pushed_ids + 已入梦标记
        night = self.svc.dream_ledger.nights()[-1]
        self.assertEqual(night["status"], "ok")
        self.assertEqual(night["event_ids"],
                         ["EVT-001-aaaa", "EVT-002-bbbb"])
        self.assertTrue(self.svc.dream_ledger.dreamed_of("EVT-001-aaaa"))
        # 已入梦事件下夜不再被选中
        r2 = self.svc.push_dream_night(pusher=self.pusher)
        self.assertEqual(r2["pushed_ids"], [])

    def test_3_rejection_then_recovery(self) -> None:
        # 场景③：heart 400 带原因（未入睡）→ 即抛不重试 → 账本 failed
        _FakeHeart.status = 400
        _FakeHeart.payload = {"error": "heart 未入睡（先调 /api/sleep/start）"}
        with self.assertRaises(DreamPushError) as ctx:
            self.svc.push_dream_night(pusher=self.pusher)
        self.assertIn("未入睡", str(ctx.exception))
        self.assertEqual(len(self._received()), 1)  # 4xx 不重试
        night = self.svc.dream_ledger.nights()[-1]
        self.assertEqual(night["status"], "failed")
        self.assertFalse(self.svc.dream_ledger.dreamed_of("EVT-001-aaaa"))
        # heart 恢复（入睡了）：同批材料再推成功入账
        _FakeHeart.status = 200
        _FakeHeart.payload = {"ok": True, "dream_id": "night-0002"}
        r = self.svc.push_dream_night(pusher=self.pusher)
        self.assertEqual(r["dream_id"], "night-0002")
        self.assertTrue(self.svc.dream_ledger.dreamed_of("EVT-001-aaaa"))

    def test_4_auth_required(self) -> None:
        # 鉴权：假 heart 要求 token——错 token 401 上抛，对 token 通过
        _FakeHeart.require_token = "dream-secret"
        bad = DreamPusher(base_url=self.pusher.base_url,
                          auth_token="wrong", instance="loism")
        with self.assertRaises(DreamPushError):
            bad.push_night([{"text": "梦", "turn_span": ["t-000001",
                                                        "t-000001"]}])
        good = DreamPusher(base_url=self.pusher.base_url,
                           auth_token="dream-secret", instance="loism")
        r = good.push_night([])
        self.assertTrue(r["ok"])

    def test_5_unreachable_fails_fast_after_retries(self) -> None:
        # 不可达（连接拒绝）：3 次快速重试后抛 DreamPushError
        dead = DreamPusher(base_url="http://127.0.0.1:9", auth_token="")
        with self.assertRaises(DreamPushError):
            dead.push_night([])
        self.assertEqual(len(self._received()), 0)  # 假 heart 没收到东西


if __name__ == "__main__":
    unittest.main()
