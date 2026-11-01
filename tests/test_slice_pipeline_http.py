"""Actual HTTP hooks + HTTP source + HTTP embedding, all isolated fixtures."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from ember import source_client
from ember.instances import InstanceRegistry
from ember.slice_embedding import SliceEmbeddingConfig
from ember.turn_slices import TurnSliceStore


class Provider(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.reads.append(self.path)
        messages=[{'message_id':'1','role':'user','content':'original user'},
                  {'message_id':'2','role':'assistant','content':'original assistant'}]
        from urllib.parse import parse_qs,urlsplit
        if urlsplit(self.path).path=='/api/source/increment':
            cursor=json.loads(parse_qs(urlsplit(self.path).query)['cursor'][0])
            self.respond({'cursor':'latest','has_more':False,'turns':[] if cursor else [
                {'session_id':'s','messages':messages,'user_is_system':self.server.system}]})
        else:
            self.respond({'messages':messages})

    def do_POST(self):
        payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.inputs.append(payload)
        self.server.embedding_started.set()
        self.server.allow_embedding.wait(5)
        from ember.lifecycle_config import lifecycle_config
        vector=[0.,1.,0.] if payload['input'] in lifecycle_config()['slice']['filler_texts'] else [1.,.5,-.25]
        self.respond({'model':self.server.model,'data':[{'index':0,'embedding':vector}]})

    def respond(self, payload):
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class PipelineHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = InstanceRegistry(self.tmp.name, ['default', 'other'])
        self.provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        self.provider.reads, self.provider.inputs = [], []
        self.provider.system=False
        self.provider.embedding_started = threading.Event()
        self.provider.allow_embedding = threading.Event()
        self.thread = threading.Thread(target=self.provider.serve_forever)
        self.thread.start()
        self.addCleanup(self.close_provider)
        base = f'http://127.0.0.1:{self.provider.server_port}'
        self.config = SliceEmbeddingConfig('synthetic-secret', base_url=base + '/v1', timeout_seconds=5)
        self.provider.model = self.config.model
        import sidecar
        from ember.slice_reranker import RerankerConfig
        self.sidecar = sidecar
        for patcher in (patch.object(sidecar, 'REGISTRY', self.registry), patch.object(sidecar, 'TOKEN', ''),
                        patch.object(sidecar, 'session_manager', sidecar.StreamableHTTPSessionManager(
                            app=sidecar.mcp_server, json_response=False, stateless=True)),
                        patch.object(SliceEmbeddingConfig, 'load', return_value=self.config),
                        patch('ember.slice_presentation.RerankerConfig.load',return_value=RerankerConfig()),
                        patch.multiple(source_client, SOURCE_URL=base, SOURCE_TOKEN='synthetic-source-token')):
            patcher.start()
            self.addCleanup(patcher.stop)

    def close_provider(self):
        self.provider.allow_embedding.set()
        self.provider.shutdown()
        self.provider.server_close()
        self.thread.join()

    def finish_turn(self, client, system=False):
        self.provider.system=system
        start = client.post('/ember/hooks/turn_start', json={
            'session_id': 's', 'message': 'hook payload is not source',
            'user_message_id': 1, 'recall_id': 'r'})
        self.assertEqual(start.status_code, 200, start.text)
        result = client.post('/ember/hooks/turn_end', json={
            'session_id': 's', 'assistant_reply': 'hook reply is not source',
            'assistant_message_id': 2, 'turn': start.json()['turn'],
            'recall_id': 'r', 'user_is_system': system})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()['ok'])
        self.assertNotIn('slice_ingestion',result.json())
        return result.json()

    def refresh(self,client):
        return client.post('/ember/briefing/refresh',json={'session_id':'s','keep_from_message_id':3})

    def slices(self):
        store=TurnSliceStore(self.registry.dir_of('default'))
        if not store.path.exists():
            return store,[]
        with store._db() as db:
            ids=[r[0] for r in db.execute('SELECT slice_id FROM turn_slices')]
        return store,[store.get(i) for i in ids]

    def test_turn_end_does_not_slice_and_briefing_pulls_original_increment(self):
        self.provider.allow_embedding.set()
        with TestClient(self.sidecar.app) as client:
            self.finish_turn(client)
            self.assertFalse(self.provider.reads)
            self.refresh(client)
            store,rows=self.slices()
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['messages'][0]['content'],'original user')
            self.assertEqual(store.embedding(rows[0]['slice_id'],self.config.space)['dimensions'],3)
            self.refresh(client)
            self.assertEqual(len(self.slices()[1]),1)
            self.assertTrue(all('/api/source/increment?' in r for r in self.provider.reads))
            self.assertIn('user:\noriginal user\n\nassistant:\noriginal assistant',[r['input'] for r in self.provider.inputs])

    def test_provider_system_flag_excludes_user_from_slice_and_embedding(self):
        self.provider.allow_embedding.set()
        with TestClient(self.sidecar.app) as client:
            self.finish_turn(client,system=True)
            self.refresh(client)
        store,rows=self.slices()
        self.assertEqual([m['role'] for m in rows[0]['messages']],['assistant'])
        self.assertIn('assistant:\noriginal assistant',[r['input'] for r in self.provider.inputs])

    def test_http_rejects_non_boolean_system_marker(self):
        with TestClient(self.sidecar.app) as client:
            result = client.post('/ember/hooks/turn_end', json={'user_is_system': 'false'})
            self.assertEqual(result.status_code, 400)
        self.assertFalse(self.provider.reads)

    def test_raw_recall_preview_and_next_hook_use_slice_vector_without_injection(self):
        self.provider.allow_embedding.set()
        with TestClient(self.sidecar.app) as client:
            self.finish_turn(client)
            self.refresh(client)
            preview = client.post('/ember/recall/slices/preview', json={'input': 'fresh preview query'})
            self.assertEqual(preview.status_code, 200, preview.text)
            metadata = preview.json()
            self.assertEqual(metadata['ranked_count'], 1)
            self.assertEqual(metadata['candidate_threshold'], .6)
            self.assertEqual(metadata['candidate_count'], 1)
            self.assertFalse(metadata['injected'])
            trace = client.get('/ember/recall/slices/' + metadata['trace_id'] + '?view=sources')
            self.assertEqual(trace.status_code, 200)
            self.assertEqual(trace.json()['rankings'][0]['messages'][0]['content'], 'original user')
            self.assertEqual(len(trace.json()['candidate_pool']), 1)
            self.assertEqual(client.get('/ember/recall/slices/' + metadata['trace_id'],
                headers={'X-Heart-Instance': 'other'}).status_code, 404)
            start = client.post('/ember/hooks/turn_start', json={
                'session_id': 's', 'message': 'fresh hook query', 'user_message_id': 3, 'recall_id': 'next'})
            self.assertEqual(start.status_code, 200, start.text)
            self.assertEqual(start.json()['slice_recall']['ranked_count'], 1)
            self.assertFalse(start.json()['injected'])
            self.assertIsNone(start.json()['injected_text'])
            self.assertEqual(self.provider.inputs[-1]['input'], 'fresh hook query')
