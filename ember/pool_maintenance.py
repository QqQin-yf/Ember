"""Classify each newborn pool independently, using historical slices only."""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .generations import fingerprint
from .knowledge import KnowledgeStore, embed_knowledge
from .maintenance_client import MaintenanceConfig, MaintenanceClient, MaintenanceModelError
from .slice_maintenance import SliceMaintenanceWorker


class PoolQueue:
    def __init__(self, root):
        self.root=Path(root)
        self.knowledge=KnowledgeStore(root)

    @contextmanager
    def db(self):
        with self.knowledge.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS pool_jobs (
                trace_id TEXT PRIMARY KEY,instance TEXT NOT NULL,sources_json TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0,lease_until REAL NOT NULL DEFAULT 0,
                token TEXT,error_code TEXT,result_json TEXT)''')
            yield db

    def enqueue(self, trace):
        if trace.get('purpose')!='automatic':
            return
        sources=[{k:c[k] for k in ('slice_id','content_hash','session_id','user_message_id','assistant_message_id')}
                 for c in trace.get('candidate_pool',[])]
        anchor=trace.get('anchor_message_id')
        # The current input/reply is never maintenance material, including a
        # regenerated reply or an already stored future slice on this session.
        sources=[s for s in sources if not (s['session_id']==trace.get('session_id') and anchor and
                   max(int(s['assistant_message_id']),int(s['user_message_id'] or 0))>=int(anchor))]
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO pool_jobs(trace_id,instance,sources_json,state) VALUES (?,?,?,?)',
                (trace['trace_id'],trace['instance'],json.dumps(sources),'pending' if len(sources)>1 else 'done'))

    def claim(self):
        if not self.knowledge.path.exists():
            return None
        now=time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT * FROM pool_jobs WHERE (state IN ('pending','retry','waiting_config') AND retry_at<=?) OR (state='working' AND lease_until<=?) ORDER BY rowid LIMIT 1",(now,now)).fetchone()
            if row is None:
                return None
            token=uuid.uuid4().hex
            db.execute("UPDATE pool_jobs SET state='working',attempts=attempts+1,token=?,lease_until=? WHERE trace_id=?",(token,now+120,row['trace_id']))
            return dict(row,token=token,attempts=row['attempts']+1)

    def finish(self, job, state, groups=(), error=None):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute("SELECT 1 FROM pool_jobs WHERE trace_id=? AND token=? AND state='working'",(job['trace_id'],job['token'])).fetchone():
                return
            for group in groups:
                sources=group['sources']
                # Group identity is independent of trigger turn: a reviewed
                # group does not repeatedly become a new App task.
                gid='GR-'+fingerprint([job['instance'],group['kind'],sorted((s['slice_id'],s['content_hash']) for s in sources)])[:24]
                db.execute('INSERT OR IGNORE INTO memory_groups(group_id,instance,kind,sources_json,reason) VALUES (?,?,?,?,?)',
                    (gid,job['instance'],group['kind'],json.dumps(sources),group['reason']))
            delay=min(300,2**min(job['attempts'],8)) if state in ('retry','waiting_config') else 0
            db.execute('UPDATE pool_jobs SET state=?,result_json=?,error_code=?,retry_at=?,lease_until=0,token=NULL WHERE trace_id=? AND token=?',
                (state,json.dumps(list(groups),ensure_ascii=False),error,time.time()+delay,job['trace_id'],job['token']))


def validate_groups(decisions, sources, originals):
    if not isinstance(decisions,list):
        raise MaintenanceModelError('pool_groups_invalid')
    known={s['slice_id']:s for s in sources}
    messages={r['slice_id']:r['messages'] for r in originals}
    result=[]
    for group in decisions:
        if not isinstance(group,dict) or group.get('kind') not in ('same_information','pattern','same_event'):
            raise MaintenanceModelError('pool_groups_invalid')
        ids=group.get('slice_ids')
        evidence=group.get('evidence')
        if (not isinstance(ids,list) or len(ids)<2 or any(not isinstance(s,str) for s in ids) or
                len(set(ids))!=len(ids) or not set(ids)<=set(known) or not isinstance(evidence,list) or
                len(evidence)!=len(ids) or any(not isinstance(e,dict) or not isinstance(e.get('slice_id'),str) for e in evidence) or
                {e['slice_id'] for e in evidence}!=set(ids) or not isinstance(group.get('reason'),str)):
            raise MaintenanceModelError('pool_groups_invalid')
        for entry in evidence:
            quote=entry.get('quote')
            if not isinstance(quote,str) or not quote or not any(quote in m['content'] for m in messages[entry['slice_id']]):
                raise MaintenanceModelError('pool_evidence_invalid')
        result.append({'kind':group['kind'],'sources':[known[s] for s in ids],
                       'reason':group['reason'],'evidence':evidence})
    return result


class PoolMaintenanceWorker:
    def __init__(self, registry):
        self.registry=registry
        self.config_path=SliceMaintenanceWorker(registry).config_path
        self.stop_event=asyncio.Event()
        self.last_discover=0

    def discover(self):
        for name in self.registry.names:
            root=Path(self.registry.dir_of(name))
            path=root/'slice_recall.sqlite3'
            if not path.exists():
                continue
            db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
            try:
                traces=[json.loads(r[0]) for r in db.execute('SELECT metadata_json FROM queries WHERE instance=?',(name,))]
            finally:
                db.close()
            for trace in traces:
                PoolQueue(root).enqueue(trace)

    def process_one(self):
        if time.time()-self.last_discover>5:
            self.discover();self.last_discover=time.time()
        for name in self.registry.names:
            root=self.registry.dir_of(name)
            queue=PoolQueue(root)
            job=queue.claim()
            if job is None:
                continue
            try:
                config=MaintenanceConfig.load(self.config_path)
                sources=json.loads(job['sources_json'])
                store=KnowledgeStore(root,job['instance'])
                originals=store.read_sources(sources)
                from .memory_maintenance import validate_result, apply_result
                trace={}
                trace_path=Path(root)/'slice_recall.sqlite3'
                if trace_path.exists():
                    with sqlite3.connect(trace_path) as db:
                        record=db.execute('SELECT metadata_json FROM queries WHERE trace_id=?',(job['trace_id'],)).fetchone()
                    trace=json.loads(record[0]) if record else {}
                def historical(g):
                    return not any(s['session_id']==trace.get('session_id') and trace.get('anchor_message_id') and
                        int(s['assistant_message_id'])>=int(trace['anchor_message_id']) for s in g['sources'])
                service=self.registry.service(name)
                for offset in range(0,len(originals),config.batch_size):
                    batch=originals[offset:offset+config.batch_size]
                    payload={'phase':'memory_pool','subject':'recall:'+job['trace_id'],
                        'materials':[{'id':r['slice_id'],'kind':'slice','document':{'messages':r['messages']},
                           'sources':[next(s for s in sources if s['slice_id']==r['slice_id'])]} for r in batch],
                        'known_groups':[g for g in store.groups(False) if historical(g)]}
                    try:
                        decisions=validate_result(MaintenanceClient(config).judge('memory_pool',payload),payload)
                    except ValueError as exc:
                        raise MaintenanceModelError(str(exc)) from None
                    with service._generation_lock:
                        store.read_sources(sources)
                        apply_result(service,payload,decisions)
                queue.finish(job,'done',[])
            except MaintenanceModelError as exc:
                queue.finish(job,'waiting_config' if str(exc)=='maintenance_config_missing' else 'retry',error=str(exc))
            except ValueError:
                queue.finish(job,'stale',error='pool_source_changed_or_invalid')
            except (OSError,sqlite3.Error,TypeError,KeyError):
                queue.finish(job,'retry',error='pool_maintenance_unavailable')
            return True
        return False

    async def run(self):
        while not self.stop_event.is_set():
            try:
                processed=await asyncio.to_thread(self.process_one)
            except Exception:
                processed=False
            if not processed:
                try:
                    await asyncio.wait_for(self.stop_event.wait(),timeout=1)
                except asyncio.TimeoutError:
                    pass


class KnowledgeEmbeddingWorker:
    def __init__(self, registry):
        self.registry=registry
        self.stop_event=asyncio.Event()

    def process_one(self):
        from .slice_embedding import SliceEmbeddingConfig
        import os
        path=os.environ.get('EMBER_SLICE_EMBED_CONFIG')
        for name in self.registry.names:
            root=self.registry.dir_of(name)
            if KnowledgeStore(root,name).all():
                if embed_knowledge(root,name,SliceEmbeddingConfig.load(path)):
                    return True
        return False

    async def run(self):
        while not self.stop_event.is_set():
            try:
                processed=await asyncio.to_thread(self.process_one)
            except Exception:
                processed=False
            if not processed:
                try:
                    await asyncio.wait_for(self.stop_event.wait(),timeout=5)
                except asyncio.TimeoutError:
                    pass
