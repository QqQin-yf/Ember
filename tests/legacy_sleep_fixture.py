"""Sleep-owned material handoff and durable, idempotent App AI tool receipts."""
from contextlib import contextmanager
import copy
import json

from ember.generations import fingerprint, identity
from ember.knowledge import KnowledgeStore
from tests.legacy_knowledge_tools import dispatch, tool_specs, VOLATILE_TOOLS
from ember.tool_validation import validate_tool_input


def fail(code):
    return {'status':'failed','code':code}


class SleepKnowledge:
    def __init__(self, service):
        self.service=service
        self.store=KnowledgeStore(service.store.root,service.source_instance)
        if not hasattr(service,'_sleep_knowledge_reads'):
            service._sleep_knowledge_reads={}
            service._sleep_knowledge_read_ids={}

    @contextmanager
    def db(self):
        with self.store.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS sleep_knowledge_runs (
                sleep_id TEXT PRIMARY KEY,instance TEXT NOT NULL,session_id TEXT NOT NULL,
                state TEXT NOT NULL,groups_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sleep_knowledge_calls (
                sleep_id TEXT NOT NULL,call_id TEXT NOT NULL,request_hash TEXT NOT NULL,
                name TEXT NOT NULL,state TEXT NOT NULL,result_json TEXT,volatile INTEGER NOT NULL,
                PRIMARY KEY(sleep_id,call_id));''')
            yield db

    def run(self,sid):
        with self.db() as db:
            row=db.execute('SELECT * FROM sleep_knowledge_runs WHERE sleep_id=? AND instance=?',
                (sid,self.store.instance)).fetchone()
        return dict(row) if row else None

    def response(self,row):
        snapshot=json.loads(row['groups_json'])
        current={r['group_id']:r for r in self.store.groups(False)}
        pending=[g for g in snapshot if g['group_id'] in current and current[g['group_id']]['state']=='pending']
        if row['state']=='completed':
            pending=[] # A later substantive update belongs to the next Sleep.
        source_ids={s['slice_id'] for g in pending for s in g['sources']}
        with self.db() as db:
            receipts=db.execute("SELECT c.result_json FROM sleep_knowledge_calls c JOIN sleep_knowledge_runs r USING(sleep_id) WHERE r.instance=? AND c.state='completed' AND c.volatile=0 AND c.name IN ('write_memory_knowledge','write_event_from_slices')",(self.store.instance,)).fetchall()
        existing=[]
        for receipt in receipts:
            result=json.loads(receipt[0]) if receipt[0] else {}
            if result.get('status')=='created' and source_ids.intersection(result.get('source_slice_ids',[])):
                existing.append({k:result[k] for k in ('memory_id','event_id','summary','source_slice_ids') if k in result})
        return {'status':'ok','sleep_id':row['sleep_id'],'session_id':row['session_id'],'state':row['state'],
            'groups':pending,'remaining':len(pending),'tools':tool_specs(sleep=True),
            'knowledge':self.knowledge(),
            'existing_outputs':existing,
            'stale_group_ids':[g['group_id'] for g in snapshot if g['group_id'] not in current]}

    def knowledge(self):
        return {'briefing':self.store.briefing(),'items':[
            {k:r[k] for k in ('memory_id','kind','content','cues','pinned','sources','confidence')}
            for r in self.store.all()]}

    def start(self,body):
        if not isinstance(body,dict) or set(body)!={'sleep_id','session_id'} or any(
                not isinstance(body.get(k),str) or identity(body[k])!=body[k] for k in ('sleep_id','session_id')):
            return fail('invalid_sleep_knowledge_identity')
        with self.service._generation_lock:
            old=self.run(body['sleep_id'])
            if old:
                return self.response(old) if old['session_id']==body['session_id'] else fail('sleep_context_conflict')
            from ember.memory_lifecycle import Lifecycle
            from ember.review_aggregation import consolidate
            consolidate(Lifecycle(self.store.root,self.store.instance))
            groups=self.store.groups()
            # Knowledge review needs recent experiences, not only the two old
            # statements being compared. Freeze completed source IDs at Sleep start.
            from ember.turn_slices import TurnSliceStore
            slices=TurnSliceStore(str(self.store.root))
            recent=[]
            if slices.path.exists():
                with slices._db() as db:
                    ids=[r[0] for r in db.execute('SELECT slice_id FROM turn_slices WHERE instance=? ORDER BY CAST(assistant_message_id AS INTEGER) DESC LIMIT 12',(self.store.instance,))]
                recent=self.store.sources(ids) if ids else []
            for g in groups:
                if g['kind']=='knowledge_review':
                    g['material']['recent_sources']=recent
                    g['sources']=list({s['slice_id']:s for s in g['sources']+recent}.values())
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute("SELECT 1 FROM sleep_knowledge_runs WHERE instance=? AND state IN ('active','unknown')",(self.store.instance,)).fetchone():
                    return fail('sleep_knowledge_already_active')
                db.execute('INSERT INTO sleep_knowledge_runs VALUES (?,?,?,?,?)',
                    (body['sleep_id'],self.store.instance,body['session_id'],'active',json.dumps(groups,ensure_ascii=False)))
            return self.response(self.run(body['sleep_id']))

    def call(self,body):
        if (not isinstance(body,dict) or set(body)!={'sleep_id','session_id','call_id','name','arguments'} or
                any(not isinstance(body.get(k),str) or identity(body[k])!=body[k] for k in ('sleep_id','session_id','call_id','name')) or
                not isinstance(body.get('arguments'),dict)):
            return fail('invalid_sleep_tool_arguments')
        with self.service._generation_lock:
            row=self.run(body['sleep_id'])
            if row is None or row['session_id']!=body['session_id']:
                return fail('sleep_context_missing_or_conflicting')
            try:
                digest=fingerprint(body)
            except (ValueError,TypeError):
                return fail('invalid_sleep_tool_arguments')
            with self.db() as db:
                old=db.execute('SELECT * FROM sleep_knowledge_calls WHERE sleep_id=? AND call_id=?',
                    (body['sleep_id'],body['call_id'])).fetchone()
            if old:
                if old['request_hash']!=digest:
                    return fail('sleep_tool_id_conflict')
                if old['state']!='completed':
                    return fail('sleep_tool_outcome_unknown')
                if old['volatile']:
                    return copy.deepcopy(self.service._sleep_knowledge_reads.get(
                        (body['sleep_id'],body['call_id']),fail('result_not_retained')))
                return json.loads(old['result_json'])
            if row['state']!='active':
                return fail('sleep_knowledge_not_active')
            spec=next((s['function'] for s in tool_specs(sleep=True) if s['function']['name']==body['name']),None)
            if spec is None:
                return fail('sleep_tool_not_allowed')
            error=validate_tool_input(body['arguments'],spec['parameters'])
            if error:
                return error
            volatile=body['name'] in VOLATILE_TOOLS
            with self.db() as db:
                db.execute('INSERT INTO sleep_knowledge_calls VALUES (?,?,?,?,?,?,?)',
                    (body['sleep_id'],body['call_id'],digest,body['name'],'in_progress',None,int(volatile)))
            groups=json.loads(row['groups_json'])
            context={'kind':'sleep','sleep_id':row['sleep_id'],
                'session':self.service.get_or_create_session(row['session_id']),'turn':0,
                'group_ids':{g['group_id'] for g in groups},
                'snapshot_groups':{g['group_id']:g for g in groups},
                'source_ids':{s['slice_id'] for g in groups for s in g['sources']},
                'frozen_sources':{s['slice_id']:s for g in groups for s in g['sources']},
                'read_source_ids':self.service._sleep_knowledge_read_ids.get(row['sleep_id'],set())}
            try:
                if body['name']=='list_memory_groups':
                    visible=({g['group_id'] for g in groups})
                    result={'status':'ok','groups':(
                        [g for g in self.store.groups(False) if g['group_id'] in visible]
                        if body['arguments'].get('include_reviewed') else self.response(row)['groups'])}
                elif body['name']=='list_memory_knowledge':
                    result={'status':'ok',**self.knowledge()}
                else:
                    result=dispatch(self.service,body['name'],body['arguments'],context)
                with self.db() as db:
                    db.execute("UPDATE sleep_knowledge_calls SET state='completed',result_json=? WHERE sleep_id=? AND call_id=?",
                        (json.dumps(result,ensure_ascii=False) if not volatile else None,body['sleep_id'],body['call_id']))
                if volatile:
                    self.service._sleep_knowledge_reads[(body['sleep_id'],body['call_id'])]=copy.deepcopy(result)
                    while len(self.service._sleep_knowledge_reads)>256:
                        self.service._sleep_knowledge_reads.pop(next(iter(self.service._sleep_knowledge_reads)))
                    if result.get('status')=='ok':
                        self.service._sleep_knowledge_read_ids.setdefault(row['sleep_id'],set()).update(
                            r['slice_id'] for r in result.get('slices',[]))
                return result
            except Exception:
                with self.db() as db:
                    db.execute("UPDATE sleep_knowledge_calls SET state='unknown' WHERE sleep_id=? AND call_id=?",(body['sleep_id'],body['call_id']))
                    db.execute("UPDATE sleep_knowledge_runs SET state='unknown' WHERE sleep_id=?",(body['sleep_id'],))
                return fail('sleep_tool_outcome_unknown')

    def finish(self,body):
        if (not isinstance(body,dict) or set(body)!={'sleep_id','session_id','state'} or
                any(not isinstance(body.get(k),str) or identity(body[k])!=body[k] for k in ('sleep_id','session_id')) or
                body.get('state') not in ('completed','stopped')):
            return fail('invalid_sleep_finish')
        with self.service._generation_lock:
            row=self.run(body.get('sleep_id'))
            if row is None or row['session_id']!=body.get('session_id'):
                return fail('sleep_context_missing_or_conflicting')
            if row['state']!='active':
                return self.response(row)
            response=self.response(row)
            if body['state']=='completed' and response['remaining']:
                return fail('sleep_materials_not_finished')
            with self.db() as db:
                unknown=db.execute("SELECT 1 FROM sleep_knowledge_calls WHERE sleep_id=? AND state!='completed'",(body['sleep_id'],)).fetchone()
                db.execute('UPDATE sleep_knowledge_runs SET state=? WHERE sleep_id=?',
                    ('unknown' if unknown else body['state'],body['sleep_id']))
            self.service._sleep_knowledge_read_ids.pop(body['sleep_id'],None)
            from ember.memory_lifecycle import Lifecycle
            from ember.review_aggregation import consolidate
            consolidate(Lifecycle(self.store.root,self.store.instance))
            return self.response(self.run(body['sleep_id']))
