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
        self.respond({'messages': [{'message_id': '1', 'role': 'user', 'content': 'original user'},
                                   {'message_id': '2', 'role': 'assistant', 'content': 'original assistant'}]})

    def do_POST(self):
        self.server.inputs.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
        self.server.embedding_started.set()
        self.server.allow_embedding.wait(5)
        self.respond({'model': self.server.model, 'data': [{'index': 0, 'embedding': [1., .5, -.25]}]})

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
        self.provider.embedding_started = threading.Event()
        self.provider.allow_embedding = threading.Event()
        self.thread = threading.Thread(target=self.provider.serve_forever)
        self.thread.start()
        self.addCleanup(self.close_provider)
        base = f'http://127.0.0.1:{self.provider.server_port}'
        self.config = SliceEmbeddingConfig('synthetic-secret', base_url=base + '/v1', timeout_seconds=5)
        self.provider.model = self.config.model
        import sidecar
        self.sidecar = sidecar
        for patcher in (patch.object(sidecar, 'REGISTRY', self.registry), patch.object(sidecar, 'TOKEN', ''),
                        patch.object(sidecar, 'session_manager', sidecar.StreamableHTTPSessionManager(
                            app=sidecar.mcp_server, json_response=False, stateless=True)),
                        patch.object(SliceEmbeddingConfig, 'load', return_value=self.config),
                        patch.multiple(source_client, SOURCE_URL=base, SOURCE_TOKEN='synthetic-source-token')):
            patcher.start()
            self.addCleanup(patcher.stop)

    def close_provider(self):
        self.provider.allow_embedding.set()
        self.provider.shutdown()
        self.provider.server_close()
        self.thread.join()

    def finish_turn(self, client, system=False):
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
        return result.json()['slice_ingestion']['job_id']

    def await_ready(self, client, job_id):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            result = client.get('/ember/slices/jobs/' + job_id)
            self.assertEqual(result.status_code, 200)
            if result.json()['state'] == 'ready':
                return result.json()
            time.sleep(.02)
        self.fail('Background pipeline did not reach ready')

    def test_turn_end_returns_while_embedding_waits_then_all_data_is_stored(self):
        with TestClient(self.sidecar.app) as client:
            job_id = self.finish_turn(client)
            try:
                self.assertTrue(self.provider.embedding_started.wait(4))
                self.assertFalse(self.provider.allow_embedding.is_set())
                status = client.get('/ember/slices/jobs/' + job_id).json()
                self.assertEqual(status['state'], 'working')
                store = TurnSliceStore(self.registry.dir_of('default'))
                self.assertEqual(store.get(status['slice_id'])['messages'][0]['content'], 'original user')
            finally:
                self.provider.allow_embedding.set()
            status = self.await_ready(client, job_id)
            self.assertEqual(store.embedding(status['slice_id'], self.config.space)['dimensions'], 3)
            self.assertEqual(self.provider.inputs[0]['input'],
                             'user:\noriginal user\n\nassistant:\noriginal assistant')
            self.assertEqual(len(self.provider.reads), 1)
            self.assertEqual(client.get('/ember/slices/jobs/' + job_id,
                headers={'X-Heart-Instance': 'other'}).status_code, 404)

    def test_http_system_flag_excludes_user_from_slice_and_embedding(self):
        self.provider.allow_embedding.set()
        with TestClient(self.sidecar.app) as client:
            job_id = self.finish_turn(client, system=True)
            status = self.await_ready(client, job_id)
        store = TurnSliceStore(self.registry.dir_of('default'))
        self.assertEqual([m['role'] for m in store.get(status['slice_id'])['messages']], ['assistant'])
        self.assertEqual(self.provider.inputs[0]['input'], 'assistant:\noriginal assistant')

    def test_http_rejects_non_boolean_system_marker(self):
        with TestClient(self.sidecar.app) as client:
            result = client.post('/ember/hooks/turn_end', json={'user_is_system': 'false'})
            self.assertEqual(result.status_code, 400)
        self.assertFalse(self.provider.reads)
