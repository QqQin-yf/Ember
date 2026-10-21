"""Real App HTTP provider + Ember consumers, temporary DB, no model or production config.

Run: python -B scripts/check_source_path_app.py --app-dir D:/deliz/server
The App repository is read-only. Its actual index.ts is started on an ephemeral
loopback port; only synthetic fixtures and generated credentials are used.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ember import source_client
from ember.service import EmberService
from ember.store import EmberStore
from ember.llm import MockLLMClient
from ember.models import Event

BOOTSTRAP = r'''
import { writeFileSync } from "node:fs";
const { insertMessage, setLeaf, ensureConversation } = await import(APP_DB);
const conv = ensureConversation("source-test"), other = ensureConversation("other-test");
const say = (p, role, text, extra = {}) => insertMessage({ conversationId: conv.id,
  parentId: p?.id ?? null, role, content: [{type:"text", text}], ...extra });
const u1 = say(null,"user","original-user"), a1 = say(u1,"assistant","original-reply");
const u2 = say(a1,"user","old-user"), a2 = say(u2,"assistant","old-reply");
const a2b = say(u2,"assistant","regenerated-reply");
const u3 = say(a2,"user","old-followup"), a3 = say(u3,"assistant","old-followup-reply");
const ux = say(a1,"user","edited-user"), ax = say(ux,"assistant","edited-reply");
const uc = say(ax,"user","cancel-user"), ac = say(uc,"assistant","partial",{status:"cancelled"});
const sleep = say(a1,"user","sleep-only",{channel:"sleep"});
const foreign = say(null,"user","foreign-body",{conversationId:other.id});
setLeaf(conv.id, ax.id);
writeFileSync(process.env.FIXTURE_FILE,JSON.stringify({sid:conv.id,other:other.id,u1,a1,u2,a2,a2b,u3,a3,ux,ax,uc,ac,sleep,foreign}));
await import(APP_INDEX);
'''

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument("--result-file", type=Path)
    args = parser.parse_args()
    app = args.app_dir.resolve()
    token = secrets.token_hex(32)
    with tempfile.TemporaryDirectory(prefix="ember-path-app-") as tmp:
        root = Path(tmp)
        bootstrap = BOOTSTRAP.replace("APP_DB", json.dumps((app / "src/db.ts").as_uri())).replace("APP_INDEX", json.dumps((app / "src/index.ts").as_uri()))
        (root / "bootstrap.mts").write_text(bootstrap, encoding="utf-8")
        (root / "empty.env").write_text("", encoding="utf-8")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {k:v for k,v in os.environ.items() if not k.startswith(("EMBER_", "HEART_", "LLM_", "DELIZ_", "DOTENV_")) and k not in
               ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TAVILY_API_KEY", "SILICONFLOW_API_KEY", "GLM_API_KEY", "ZHIPU_API_KEY")}
        env.update(DB_PATH=str(root / "app.sqlite3"), DOTENV_CONFIG_PATH=str(root / "empty.env"),
                   FIXTURE_FILE=str(root / "fixture.json"), HOST="127.0.0.1", PORT=str(port),
                   DELIZ_TOKEN=token, EMBER_INSTANCES="default,phase2", EMBER_BASE_URL="", HEART_BASE_URL="",
                   EMBER_RECALL_REFS="0", HEART_PHASE2="0")
        base = f"http://127.0.0.1:{port}"
        with (root / "app.log").open("wb") as log:
            child = subprocess.Popen([shutil.which("node") or "node", "--import", (app / "node_modules/tsx/dist/loader.mjs").as_uri(),
                                      str(root / "bootstrap.mts")], cwd=root, env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    try:
                        with urllib.request.urlopen(base + "/health", timeout=0.3) as resp:
                            assert resp.status == 200
                        break
                    except OSError:
                        if child.poll() is not None:
                            raise RuntimeError("Isolated App failed to start: " + (root / "app.log").read_text(encoding="utf-8", errors="replace")[-3000:].replace(token,"[redacted]"))
                        time.sleep(.05)
                else:
                    raise RuntimeError("Isolated App startup timed out")
                fixture = json.loads((root / "fixture.json").read_text(encoding="utf-8"))
                sid = fixture["sid"]
                checks = []
                def ids(*keys):
                    return [str(fixture[key]["seq"]) for key in keys]
                with patch.multiple(source_client, SOURCE_URL=base, SOURCE_TOKEN=token):
                    for name, keys, contents in (
                        ("old_branch", ("u2","a2","u3","a3"), ["old-user","old-reply","old-followup","old-followup-reply"]),
                        ("regenerated_sibling", ("u2","a2b"), ["old-user","regenerated-reply"]),
                        ("edited_branch", ("ux","ax"), ["edited-user","edited-reply"]),
                        ("non_contiguous_no_fill", ("u1","a1","u3","a3"), ["original-user","original-reply","old-followup","old-followup-reply"])):
                        rows = source_client.fetch_source_path(sid, ids(*keys))
                        assert [r["content"] for r in rows] == contents, name
                        checks.append(name)
                    for name, keys, session in (
                        ("mixed_siblings_rejected", ("a2","a2b"), sid),
                        ("mixed_edited_rejected", ("ux","a3"), sid),
                        ("foreign_session_rejected", ("foreign",), sid),
                        ("unknown_session_rejected", ("u1",), "missing"),
                        ("cancelled_rejected", ("uc","ac"), sid),
                        ("sleep_rejected", ("sleep",), sid)):
                        try:
                            source_client.fetch_source_path(session, ids(*keys))
                        except source_client.SourceUnavailable:
                            checks.append(name)
                        else:
                            raise AssertionError(name)
                    svc = EmberService(EmberStore(str(root / "ember")), MockLLMClient())
                    def event(eid, turns):
                        ev = Event(event_id=eid, version_id=eid + "-v1", source_ref="conversation/session-"+sid,
                                   raw_ref="r", event_time="2026-10-21", ingest_time="2026-10-21", summary=eid, core=eid)
                        spans = [(turn, turn) for turn, _, _ in turns]
                        corpus = [{"turn": turn, "role": role, "_source_session_id": sid, "_source_message_id": seq}
                                  for turn, u, a in turns for role, seq in (("user",u),("assistant",a))]
                        svc.event_sources.capture(eid, ev.version_id, sid, spans, corpus)
                        svc.store.write_event(ev)
                        svc.turn_spans.fill(eid, spans, filled_by="synthetic-probe")
                        return ev
                    ev = event("old", [(2,*ids("u2","a2")), (3,*ids("u3","a3"))])
                    assert "old-reply" in svc.read_source_event("old")["transcript"]
                    lines = []
                    assert svc._inline_source_lines("old",lines)
                    assert "old-followup" in svc._briefing_source_block(ev)
                    checks.append("event_inline_briefing_old_path")
                    event("mixed", [(1,*ids("u2","a2b")), (2,*ids("u3","a3"))])
                    assert svc.read_source_event("mixed")["status"] == "not_archived"
                    checks.append("event_mixed_not_archived")
                    for rid, user, assistant, text in (("g1","u2","a2","old-user"),("g2","u3","a3","old-followup")):
                        out = svc.hook_turn_start(sid, text, user_message_id=fixture[user]["seq"], recall_id=rid)
                        assert "turn" in out, out
                        assert svc.hook_turn_end(sid, ("old-reply" if rid=="g1" else "old-followup-reply"),
                            turn=out["turn"], assistant_message_id=fixture[assistant]["seq"], recall_id=rid)["ok"]
                    corpus = svc._quote_corpus(svc.sessions[sid])
                    assert [m["content"] for m in corpus] == ["old-user","old-reply","old-followup","old-followup-reply"]
                    # Fetch only registered exact messages; no numeric gap/leaf expansion.
                    checks.append("quote_corpus_exact_generations")
                    svc.hook_turn_start(sid, "edited-user", user_message_id=fixture["ux"]["seq"], recall_id="g3")
                    svc.hook_turn_end(sid,"edited-reply",turn=3,assistant_message_id=fixture["ax"]["seq"],recall_id="g3")
                    assert svc._fetched_corpus(sid) == []
                    cursor = svc.observe_cursor.get(sid)
                    try:
                        svc._observe_session(sid)
                    except source_client.SourceUnavailable:
                        pass
                    else:
                        raise AssertionError("mixed observation accepted")
                    assert svc.observe_cursor.get(sid) == cursor
                    checks.append("mixed_observe_no_cursor_advance")
                    with patch.object(source_client,"SOURCE_TOKEN","wrong-synthetic-token"):
                        try:
                            source_client.fetch_source_path(sid,ids("u1","a1"))
                        except source_client.SourceUnavailable:
                            checks.append("invalid_auth_rejected")
                        else:
                            raise AssertionError("invalid auth accepted")
                    # App currently ignores this header. Record this as a production
                    # isolation prerequisite, not a passed cross-instance test.
                    try:
                        source_client.fetch_source_path(sid, ids("u1","a1"), instance="unknown-instance")
                    except source_client.SourceUnavailable:
                        instance_enforced = True
                    else:
                        instance_enforced = False
                report = {"status":"passed", "provider":"real App index.ts + temporary SQLite", "checks":checks,
                          "cross_instance_provider_enforced":instance_enforced,
                          "production_config_verified":False, "production_switches_changed":False}
                if args.result_file:
                    args.result_file.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
                print(json.dumps(report,ensure_ascii=False))
            finally:
                child.terminate()
                child.wait(timeout=15)

if __name__ == "__main__":
    main()
