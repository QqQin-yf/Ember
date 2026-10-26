"""Isolated real Heart HTTP server; stdin controls only synthetic candidates.

No heartbeat/delivery thread or daily configuration is loaded. The HTTP handlers,
durable transactions and run validators are the actual Heart implementation.
"""
import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
sys.path.insert(0, config["heart_root"])
from tests.test_dialogue_v1 import _make_server
from heart.boundary import render_standing

server = _make_server(Path(config["runtime"]))
inst = server.default
inst.config = replace(inst.config, auth_token=config["token"])
server.default_config = inst.config
inst.wake_delivery_enabled = True
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
print(json.dumps({"port": server.port}), flush=True)
try:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request["op"] == "candidate":
                window = request["window"]
                with inst.lock:
                    existing = inst.metadata_v1.get_window(window["session_id"])["window"]
                    if existing is None or existing["window_revision"] < window["window_revision"]:
                        inst.metadata_v1.window(window)
                    else:
                        assert existing["window_revision"] == window["window_revision"]
                    cycle = inst.wake_cycle
                    now = time.time()
                    cycle.parameters = replace(cycle.parameters, delay_minutes=(0, 0))
                    cycle.activity(now-10, window["event_id"], session_id=window["session_id"],
                                   activity_revision=window["activity_revision"],
                                   window_revision=window["window_revision"])
                    cycle.threshold = 1
                    cycle.observe(now-5, 5, inst.engine.snapshot())
                    cycle.observe(now, 5, inst.engine.snapshot())
                    body = cycle.take_due(now)
                    assert body is not None, "no due candidate"
                    body.update(protocol_version="heart-wake/1",
                                body_observation=render_standing(inst.engine.snapshot()))
                result = {"candidate": body}
            elif request["op"] == "evidence":
                result = {"events": [e["record"] for e in inst._v1_state["events"].values()]}
            else:
                raise ValueError("unknown fixture operation")
            print(json.dumps(result), flush=True)
        except Exception as error:
            print(json.dumps({"error": type(error).__name__, "message": str(error)}), flush=True)
finally:
    server.shutdown()
    server.server_close()
    thread.join(2)
