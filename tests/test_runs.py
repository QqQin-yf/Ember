"""Independent proactive runs: durable identity, reply proof and no dialogue fallback."""
import copy
import json
import tempfile
import unittest
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from ember.llm import MockLLMClient
from ember.service import EmberService
from ember.source_client import SourceUnavailable
from ember.store import EmberStore
from ember.runs import RunStore


def start_args(run="run-1", wake="wake-1"):
    return {"event_id":"start-"+run,"kind":"wake","run_id":run,"wake_id":wake,"session_id":"main",
            "activity_revision":4,"window_revision":2,"leaf_id":"real-leaf-uuid","context_start_id":None,
            "messages":[{"message_id":"11","role":"user"},{"message_id":"12","role":"assistant"}],
            "occurred_at":"2026-10-24T12:00:00+08:00"}


def finish_args(status="done", spoke=False):
    return {"event_id":"finish-run-1","kind":"wake","run_id":"run-1","wake_id":"wake-1","session_id":"main",
            "status":status,"spoke":spoke,"assistant_message_id":"13" if spoke else None,
            "assistant_reply":"PRIVATE ACTUAL REPLY" if spoke else "", "tools":[],"finished_at":"2026-10-24T12:01:00+08:00"}


def source_rows(*args, **kwargs):
    return [{"message_id":11,"role":"user","content":"real history"},
            {"message_id":12,"role":"assistant","content":"real past reply"},
            {"message_id":13,"role":"assistant","content":"PRIVATE ACTUAL REPLY"}]


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.svc=EmberService(EmberStore(self.temp.name),MockLLMClient())

    def start(self, body=None):
        return self.svc.sidecar_run_start(start_args() if body is None else body)

    def finish(self, body=None):
        return self.svc.sidecar_run_finish(finish_args() if body is None else body)

    def call(self, call="t", name="read_memory_source", args=None):
        return self.svc.sidecar_call_tool(name,{"event_id":"E"} if args is None else args,run_id="run-1",tool_call_id=call)

    def test_start_does_not_open_turn_recall_or_change_live_context(self):
        sentinel={"session":"unchanged"};self.svc._live_turn=sentinel
        with patch.object(self.svc,"run_recall_pipeline",side_effect=AssertionError("no recall")):
            first=self.start();again=self.start()
        self.assertEqual(first,again)
        self.assertEqual(first["status"],"running")
        self.assertEqual(first["origin"],"assistant_initiated")
        self.assertIs(self.svc._live_turn,sentinel)
        self.assertFalse(self.svc.sessions)
        self.assertFalse(self.svc.store.all_events())
        self.assertFalse((self.root/"generations.sqlite3").exists())
        self.assertFalse((self.root/"turn_ledger.jsonl").exists())

    def test_start_conflicts_and_instance_slot(self):
        self.start()
        changed=start_args();changed["activity_revision"]=5
        self.assertEqual(self.start(changed)["code"],"id_conflict")
        changed=start_args();changed["event_id"]="another"
        self.assertEqual(self.start(changed)["code"],"id_conflict")
        self.assertEqual(self.start(start_args("other","other-wake"))["code"],"run_busy")
        self.finish()
        self.assertEqual(self.start(start_args("other","other-wake"))["status"],"running")

    def test_concurrent_start_and_finish_are_idempotent(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            starts=list(pool.map(lambda _:self.start(),range(8)))
        self.assertTrue(all(x==starts[0] for x in starts))
        with ThreadPoolExecutor(max_workers=4) as pool:
            ends=list(pool.map(lambda _:self.finish(),range(8)))
        self.assertTrue(all(x==ends[0] for x in ends))

    def test_first_terminal_is_immutable_and_original_start_replays(self):
        first=self.start();end=self.finish()
        self.assertEqual(self.finish(),end)
        self.assertEqual(self.start(),first)
        changed=finish_args("cancelled")
        self.assertEqual(self.finish(changed)["code"],"id_conflict")
        changed["event_id"]="new-finish"
        self.assertEqual(self.finish(changed)["code"],"run_finished")
        self.assertEqual(self.svc.sidecar_run_status("run-1")["status"],"done")
        changed=start_args("another","another");changed["event_id"]="finish-run-1"
        self.assertEqual(self.start(changed)["code"],"id_conflict")

    def test_quiet_cancelled_and_error_are_real_terminal_records_only(self):
        for status in ("done","cancelled","error"):
            with tempfile.TemporaryDirectory() as root:
                svc=EmberService(EmberStore(root),MockLLMClient())
                svc.sidecar_run_start(start_args())
                body=finish_args(status)
                body["tools"]=[{"tool_call_id":"app-t","name":"web_fetch","status":"unknown","result_ref":"wake-tool:run-1:app-t"}]
                result=svc.sidecar_run_finish(body)
                self.assertEqual(result["status"],status)
                self.assertFalse(result["spoke"])
                self.assertIsNone(result["assistant_message_id"])
                self.assertEqual(result["tool_evidence"],"app_metadata_only")
                self.assertFalse(svc.store.all_events())
                self.assertFalse(svc.sessions)

    def test_visible_finish_requires_exact_provider_path_and_never_persists_reply(self):
        self.start()
        with patch("ember.source_client.fetch_source_path",side_effect=source_rows) as source:
            result=self.finish(finish_args(spoke=True))
        self.assertEqual(result["status"],"done",result)
        self.assertTrue(result["spoke"])
        self.assertEqual(source.call_args.args,("main",["11","12","13"]))
        self.assertEqual(source.call_args.kwargs,{"instance":"default"})
        self.assertNotIn("PRIVATE ACTUAL REPLY",json.dumps(self.svc.sidecar_run_status("run-1")))
        self.assertNotIn(b"PRIVATE ACTUAL REPLY",(self.root/"runs.sqlite3").read_bytes())
        with patch("ember.source_client.fetch_source_path",side_effect=AssertionError("replay only")):
            self.assertEqual(self.finish(finish_args(spoke=True)),result)

    def test_unavailable_source_freezes_fixed_intent_and_retries_after_restart(self):
        self.start()
        body=finish_args(spoke=True)
        with patch("ember.source_client.fetch_source_path",side_effect=SourceUnavailable("unavailable","source_unreachable")):
            self.assertEqual(self.finish(body)["code"],"run_source_unavailable")
        self.assertEqual(self.svc.sidecar_run_status("run-1")["status"],"finishing")
        self.assertEqual(self.call()["code"],"run_finished")
        changed=finish_args();self.assertEqual(self.finish(changed)["code"],"id_conflict")
        self.svc=EmberService(EmberStore(self.temp.name),MockLLMClient())
        self.assertEqual(self.svc.sidecar_run_status("run-1")["status"],"finishing")
        with patch("ember.source_client.fetch_source_path",side_effect=source_rows):
            self.assertEqual(self.finish(body)["status"],"done")

    def test_wrong_reply_text_role_or_path_does_not_commit_done(self):
        self.start()
        for transform in (lambda r:r[:-1],lambda r:list(reversed(r)),lambda r:[dict(x,role="user") if x["message_id"]==13 else x for x in r],
                          lambda r:[dict(x,content="different") if x["message_id"]==13 else x for x in r]):
            with self.subTest(transform=transform),patch("ember.source_client.fetch_source_path",return_value=transform(source_rows())):
                self.assertEqual(self.finish(finish_args(spoke=True))["code"],"run_source_mismatch")
        self.assertEqual(self.svc.sidecar_run_status("run-1")["status"],"finishing")

    def test_read_receipt_replays_without_reexecuting_even_after_finish(self):
        self.start()
        with patch.object(self.svc,"sidecar_read_memory_source",return_value={"status":"ok","transcript":"PRIVATE READ"}) as read:
            first=self.call();self.assertEqual(self.call(),first)
            self.finish();self.assertEqual(self.call(),first)
            self.assertEqual(read.call_count,1)
            self.assertEqual(self.call(args={"event_id":"different"})["code"],"id_conflict")
            self.assertEqual(self.call(call="new")["code"],"run_finished")
        self.assertNotIn(b"PRIVATE READ",(self.root/"runs.sqlite3").read_bytes())

    def test_restart_preserves_running_but_never_reopens_execution(self):
        original=self.start()
        with patch.object(self.svc,"sidecar_read_memory_source",return_value={"status":"ok","transcript":"PRIVATE"}):self.call()
        self.svc=EmberService(EmberStore(self.temp.name),MockLLMClient())
        self.assertEqual(self.start(),original)
        self.assertFalse(self.svc.sidecar_run_status("run-1")["execution_context_available"])
        self.assertEqual(self.call()["code"],"result_not_retained")
        self.assertEqual(self.call(call="new")["code"],"run_context_lost")
        self.assertEqual(self.finish(finish_args("error"))["status"],"error")

    def test_accepted_unknown_tool_is_not_reexecuted(self):
        self.start()
        with patch.object(self.svc,"sidecar_read_memory_source",side_effect=RuntimeError("PRIVATE ERROR")) as read:
            self.assertEqual(self.call()["code"],"tool_outcome_unknown")
            self.assertEqual(self.call()["code"],"tool_outcome_unknown")
            self.assertEqual(read.call_count,1)
        self.assertNotIn(b"PRIVATE ERROR",(self.root/"runs.sqlite3").read_bytes())

    def test_writes_and_side_effectful_search_are_not_allowed(self):
        self.start()
        for name in ("write_event","revise_memory_event","pin_memory","unpin_memory","search_memory"):
            with self.subTest(name=name):self.assertEqual(self.call(name=name)["code"],"run_tool_not_allowed")
        self.assertFalse(self.svc.store.all_events())

    def test_read_cache_is_bounded_and_eviction_never_reexecutes(self):
        self.start()
        with patch("ember.runs.MAX_READ_RESULTS",1),patch.object(self.svc,"sidecar_read_memory_source",return_value={"status":"ok","transcript":"private"}) as read:
            self.call("first");self.call("second")
            self.assertEqual(len(self.svc.runs.results),1)
            self.assertEqual(self.call("first")["code"],"result_not_retained")
            self.assertEqual(read.call_count,2)

    def test_query_missing_does_not_create_a_database(self):
        self.assertEqual(self.svc.sidecar_run_status("missing")["code"],"run_not_found")
        self.assertFalse((self.root/"runs.sqlite3").exists())

    def test_missing_unknown_mixed_and_cross_instance_never_fall_back(self):
        self.svc._live_turn={"session":"poisoned"}
        self.assertEqual(self.call()["code"],"run_not_found")
        self.assertEqual(self.svc.sidecar_call_tool("read_memory_source",{},run_id="run-1")["code"],"invalid_identity")
        self.assertEqual(self.svc.sidecar_call_tool("read_memory_source",{},run_id="run-1",tool_call_id="t",recall_id="r")["code"],"context_mismatch")
        self.start()
        with tempfile.TemporaryDirectory() as root:
            other=EmberService(EmberStore(root),MockLLMClient())
            self.assertEqual(other.sidecar_run_status("run-1")["code"],"run_not_found")
        self.assertIsNone(self.svc.sidecar_run_status("unknown").get("run_id"))

    def test_invalid_fields_are_rejected_before_database_creation(self):
        cases=[("event_id",None),("kind","user"),("activity_revision",None),("window_revision",True),
               ("leaf_id",12),("messages",[{"message_id":"1","role":"system"}]),
               ("messages",[{"message_id":"1","role":"user","content":"FAKE REMINDER"}]),
               ("occurred_at","2026-10-24T12:00:00")]
        for field,value in cases:
            with self.subTest(field=field):
                body=start_args();body[field]=value
                result=self.start(body)
                self.assertEqual(result["code"],"invalid_arguments",result)
                self.assertFalse((self.root/"runs.sqlite3").exists())
        body=start_args();body["messages"]*=2
        self.assertEqual(self.start(body)["field"],"messages[2].message_id")
        body=start_args();body["reminder"]="not accepted"
        self.assertEqual(self.start(body)["field"],"reminder")

    def test_real_process_crash_after_tool_acceptance_never_reexecutes(self):
        payload=json.dumps({"root":str(self.root),"start":start_args()})
        code = "import json,os,sys; from ember.runs import RunStore; p=json.loads(sys.stdin.read()); r=RunStore(p['root']); r.start(p['start']); r.call('run-1','crashed','read_memory_source',{},lambda *args: os._exit(23))"
        process=subprocess.run([sys.executable,"-B","-c",code],input=payload,text=True,capture_output=True,cwd=Path(__file__).resolve().parents[1],timeout=10)
        self.assertEqual(process.returncode,23,process.stderr)
        view=self.svc.sidecar_run_status("run-1")
        self.assertEqual(view["status"],"running")
        self.assertFalse(view["execution_context_available"])
        result=self.svc.sidecar_call_tool("read_memory_source",{},run_id="run-1",tool_call_id="crashed")
        self.assertEqual(result["code"],"tool_outcome_unknown")
        self.assertEqual(self.finish(finish_args("error"))["status"],"error")

    def test_real_process_preserves_pending_finish_across_crash(self):
        payload=json.dumps({"root":str(self.root),"start":start_args(),"finish":finish_args(spoke=True)})
        code = "import json,os,sys; from ember.runs import RunStore,failure; p=json.loads(sys.stdin.read()); r=RunStore(p['root']); r.start(p['start']); r.finish(p['finish'],lambda *args:failure('run_source_unavailable','pending')); os._exit(24)"
        process=subprocess.run([sys.executable,"-B","-c",code],input=payload,text=True,capture_output=True,cwd=Path(__file__).resolve().parents[1],timeout=10)
        self.assertEqual(process.returncode,24,process.stderr)
        self.assertEqual(self.svc.sidecar_run_status("run-1")["status"],"finishing")
        with patch("ember.source_client.fetch_source_path",side_effect=source_rows):
            self.assertEqual(self.finish(finish_args(spoke=True))["status"],"done")
        self.assertNotIn(b"PRIVATE ACTUAL REPLY",(self.root/"runs.sqlite3").read_bytes())

    def test_tool_result_reference_is_metadata_only_and_has_app_shape(self):
        self.start()
        body=finish_args()
        body["tools"]=[{"tool_call_id":"t","name":"web_fetch","status":"completed","result_ref":"https://not-tool-evidence"}]
        result=self.finish(body)
        self.assertEqual(result["code"],"invalid_arguments")
        self.assertEqual(result["field"],"tools[0].result_ref")
        self.assertEqual(self.svc.sidecar_run_status("run-1")["status"],"running")

    def test_invalid_finish_identity_status_or_spoke_never_becomes_terminal(self):
        self.start()
        for field,value in (("session_id","wrong"),("wake_id","wrong"),("status",[]),("spoke",1),
                            ("assistant_reply","uncommitted"),("assistant_message_id","13"),("finished_at","2020-01-01T00:00:00Z")):
            with self.subTest(field=field):
                body=finish_args();body[field]=value
                self.assertIn(self.finish(body)["code"],("invalid_arguments","context_mismatch"))
                self.assertEqual(self.svc.sidecar_run_status("run-1")["status"],"running")




class RunHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncio,importlib,os
        import httpx
        from ember.instances import InstanceRegistry
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        with patch("ember.env.load_env"),patch.dict(os.environ,{"EMBER_DATA_ROOT":self.temp.name,"EMBER_INSTANCES":"default,other","EMBER_TOKEN":""}):
            self.http=importlib.import_module("sidecar")
        self.registry=InstanceRegistry(self.temp.name,["default","other"])
        for name,value in (("REGISTRY",self.registry),("TOKEN","test-auth")):
            setting=patch.object(self.http,name,value);setting.start();self.addCleanup(setting.stop)
        auth=patch.object(self.http.app,"_expect",b"Bearer test-auth")
        auth.start();self.addCleanup(auth.stop)
        self.manager=StreamableHTTPSessionManager(app=self.http.mcp_server,json_response=True,stateless=True)
        setting=patch.object(self.http,"session_manager",self.manager);setting.start();self.addCleanup(setting.stop)
        ready,stop=asyncio.Event(),asyncio.Event()
        async def serve():
            async with self.manager.run():
                ready.set();await stop.wait()
        task=asyncio.create_task(serve());await ready.wait()
        async def close():stop.set();await task
        self.addAsyncCleanup(close)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.http.app),base_url="http://test",
            headers={"Authorization":"Bearer test-auth","Accept":"application/json, text/event-stream"})
        self.addAsyncCleanup(self.client.aclose)

    async def start(self):
        return await self.client.post("/ember/runs/start",json=start_args())

    async def test_http_lifecycle_fields_idempotence_and_terminal_rejection(self):
        first=await self.start();self.assertEqual(first.status_code,200)
        self.assertEqual(first.json()["allowed_tools"],["read_memory_source"])
        again=await self.start();self.assertEqual(first.json(),again.json())
        end=await self.client.post("/ember/runs/finish",json=finish_args())
        self.assertEqual(end.status_code,200);self.assertEqual(end.json()["status"],"done")
        view=await self.client.get("/ember/runs/run-1")
        self.assertEqual(view.json()["session_id"],"main")
        self.assertFalse(view.json()["execution_context_available"])
        changed=finish_args("cancelled")
        self.assertEqual((await self.client.post("/ember/runs/finish",json=changed)).status_code,409)

    async def test_http_auth_unknown_instance_duplicates_and_malformed_body(self):
        self.assertEqual((await self.client.post("/ember/runs/start",headers={"Authorization":"Bearer wrong"},json=start_args())).status_code,401)
        self.assertEqual((await self.client.post("/ember/runs/start",headers={"X-Heart-Instance":"missing"},json=start_args())).status_code,404)
        self.assertEqual((await self.client.post("/ember/runs/start",headers=[("X-Heart-Instance","default"),("X-Heart-Instance","other")],json=start_args())).status_code,400)
        self.assertEqual((await self.client.post("/ember/runs/start",json=[])).status_code,400)
        unknown=await self.client.get("/ember/runs/missing")
        self.assertEqual(unknown.status_code,404);self.assertEqual(unknown.json()["code"],"run_not_found")

    async def test_http_mcp_binds_run_and_never_falls_back_to_ordinary_turn(self):
        await self.start()
        svc=self.registry.service("default")
        body={"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"read_memory_source","arguments":{"event_id":"E"}}}
        headers={"X-Ember-Run-Id":"run-1","X-Ember-Tool-Call-Id":"t"}
        with patch.object(svc,"sidecar_read_memory_source",return_value={"status":"ok","transcript":"PRIVATE"}) as read:
            one=(await self.client.post("/mcp/",headers=headers,json=body)).json()["result"]
            again=(await self.client.post("/mcp/",headers=headers,json=body)).json()["result"]
            self.assertEqual(one,again);self.assertFalse(one["isError"]);self.assertEqual(read.call_count,1)
        mismatch=(await self.client.post("/mcp/",headers={**headers,"X-Ember-Recall-Id":"r"},json=body)).json()["result"]
        self.assertTrue(mismatch["isError"])
        self.assertEqual(json.loads(mismatch["content"][0]["text"])["code"],"context_mismatch")
        other=await self.client.get("/ember/runs/run-1",headers={"X-Heart-Instance":"other"})
        self.assertEqual(other.status_code,404)
        self.assertFalse(svc.sessions)
        self.assertFalse(svc.store.all_events())

    async def test_http_visible_pending_is_retryable_and_response_matches_app(self):
        await self.start()
        with patch("ember.source_client.fetch_source_path",side_effect=SourceUnavailable("down","source_unreachable")):
            pending=await self.client.post("/ember/runs/finish",json=finish_args(spoke=True))
        self.assertEqual(pending.status_code,503);self.assertEqual(pending.headers["retry-after"],"2")
        self.assertEqual((await self.client.get("/ember/runs/run-1")).json()["status"],"finishing")
        with patch("ember.source_client.fetch_source_path",side_effect=source_rows):
            done=await self.client.post("/ember/runs/finish",json=finish_args(spoke=True))
        self.assertEqual(done.status_code,200)
        self.assertEqual((done.json()["run_id"],done.json()["status"]),("run-1","done"))


if __name__=="__main__":unittest.main()
