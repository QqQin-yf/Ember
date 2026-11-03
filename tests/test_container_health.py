import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

class ContainerHealthTests(unittest.TestCase):
    def test_health_is_authenticated_without_initializing_workers(self):
        with tempfile.TemporaryDirectory() as root:
            env={**os.environ,'EMBER_DATA_ROOT':root,'EMBER_TOKEN':'fixture-token','EMBER_HOST':'127.0.0.1'}
            code="""import asyncio,httpx,sidecar
async def check():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=sidecar.app),base_url='http://fixture') as c:
        assert (await c.get('/health')).status_code==401
        r=await c.get('/health',headers={'Authorization':'Bearer fixture-token'})
        assert r.status_code==200 and r.json()=={'ok':True,'service':'ember'}
asyncio.run(check())
"""
            result=subprocess.run([sys.executable,'-c',code],env=env,cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_recall_embedding_base_override_keeps_provider_and_model(self):
        from unittest.mock import patch
        from ember.model_config import settings, ModelConfigError
        with patch.dict(os.environ,{'EMBER_EMBED_BASE_URL':'https://recall.example/v1'}):
            recall=settings('recall_embed',require_key=False)
            main=settings('embed',require_key=False)
            self.assertEqual(recall.base_url,'https://recall.example/v1')
            self.assertEqual(recall.model,'Qwen/Qwen3-Embedding-8B')
            self.assertEqual(recall.key_env,'EMBER_EMBED_API_KEY')
            self.assertEqual(main.base_url,'https://api.siliconflow.cn/v1')
        with patch.dict(os.environ,{'EMBER_EMBED_BASE_URL':'http://insecure.example/v1'}):
            with self.assertRaises(ModelConfigError):settings('recall_embed',require_key=False)
