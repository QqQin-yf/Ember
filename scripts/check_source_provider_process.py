"""Explicit isolated startup self-check, executed in the actual serving PID.

No public debug endpoints. Called only by run_phase2_integration.py after the
source configuration is loaded and sidecar registry imported, before serving.
Credentials never enter the report, logs or command arguments.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
import uuid
from ember import source_client
from ember.models import Event


def run_checks(sidecar, sample_path: Path, report_path: Path) -> None:
    sample = json.loads(sample_path.read_text(encoding="utf-8-sig"))
    base, secret = source_client.SOURCE_URL.rstrip("/"), source_client.SOURCE_TOKEN
    if base != "http://127.0.0.1:8792":
        raise ValueError("The provided diagnostic fixture is restricted to isolated App 8792")
    expected = {str(m["seq"]): m for m in sample["messages"]}
    results, clients, auth, scopes, fixtures = [], [], [], [], []
    def wire(endpoint, query, instance="default", token=secret):
        request = urllib.request.Request(base + endpoint + "?" + urllib.parse.urlencode(query))
        if instance is not None:
            request.add_header("X-Heart-Instance", instance)
        if token is not None:
            request.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(request, timeout=source_client.SOURCE_TIMEOUT) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())
    def denied(body):
        return (isinstance(body, dict) and "messages" not in body
                and not any(m["text"] in json.dumps(body, ensure_ascii=False) for m in sample["messages"]))
    def same(rows, ids):
        return ([str(r["message_id"]) for r in rows] == ids and all(
            row["role"] == expected[seq]["role"] and row["content"] == expected[seq]["text"]
            for row, seq in zip(rows, ids)))
    for case in sample["cases"]:
        endpoint = case.get("endpoint", "/api/source/path")
        query = {"session_id": case["session_id"]}
        if endpoint == "/api/source/path":
            query["message_ids"] = case["message_ids"]
            ids = sorted(case["message_ids"].split(","), key=lambda x:int(x or 0))
        else:
            query.update({"from": case["from"], "to": case["to"]})
            ids = [str(m["seq"]) for m in sample["messages"] if
                   m["session_id"] == case["session_id"] and case["from"] <= m["seq"] <= case["to"]]
        status, body = wire(endpoint, query, case["instance"])
        success = status == case["expect"]
        if status == 200:
            success = success and same(body.get("messages", []), ids)
        else:
            success = success and denied(body) and body.get("code") == case.get("code")
        row = {"name":case["name"], "http_status":status, "code":body.get("code"), "passed":success,
               "no_body_on_rejection":denied(body) if status != 200 else None}
        if status == 200:
            row["messages"] = [{"message_id":str(m["message_id"]), "role":m["role"], "content":m["content"]} for m in body["messages"]]
        results.append(row)
        if endpoint == "/api/source/path" and case["instance"] is not None and case["message_ids"]:
            try:
                rows = source_client.fetch_source_path(case["session_id"], ids, instance=case["instance"])
                passed = status == 200 and same(rows, ids)
                outcome = "exact_original" if passed else "unexpected_acceptance"
            except source_client.SourceUnavailable as exc:
                passed = status != 200 and str(status) in str(exc)
                outcome = "unavailable"
            clients.append({"name":case["name"], "passed":passed, "outcome":outcome})
    query = {"session_id":"synthetic-src-a", "message_ids":"1,2"}
    for name, token in (("missing_token", None),("wrong_token","invalid-synthetic-token")):
        status, body = wire("/api/source/path",query,token=token)
        auth.append({"name":name,"http_status":status,"passed":status==401 and denied(body)})
    for endpoint in ("/api/conversations","/api/settings","/api/models","/api/files","/api/messages","/api/news/topics","/api/source/path/"):
        status, body = wire(endpoint, {})
        scopes.append({"endpoint":endpoint,"http_status":status,"passed":status==401 and denied(body)})
    # Explicit provenance fixtures only; no fake dialogue hooks or automatic
    # memory tools, no model/Heart/ear calls. Normal MCP reads verify these later.
    fixture_specs = [
        ("current", "default", "synthetic-src-a", [(1,2),(3,4)], True),
        ("old_branch", "default", "synthetic-src-a", [(3,5),(6,7)], True),
        ("second_own", "second", "synthetic-src-c", [(17,18)], True),
        ("default_cross", "default", "synthetic-src-c", [(17,18)], False),
        ("second_cross", "second", "synthetic-src-a", [(1,2)], False),
        ("mixed", "default", "synthetic-src-a", [(3,5),(8,9)], False),
        ("other_session_seq", "default", "synthetic-src-a", [(15,16)], False),
        ("cancelled", "default", "synthetic-src-a", [(13,14)], False),
        ("sleep", "default", "synthetic-src-a", [(12,14)], False),
        ("missing", "default", "synthetic-src-a", [(99998,99999)], False)]
    marker = uuid.uuid4().hex[:12]
    for name, instance, sid, pairs, accepted in fixture_specs:
        svc = sidecar.REGISTRY.service(instance)
        if svc is None:
            raise ValueError("Diagnostic instance missing")
        eid = "SRC-CHECK-"+marker+"-"+name
        version = eid+"-v1"
        spans = [(1,len(pairs))]
        corpus = [{"turn":t,"role":role,"_source_session_id":sid,"_source_message_id":str(seq)}
                  for t,pair in enumerate(pairs,1) for role,seq in zip(("user","assistant"),pair)]
        svc.event_sources.capture(eid,version,sid,spans,corpus)
        svc.store.write_event(Event(event_id=eid,version_id=version,source_ref="conversation/session-"+sid,
            raw_ref="isolated-source-check",event_time="2026-10-21",ingest_time="2026-10-21",
            summary="Explicit isolated source fixture",core="Explicit isolated source fixture"))
        svc.turn_spans.fill(eid,spans,filled_by="isolated-source-fixture")
        result = svc.sidecar_read_memory_source(eid)
        transcript = "\n".join(f"[turn {t}] {role}: {expected[str(seq)]['text']}"
                      for t,pair in enumerate(pairs,1) for role,seq in zip(("user","assistant"),pair)) if accepted else None
        passed = (result.get("status")=="ok" and result.get("transcript")==transcript) if accepted else (
                  result.get("status")=="not_archived" and "transcript" not in result)
        fixtures.append({"name":name,"instance":instance,"event_id":eid,"expected_status":"ok" if accepted else "not_archived",
                         "expected_transcript":transcript,"passed":passed})
    all_ok = all(r["passed"] for group in (results,clients,auth,scopes,fixtures) for r in group)
    report={"status":"passed" if all_ok else "failed", "pid":os.getpid(),
            "execution":"startup self-check inside actual sidecar PID, before listen; confirm same PID via external MCP",
            "source_url":base,"source_timeout":source_client.SOURCE_TIMEOUT,
            "source_token_loaded":bool(secret),"source_token_disclosed":False,
            "loopback_proxy_bypass":urllib.request.proxy_bypass("127.0.0.1"),
            "sample_cases":results,"client_cases":clients,"auth_cases":auth,"scope_cases":scopes,"mcp_fixtures":fixtures,
            "production_verified":False,"production_switches_changed":False}
    report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    if not all_ok or not report["loopback_proxy_bypass"]:
        raise RuntimeError("Isolated source startup self-check failed; see non-secret report")
    print("[isolated-source-check] 18 provider cases passed in serving PID; credentials withheld",flush=True)
