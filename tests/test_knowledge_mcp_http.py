"""The same MCP discovery, identity and tool calls consumed by App AI."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from ember.instances import InstanceRegistry
from ember.turn_slices import TurnSliceStore


class KnowledgeMCPTests(unittest.TestCase):
    def test_app_reads_and_authors_knowledge_over_ordinary_mcp(self):
        import sidecar
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        with tempfile.TemporaryDirectory() as directory:
            registry=InstanceRegistry(directory,['default'])
            service=registry.service('default')
            source=TurnSliceStore(service.store.root).save('old',[
                {'message_id':1,'role':'user','content':'午饭总吃黄焖鸡，省得纠结。'},
                {'message_id':2,'role':'assistant','content':'固定选择能省心。'}])
            other=TurnSliceStore(service.store.root).save('older',[
                {'message_id':3,'role':'user','content':'午饭选固定的，省去纠结。'},
                {'message_id':4,'role':'assistant','content':'不用重新选。'}])
            from ember.knowledge import KnowledgeStore
            knowledge=KnowledgeStore(service.store.root)
            with knowledge.db() as db:
                db.execute('INSERT INTO memory_groups(group_id,instance,kind,sources_json,reason) VALUES (?,?,?,?,?)',
                    ('group-http','default','pattern',json.dumps(knowledge.sources([source['slice_id'],other['slice_id']])),'same habit'))
            manager=StreamableHTTPSessionManager(app=sidecar.mcp_server,json_response=True,stateless=True)
            with patch.object(sidecar,'REGISTRY',registry),patch.object(sidecar,'TOKEN',''), \
                    patch.object(sidecar,'session_manager',manager), \
                    patch('ember.knowledge.KnowledgeEmbeddingWorker.process_one',return_value=False), \
                    patch('ember.extraction.Extraction.discover',return_value=False), \
                    TestClient(sidecar.app) as client:
                headers={'Accept':'application/json, text/event-stream'}
                listed=client.post('/mcp/',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}}).json()
                names={t['name'] for t in listed['result']['tools']}
                self.assertIn('write_memory_knowledge',names)
                self.assertIn('get_memory_extraction_batch',names)
                self.assertIn('read_memory_slices',names)
                for path,method in [('link/batches','GET'),('link/progress','GET'),('link/submit','POST'),('structure/run','POST')]:
                    retired=client.request(method,'/ember/sleep/'+path,json={} if method=='POST' else None)
                    self.assertEqual(retired.status_code,410)
                    self.assertEqual(retired.json()['code'],'legacy_sleep_step_retired')
                start=client.post('/ember/hooks/turn_start',json={'session_id':'new','message':'current input',
                    'user_message_id':101,'recall_id':'knowledge-http'})
                self.assertEqual(start.status_code,200)
                def call(name,args,cid):
                    response=client.post('/mcp/',headers={**headers,'X-Ember-Recall-Id':'knowledge-http',
                        'X-Ember-Tool-Call-Id':cid},json={'jsonrpc':'2.0','id':2,'method':'tools/call',
                        'params':{'name':name,'arguments':args}})
                    self.assertEqual(response.status_code,200,response.text)
                    outer=response.json()['result']
                    return json.loads(outer['content'][0]['text']),outer.get('isError',False)
                read,error=call('read_memory_slices',{'slice_ids':[source['slice_id']]},'read')
                self.assertFalse(error)
                self.assertIn('午饭',read['slices'][0]['messages'][0]['content'])
                args={'confidence':8,'kind':'pattern','content':'倾向于固定午饭选择以减少决策负担','recall_cues':['黄焖鸡'],
                      'source_slice_ids':[source['slice_id']]}
                ordinary,error=call('write_memory_knowledge',args,'ordinary-write')
                self.assertFalse(error)
                self.assertEqual(ordinary['status'],'created')
                for operation in ('start','tool','finish'):
                    retired=client.post('/ember/sleep/knowledge/'+operation,json={})
                    self.assertEqual(retired.status_code,410)
                    self.assertEqual(retired.json()['code'],'legacy_sleep_step_retired')
                created,error=call('write_memory_knowledge',args,'write')
                self.assertFalse(error)
                repeated,error=call('write_memory_knowledge',args,'write')
                self.assertEqual(repeated,created)
                mid=created['memory_id']
                listed,error=call('list_memory_knowledge',{},'knowledge-list')
                self.assertFalse(error)
                self.assertEqual(listed['items'][0]['memory_id'],mid)
                self.assertNotIn('vector',listed['items'][0])
                client.post('/ember/briefing/refresh',json={'session_id':'new'})
                briefing=client.get('/ember/briefing').json()
                self.assertIn(mid,briefing['knowledge']['memory_ids'])
                changed,error=call('set_knowledge_pin',{'memory_id':mid,'pinned':False},'unpin')
                self.assertEqual(changed['status'],'ok')
                self.assertFalse(changed['pinned'])
                self.assertIn(mid,client.get('/ember/briefing').json()['knowledge']['memory_ids'])
                client.post('/ember/briefing/refresh',json={'session_id':'new'})
                self.assertNotIn(mid,client.get('/ember/briefing').json()['knowledge']['memory_ids'])
                # Raw read receipts keep their transcript in volatile memory.
                receipt=service.generations.call('knowledge-http','read')
                self.assertNotIn('午饭总吃黄焖鸡',receipt['result_json'] or '')



if __name__=='__main__':
    unittest.main()
