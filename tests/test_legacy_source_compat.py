"""Legacy hooks keep exact historical quotes with Phase 2 off and across restart."""
import json
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from ember import source_client
from ember.service import EmberService
from ember.store import EmberStore
from ember.llm import MockLLMClient
from ember.source_paths import legacy_messages
from tests.test_sidecar import write_args

class Provider(BaseHTTPRequestHandler):
    def do_GET(self):
        query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        ids=[int(x) for x in query.get("message_ids",[""])[0].split(",") if x]
        self.server.requests.append(ids)
        status=self.server.forced_status
        if status:
            body={"code":"source_auth_failed"}
        elif query.get("session_id")!=["main"]:
            status,body=404,{"code":"source_session_unknown"}
        elif any(x not in self.server.messages for x in ids):
            status,body=409,{"code":"source_unavailable"}
        elif 317 in ids and 349 in ids:
            status,body=409,{"code":"source_path_unknown"}
        else:
            status,body=200,{"messages":[dict(message_id=str(x),**self.server.messages[x]) for x in sorted(ids)]}
        data=json.dumps(body).encode()
        self.send_response(status);self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
    def log_message(self,*args):pass

class LegacySourceCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.server=ThreadingHTTPServer(("127.0.0.1",0),Provider)
        self.server.messages={313:{"role":"user","content":"合成旧起句甲"},316:{"role":"assistant","content":"合成旧尾句甲"},
                              317:{"role":"assistant","content":"兄弟分支原句"},321:{"role":"user","content":"合成旧起句乙"},
                              324:{"role":"assistant","content":"合成旧尾句乙"},349:{"role":"user","content":"请记录前面的事情"}}
        self.server.requests=[];self.server.forced_status=None
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        cfg=patch.multiple(source_client,SOURCE_URL=f"http://127.0.0.1:{self.server.server_port}",SOURCE_TOKEN="fake-token",SOURCE_TIMEOUT=1)
        cfg.start();self.addCleanup(cfg.stop)
        self.svc=self.new_service()
        for user,reply in ((313,316),(321,324)):
            self.svc.hook_turn_start("main",self.server.messages[user]["content"],user_message_id=user)
            self.svc.hook_turn_end("main",self.server.messages[reply]["content"],assistant_message_id=reply)
    def new_service(self):return EmberService(EmberStore(self.tmp.name),MockLLMClient())
    def current(self):self.svc.hook_turn_start("main",self.server.messages[349]["content"],user_message_id=349)
    def args(self):
        return write_args(user_quote="合成旧起句甲",quote_spans=[
            {"start_quote":"合成旧起句甲","end_quote":"合成旧尾句甲"},
            {"start_quote":"合成旧起句乙","end_quote":"合成旧尾句乙"}])
    def test_exact_old_quotes_work_without_new_ledger_after_restart(self):
        self.svc=self.new_service();self.current()
        self.assertFalse(__import__('os').path.exists(self.svc.generations.path))
        result=self.svc.sidecar_create_memory_event(self.args())
        self.assertEqual(result["status"],"created",result)
        evidence=self.svc.event_sources.get(result["event_id"])
        self.assertTrue(evidence["identity_verified"])
        self.assertEqual([(m["user_message_id"],m["assistant_message_id"]) for m in evidence["turn_messages"]],[("313","316"),("321","324")])
        self.assertTrue(all(349 in ids for ids in self.server.requests))
    def test_wrong_branch_is_excluded_without_discarding_correct_history(self):
        self.svc.turn_ledger.record("main",7,user_message_id=321)
        self.svc.turn_ledger.record_reply("main",7,317)
        self.svc=self.new_service();self.current()
        rows=self.svc._quote_corpus(self.svc.sessions["main"])
        self.assertNotIn("兄弟分支原句",[m['content'] for m in rows])
        self.assertIn("合成旧尾句甲",[m['content'] for m in rows])
        self.assertTrue(all(349 in ids for ids in self.server.requests))
    def test_raw_conflicting_receipts_never_use_max_reply(self):
        self.svc.turn_ledger.record_reply("main",1,317)
        rows=legacy_messages(self.svc.turn_ledger,"main")
        self.assertFalse(any(m["turn"]==1 for m in rows))
        self.assertEqual(self.svc.turn_ledger.reply_msg[("main",1)],317)
    def test_auth_error_is_not_reported_as_bad_quote_or_retried(self):
        self.current();self.server.forced_status=401
        before=len(self.server.requests)
        result=self.svc.sidecar_create_memory_event(self.args())
        self.assertEqual(result.get("code"),"source_auth_failed",result)
        self.assertNotIn("重摘",result['error'])
        self.assertEqual(len(self.server.requests)-before,1)
    def test_unconfigured_source_has_own_failure(self):
        self.current()
        with patch.object(source_client,"SOURCE_URL",""):
            result=self.svc.sidecar_create_memory_event(self.args())
        self.assertEqual(result.get("code"),"source_unconfigured",result)
    def test_verified_corpus_and_wrong_words_are_distinguished(self):
        self.current();args=self.args();args['quote_spans']=[{"start_quote":"从未说过的起句","end_quote":"从未说过的尾句"}]
        result=self.svc.sidecar_create_memory_event(args)
        self.assertEqual(result.get("code"),"quote_not_found",result)
    def test_network_failure_has_own_failure(self):
        self.current()
        with patch("ember.source_client._open_source",side_effect=OSError("network down")):
            result=self.svc.sidecar_create_memory_event(self.args())
        self.assertEqual(result.get("code"),"source_unreachable",result)

    def test_future_rows_are_not_used_for_an_earlier_anchor(self):
        self.svc.turn_ledger.record("main",8,user_message_id=351)
        self.server.messages[351]={"role":"user","content":"本轮之后才出现的句子"}
        self.current()
        corpus=self.svc._quote_corpus(self.svc.sessions['main'])
        self.assertNotIn("本轮之后才出现的句子",[m['content'] for m in corpus])
        self.assertTrue(all(all(seq <= 349 for seq in ids) for ids in self.server.requests))

    def test_conflict_partition_has_a_request_budget(self):
        from ember.source_paths import current_path_corpus
        identities=legacy_messages(self.svc.turn_ledger,'main')
        identities.append({'turn':7,'role':'assistant','message_id':'317'})
        corpus=current_path_corpus('main',identities,{'turn':3,'role':'user','message_id':'349'},'default',max_requests=3)
        self.assertLessEqual(len(self.server.requests),3)
        self.assertEqual(corpus.issue,'source_read_limit')
        self.assertNotIn('兄弟分支原句',[m['content'] for m in corpus])
