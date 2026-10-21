"""Probe a real isolated sidecar using a private env file; never print tokens.

Writes synthetic sessions/events only to the explicitly selected instance.
The result file contains source identities, not credentials or injected text.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import uuid

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_sidecar import write_args


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--result-file", type=Path, required=True)
    parser.add_argument("--instance", default="phase2")
    args = parser.parse_args()
    config = dict(line.split("=", 1) for line in args.env_file.read_text(encoding="utf-8-sig").splitlines()
                  if line.strip() and not line.lstrip().startswith("#") and "=" in line)
    key = "source-completion-" + uuid.uuid4().hex
    sid, reader = key + "-source", key + "-reader"
    rid, recall = key + "-writer", key + "-recall"
    text = "今天我们决定把 Ember 的传播模型改成 Route Search。"
    with httpx.Client(base_url=config["EMBER_BASE_URL"], timeout=30, headers={
            "Authorization": "Bearer " + config["EMBER_TOKEN"],
            "X-Heart-Instance": args.instance,
            "Accept": "application/json, text/event-stream"}) as client:
        def request(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            # Print status only on failure; never dump request headers/config.
            if response.status_code != 200:
                raise RuntimeError(f"Probe {path} returned HTTP {response.status_code}")
            if "text/event-stream" in response.headers.get("content-type", ""):
                return next(json.loads(line[5:].strip()) for line in response.text.splitlines()
                            if line.startswith("data:"))
            return response.json()

        request("POST", "/ember/hooks/turn_start", json={"session_id": sid,
            "recall_id": rid, "user_message_id": "9100001", "message": text})
        result = request("POST", "/mcp/", headers={"X-Ember-Recall-Id": rid,
            "X-Ember-Tool-Call-Id": "create"}, json={"jsonrpc": "2.0", "id": 1,
            "method": "tools/call", "params": {"name": "create_memory_event",
            "arguments": write_args(cues=(key,))}})
        created = json.loads(result["result"]["content"][0]["text"])
        assert created["status"] == "created", "Synthetic memory creation failed"
        eid = created["event_id"]
        start = {"session_id": reader, "recall_id": recall,
                 "user_message_id": "9100003", "message": key}
        original_response = request("POST", "/ember/hooks/turn_start", json=start)
        before = request("GET", "/ember/recalls/" + recall)
        before_ref = next(r for r in before["source_candidates"] if r["event_id"] == eid)
        assert before_ref["turn_messages"][0]["assistant_message_id"] is None
        end = {"session_id": sid, "recall_id": rid, "turn": 1, "status": "done",
               "assistant_message_id": "9100002", "assistant_reply": "Synthetic committed final reply."}
        ack = request("POST", "/ember/hooks/turn_end", json=end)
        assert ack["ok"]
        after = request("GET", "/ember/recalls/" + recall)
        assert after["snapshot"] == before["snapshot"]
        assert after["sources_revision"] == before["sources_revision"] + 1
        candidate = next(r for r in after["source_candidates"] if r["event_id"] == eid)
        assert candidate["source_evidence"] == "generation_completed"
        assert candidate["turn_messages"][0]["assistant_message_id"] == "9100002"
        assert candidate["completion_bindings"][0]["recall_id"] == rid
        assert request("POST", "/ember/hooks/turn_end", json=end) == ack
        assert request("GET", "/ember/recalls/" + recall) == after
        assert request("POST", "/ember/hooks/turn_start", json=start) == original_response
        assert request("GET", "/ember/generations/" + recall)["status"] == "running"
        request("POST", "/ember/hooks/turn_end", json={"session_id": reader,
            "recall_id": recall, "turn": original_response["turn"], "status": "cancelled"})

        next_rid, next_sid = key + "-next", key + "-next-session"
        request("POST", "/ember/hooks/turn_start", json={"session_id": next_sid,
            "recall_id": next_rid, "user_message_id": "9100005", "message": key})
        fresh = request("GET", "/ember/recalls/" + next_rid)
        fresh_ref = next(r for r in fresh["source_candidates"] if r["event_id"] == eid)
        assert fresh["sources_revision"] == 0
        assert fresh_ref["source_evidence"] == "generation_completed"
        request("POST", "/ember/hooks/turn_end", json={"session_id": next_sid,
            "recall_id": next_rid, "turn": fresh["turn"], "status": "cancelled"})
        report = {"passed": True, "instance": args.instance, "event_id": eid,
                  "source_session_id": sid, "source_recall_id": rid,
                  "recall_id": recall, "next_recall_id": next_rid,
                  "sources_revision": after["sources_revision"],
                  "frozen_reference": next(r for r in before["snapshot"]["recall_refs"]["events"] if r["event_id"] == eid),
                  "source_candidate": candidate,
                  "checks": ["real HTTP/MCP create", "pending source", "same-generation done",
                             "immutable snapshot/response", "revision increments once",
                             "newer generation unaffected", "new recall starts at revision zero"]}
        args.result_file.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
        print("PASS: real source completion, source revision and frozen recall checks")


if __name__ == "__main__":
    main()
