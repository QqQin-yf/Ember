import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from ember.model_config import registry,settings,profile,ModelConfigError
from ember.model_gateway import call_model,ModelCallError
from ember.slice_embedding import SliceEmbeddingConfig,SliceEmbeddingClient


class ModelGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'models.json';self.data=registry()
        self.data['providers']={'remote':{'base_url':'https://models.example/v1','key_env':'FIXTURE_MODEL_KEY'},
            'local':{'transport':'docker','container':'fixture','command':['ollama','run'],'arguments':['/no_think']}}
        for purpose,row in self.data['purposes'].items():
            row['provider']='local' if purpose=='direction_local' else 'remote'
        self.write()
        env=patch.dict(os.environ,{'EMBER_MODELS_CONFIG':str(self.path),'FIXTURE_MODEL_KEY':'private-fixture-key'})
        env.start();self.addCleanup(env.stop)

    def write(self):
        self.path.write_text(json.dumps(self.data),encoding='utf-8')

    def reply(self,value=None):
        return io.BytesIO(json.dumps(value or {'data':[{'index':0,'embedding':[1,0]}]}).encode())

    def test_one_purpose_change_updates_payload_and_vector_namespace(self):
        before=SliceEmbeddingConfig.load();self.data['purposes']['embed']['model']='replacement-model';self.write()
        after=SliceEmbeddingConfig.load();self.assertNotEqual(before.space,after.space)
        with patch('urllib.request.urlopen',return_value=self.reply()) as send:
            self.assertEqual(SliceEmbeddingClient(after).embed('original'),[1,0])
        request=send.call_args.args[0]
        self.assertEqual(json.loads(request.data)['model'],'replacement-model')
        self.assertEqual(request.full_url,'https://models.example/v1/embeddings')
        self.assertEqual(request.get_header('Authorization'),'Bearer private-fixture-key')

    def test_inherited_provider_and_missing_key_stop_before_network(self):
        self.data['providers']['inherited']={'inherits':'remote','key_env':'ABSENT_FIXTURE_KEY'}
        self.data['purposes']['embed']['provider']='inherited';self.write()
        self.assertEqual(settings('embed',require_key=False).base_url,'https://models.example/v1')
        with patch('urllib.request.urlopen') as send,self.assertRaises(ModelConfigError):call_model('embed',{'input':'text'})
        send.assert_not_called()

    def test_shared_recall_embedding_keeps_purpose_provider_and_call_policy(self):
        from ember.recall_embedding import embedding_model,embed_text
        self.data['purposes']['recall_embed']['model']='fixture-recall-model';self.write()
        self.assertEqual(embedding_model(),'fixture-recall-model')
        with patch('urllib.request.urlopen',return_value=self.reply()) as send:
            self.assertEqual(embed_text('query',timeout=7,retries=0),[1,0])
        request=send.call_args.args[0]
        self.assertEqual(json.loads(request.data),{'input':'query','model':'fixture-recall-model'})
        self.assertEqual(request.full_url,'https://models.example/v1/embeddings')
        self.assertEqual(request.get_header('Authorization'),'Bearer private-fixture-key')
        self.assertEqual(send.call_args.kwargs['timeout'],7)
        with patch('ember.model_gateway.call_model',return_value={'data':[{'embedding':[0,0]}]}):
            with self.assertRaisesRegex(ModelCallError,'response_invalid'):
                embed_text('query')

    def test_secrets_and_provider_cycles_are_invalid(self):
        self.data['providers']['remote']['api_key']='secret';self.write()
        with self.assertRaises(ModelConfigError):registry()
        del self.data['providers']['remote']['api_key'];self.data['providers']['remote']['inherits']='remote';self.write()
        with self.assertRaises(ModelConfigError):settings('embed')

    def test_429_retries_but_401_does_not_and_closes_errors(self):
        self.data['purposes']['embed']['retries']=2;self.write()
        error=urllib.error.HTTPError('https://models.example',429,'secret',{},io.BytesIO(b'private body'))
        with patch('urllib.request.urlopen',side_effect=[error,self.reply()]) as send,patch('time.sleep') as sleep:
            call_model('embed',{'input':'private text'})
        self.assertEqual(send.call_count,2);self.assertTrue(error.fp.closed);sleep.assert_called_once()
        error=urllib.error.HTTPError('https://models.example',401,'secret',{},io.BytesIO(b'private body'))
        with patch('urllib.request.urlopen',side_effect=error) as send,self.assertRaisesRegex(ModelCallError,'^model_http_401$'):
            call_model('embed',{'input':'text'})
        self.assertEqual(send.call_count,1);self.assertTrue(error.fp.closed)

    def test_logs_do_not_include_secrets_or_user_text(self):
        error=urllib.error.URLError('private-fixture-key private-user-text')
        with patch('urllib.request.urlopen',side_effect=error),self.assertLogs('ember.models') as logs:
            with self.assertRaises(ModelCallError) as caught:call_model('embed',{'input':'private-user-text'})
        output=' '.join(logs.output)+str(caught.exception)
        self.assertNotIn('private-fixture-key',output);self.assertNotIn('private-user-text',output)

    def test_proxy_and_timeout_use_central_policy(self):
        self.data['purposes']['embed'].update(proxy='direct',timeout_seconds=17);self.write()
        with patch('urllib.request.build_opener') as opener,patch('urllib.request.urlopen') as env_send:
            opener.return_value.open.return_value=self.reply();call_model('embed',{'input':'text'})
        env_send.assert_not_called();self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'],17)

    def test_local_transport_uses_registry_and_checks_exit_status(self):
        with patch('subprocess.run',return_value=subprocess.CompletedProcess([],0,b'local reply',b'')) as run:
            value=call_model('direction_local',{'messages':[{'role':'user','content':'prompt'}]})
        self.assertEqual(value['choices'][0]['message']['content'],'local reply')
        self.assertIn('fixture',run.call_args.args[0]);self.assertTrue(run.call_args.kwargs['check'])

    def test_no_per_call_model_override_or_secret_profile(self):
        with self.assertRaises(ModelConfigError):call_model('embed',{'model':'override','input':'text'})
        path=self.path.parent/'profile.json';path.write_text(json.dumps({'purpose':'embed','api_key':'secret'}))
        with self.assertRaises(ModelConfigError):profile(path,'embed')


if __name__=='__main__':unittest.main()
