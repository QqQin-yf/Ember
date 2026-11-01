"""Orphan-only source grouping and a durable, acknowledged extraction queue."""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time

from .core_semantic import cosine
from .event_slices import EventSliceLinks, sync_from_files
from .generations import fingerprint
from .knowledge import KnowledgeStore, knowledge_space
from .lifecycle_config import lifecycle_config
from .maintenance_models import ask
from .memory_lifecycle import Lifecycle
from .slice_embedding import SliceEmbeddingConfig, SliceEmbeddingError
from .slice_policy import informative
from .turn_slices import TurnSliceStore


def encoded(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':'))


class Extraction:
    def __init__(self,service):
        self.service=service;self.root=service.store.root;self.instance=service.source_instance
        self.knowledge=KnowledgeStore(self.root,self.instance)

    @contextmanager
    def db(self):
        conn=sqlite3.connect(Path(self.root)/'extraction.sqlite3',timeout=20)
        conn.row_factory=sqlite3.Row
        try:
            conn.executescript('''CREATE TABLE IF NOT EXISTS groups(
                id TEXT PRIMARY KEY,instance TEXT,sources TEXT,description TEXT,created TEXT,
                state TEXT DEFAULT 'pending',payload TEXT,material_hash TEXT);
                CREATE TABLE IF NOT EXISTS parts(
                id TEXT PRIMARY KEY,group_id TEXT,ordinal INTEGER,payload TEXT,
                conclusion TEXT,result_ids TEXT,delivered INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS grouping_retries(
                instance TEXT,slice_id TEXT,content_hash TEXT,space TEXT,
                failures INTEGER,error TEXT,retry_at REAL,report_after INTEGER,
                PRIMARY KEY(instance,slice_id,content_hash,space));
                CREATE TABLE IF NOT EXISTS source_results(
                instance TEXT,slice_id TEXT,content_hash TEXT,result_id TEXT,action TEXT,
                PRIMARY KEY(instance,slice_id,content_hash,result_id));''')
            if 'material_hash' not in {r[1] for r in conn.execute('PRAGMA table_info(groups)')}:
                conn.execute('ALTER TABLE groups ADD COLUMN material_hash TEXT')
            with conn:
                yield conn
        finally:
            conn.close()

    def record_sources(self,sources,result_id='',action='skipped'):
        """A source version is consumed by its durable authoring result, not an ack."""
        with self.db() as conn:
            conn.executemany('INSERT OR IGNORE INTO source_results VALUES (?,?,?,?,?)',
                [(self.instance,s['slice_id'],s['content_hash'],result_id,action) for s in sources])
            conn.executemany('DELETE FROM grouping_retries WHERE instance=? AND slice_id=? AND content_hash=?',
                [(self.instance,s['slice_id'],s['content_hash']) for s in sources])
            done=self.completed_sources(conn)
            for row in conn.execute('SELECT * FROM groups WHERE instance=?',(self.instance,)).fetchall():
                refs=json.loads(row['sources'])
                if all((s['slice_id'],s['content_hash']) in done for s in refs):
                    conn.execute("UPDATE groups SET state='completed' WHERE id=?",(row['id'],))

    def completed_sources(self,conn):
        return {(r[0],r[1]) for r in conn.execute(
            'SELECT slice_id,content_hash FROM source_results WHERE instance=?',(self.instance,))}

    def recover_outputs(self):
        # Recover a crash or discover() racing between the durable write and queue update.
        for row in self.knowledge.all():
            self.record_sources(row['sources'],row['memory_id'],'written')
        if self.knowledge.path.exists():
            with self.knowledge.db() as conn:
                rows=conn.execute('SELECT memory_id,sources_json FROM slice_memory_sources WHERE instance=?',
                                  (self.instance,)).fetchall()
            for row in rows:
                if row['memory_id'] in self.service.store._events:
                    self.record_sources(json.loads(row['sources_json']),row['memory_id'],'written')

    def reconcile(self,current):
        """Remove only consumed/unavailable slices, retaining the rest in their group."""
        result=[]
        with self.db() as conn:
            done=self.completed_sources(conn)
            rows=conn.execute('SELECT * FROM groups WHERE instance=? ORDER BY created,rowid',(self.instance,)).fetchall()
            for row in rows:
                unprocessed=[s for s in json.loads(row['sources']) if (s['slice_id'],s['content_hash']) not in done]
                remaining=[s for s in unprocessed if s['slice_id'] in current and current[s['slice_id']]['content_hash']==s['content_hash']]
                state='completed' if not unprocessed else 'pending' if remaining else 'withdrawn'
                conn.execute('UPDATE groups SET state=? WHERE id=?',(state,row['id']))
                if remaining:
                    result.append(dict(row,state=state,remaining_sources=remaining))
        return result

    def candidates(self,config):
        sync_from_files(self.root,self.instance)
        owners={r['slice_id'] for r in EventSliceLinks(self.root).all(self.instance)}
        slices=TurnSliceStore(self.root)
        if not slices.path.exists():
            return []
        with slices._db() as conn:
            ids=[r[0] for r in conn.execute('SELECT slice_id FROM turn_slices WHERE instance=? ORDER BY saved_at,slice_id',(self.instance,))]
        eligible=Lifecycle(self.root,self.instance).eligibility_checker()
        with self.db() as conn:
            done=self.completed_sources(conn)
        result=[]
        for sid in ids:
            source=slices.get(sid)
            vector=slices.embedding(sid,config.space)
            if sid not in owners and (sid,source['content_hash']) not in done and informative(source) and eligible(sid) and vector:
                result.append(dict(source,vector=vector['vector']))
        return result

    def discover(self,config=None):
        """Vector-near candidates go to a grouping model, never a recall trace."""
        config=config or SliceEmbeddingConfig.load()
        self.recover_outputs()
        candidates=self.candidates(config);current={s['slice_id']:s for s in candidates}
        rows=self.reconcile(current)
        occupied={s['slice_id'] for row in rows for s in row['remaining_sources']}
        pending=[s for s in candidates if s['slice_id'] not in occupied]
        cfg=lifecycle_config()['extraction']
        now=time.time()
        with self.db() as conn:
            retries={}
            for row in conn.execute('SELECT * FROM grouping_retries WHERE instance=?',(self.instance,)).fetchall():
                source=current.get(row['slice_id'])
                if source is None or source['content_hash']!=row['content_hash'] or row['space']!=config.space:
                    conn.execute('DELETE FROM grouping_retries WHERE instance=? AND slice_id=? AND content_hash=? AND space=?',
                                 (self.instance,row['slice_id'],row['content_hash'],row['space']))
                else:
                    retries[row['slice_id']]=row['retry_at']
        ready=[s for s in pending if retries.get(s['slice_id'],0)<=now]
        if not ready:
            return False
        seed=ready[0]
        selected=[seed]+[s for s in ready[1:] if len(s['vector'])==len(seed['vector']) and cosine(s['vector'],seed['vector'])>=cfg['group_threshold']][:cfg['group_size']-1]
        try:
            result=ask('slice_groups','只把没有上级且有信息量的原文按同一件事、规律、习惯、喜恶或主题归组。每个 slice_id 恰好出现一次；只给归组说明，不提炼认知、不写 Event。',
                {'slices':[{'slice_id':s['slice_id'],'messages':s['messages']} for s in selected]},
                {'type':'object','required':['groups'],'properties':{'groups':{'type':'array','minItems':1,'items':{
                    'type':'object','required':['description','slice_ids'],'properties':{'description':{'type':'string','minLength':1},
                    'slice_ids':{'type':'array','minItems':1,'uniqueItems':True,'items':{'type':'string'}}}}}}})
            covered=[sid for g in result['groups'] for sid in g['slice_ids']]
            if len(covered)!=len(set(covered)) or set(covered)!={s['slice_id'] for s in selected}:
                raise ValueError('extraction_group_coverage_invalid')
            latest={s['slice_id']:s for s in self.candidates(config)}
            if any(s['slice_id'] not in latest or latest[s['slice_id']]['content_hash']!=s['content_hash'] for s in selected):
                raise ValueError('extraction_sources_changed')
            byid={s['slice_id']:s for s in selected}
            with self.db() as conn:
                for group in result['groups']:
                    sources=self.knowledge.sources(group['slice_ids'])
                    gid='EX-'+fingerprint([self.instance,sorted((s['slice_id'],s['content_hash']) for s in sources)])[:24]
                    conn.execute('INSERT OR IGNORE INTO groups(id,instance,sources,description,created) VALUES (?,?,?,?,?)',
                        (gid,self.instance,encoded(sources),group['description'],min(byid[sid]['saved_at'] for sid in group['slice_ids'])))
                conn.executemany('DELETE FROM grouping_retries WHERE instance=? AND slice_id=? AND content_hash=? AND space=?',
                                 [(self.instance,s['slice_id'],s['content_hash'],config.space) for s in selected])
            return True
        except Exception as exc:
            with self.db() as conn:
                conn.execute('BEGIN IMMEDIATE')
                identity=(self.instance,seed['slice_id'],seed['content_hash'],config.space)
                previous=conn.execute('SELECT failures FROM grouping_retries WHERE instance=? AND slice_id=? AND content_hash=? AND space=?',identity).fetchone()
                failures=1+(previous[0] if previous else 0)
                delay=min(cfg['retry_max_seconds'],cfg['retry_initial_seconds']*2**min(failures-1,32))
                conn.execute('INSERT OR REPLACE INTO grouping_retries VALUES (?,?,?,?,?,?,?,?)',
                    (*identity,failures,type(exc).__name__,time.time()+delay,cfg['failure_warning_count']))
            raise

    def similar(self,sources,config):
        cfg=lifecycle_config()['extraction'];store=TurnSliceStore(self.root)
        vectors=[store.embedding(s['slice_id'],config.space) for s in sources]
        texts='\n'.join(m['content'] for s in self.knowledge.read_sources(sources) for m in s['messages'])
        result=[]
        for row in self.knowledge.all():
            if not Lifecycle(self.root,self.instance).eligible(row['memory_id']):
                continue
            scores=[cosine(v['vector'],row['vector']) for v in vectors if v and row['vector'] and
                    row['space']==knowledge_space(config) and row['vector_hash']==row['content_hash'] and len(v['vector'])==len(row['vector'])]
            score=max(scores,default=0)
            if any(c.casefold() in texts.casefold() for c in row['cues']) or score>=cfg['knowledge_threshold']:
                result.append({'memory_id':row['memory_id'],'kind':row['kind'],'content':row['content'],
                               'confidence':row['confidence'],'score':score})
        return sorted(result,key=lambda r:(-r['score'],r['memory_id']))[:cfg['similar_limit']]

    def live_group(self,gid):
        with self.db() as conn:
            row=conn.execute('SELECT * FROM groups WHERE id=? AND instance=?',(gid,self.instance)).fetchone()
        if row is None or row['state']=='withdrawn':
            raise ValueError('extraction_group_missing_or_withdrawn')
        sources=json.loads(row['sources'])
        if row['payload']:
            ids={s['slice_id'] for s in json.loads(row['payload'])['slices']}
            sources=[s for s in sources if s['slice_id'] in ids]
        self.knowledge.read_sources(sources)
        return dict(row,sources=sources)

    def materialize(self,row,config,limit):
        gid=row['id'];sources=row['remaining_sources'];digest=fingerprint(sources)
        with self.db() as conn:
            saved=conn.execute('SELECT material_hash FROM groups WHERE id=?',(gid,)).fetchone()
            exists=conn.execute('SELECT 1 FROM parts WHERE group_id=?',(gid,)).fetchone()
        if exists and saved['material_hash']==digest:
            return
        originals=self.knowledge.read_sources(sources)
        payload={'group_id':gid,'description':row['description'],
                 'slices':[{'slice_id':s['slice_id'],'messages':s['messages']} for s in originals],
                 'similar_knowledge':self.similar(sources,config)}
        prefix=gid+'-'+digest[:8]
        pid=prefix+'-1';whole=dict(payload,part_id=pid)
        parts=[]
        if len(encoded(whole))<=limit:
            parts=[whole]
        else:
            text=encoded(payload);offset=0
            while offset<len(text):
                part={'group_id':gid,'part_id':prefix+'-'+str(len(parts)+1),'offset':offset,'total_chars':len(text),'material_fragment':''}
                # Binary search counts JSON escaping and metadata against the budget.
                lo,hi=0,len(text)-offset
                while lo<hi:
                    mid=(lo+hi+1)//2;part['material_fragment']=text[offset:offset+mid]
                    if len(encoded(part))<=limit:lo=mid
                    else:hi=mid-1
                if not lo:
                    raise ValueError('extraction_batch_chars_too_small')
                part['material_fragment']=text[offset:offset+lo];parts.append(part);offset+=lo
        with self.db() as conn:
            conn.execute('DELETE FROM parts WHERE group_id=?',(gid,))
            for i,part in enumerate(parts):
                conn.execute('INSERT OR IGNORE INTO parts(id,group_id,ordinal,payload) VALUES (?,?,?,?)',
                             (part['part_id'],gid,i,encoded(part)))
            conn.execute('UPDATE groups SET payload=?,material_hash=? WHERE id=?',(encoded(payload),digest,gid))

    def batch(self):
        config=SliceEmbeddingConfig.load();limit=lifecycle_config()['extraction']['batch_chars']
        self.recover_outputs()
        current={s['slice_id']:s for s in self.candidates(config)}
        valid=self.reconcile(current)
        for row in valid:
            self.materialize(row,config,limit-2)
        units=[]
        with self.db() as conn:
            for row in valid:
                units.extend(dict(r) for r in conn.execute('SELECT * FROM parts WHERE group_id=? AND conclusion IS NULL ORDER BY ordinal',(row['id'],)))
        chosen=[]
        # Count the list delimiters and separators as well as each material.
        for unit in units:
            payload=json.loads(unit['payload'])
            if len(encoded(chosen+[payload]))>limit:
                break
            chosen.append(payload)
        chosen_ids={p['part_id'] for p in chosen}
        remaining=[u for u in units if u['id'] not in chosen_ids]
        with self.db() as conn:
            conn.executemany('UPDATE parts SET delivered=1 WHERE id=?',[(pid,) for pid in chosen_ids])
            errors=conn.execute('SELECT count(*) FROM grouping_retries WHERE instance=?',(self.instance,)).fetchone()[0]
        return {'status':'ok','groups':chosen,'material_chars':len(encoded(chosen)),
                'remaining_groups':len({u['group_id'] for u in remaining}),
                'remaining_chars':sum(len(u['payload']) for u in remaining),'grouping_errors':errors,
                'instruction':'写进 Event、约定、认知/规律或改写/合并知识时，来源切片会自动结掉；只有决定不写时调用 finish_memory_extraction_group，可指定 source_slice_ids 或超限 part_id。剩余切片下次继续返回。'}

    def finish(self,args):
        if set(args)-{'group_id','source_slice_ids','part_id'}:
            raise ValueError('finish_only_skips_sources')
        if args.get('source_slice_ids') is not None and args.get('part_id') is not None:
            raise ValueError('choose_source_slice_ids_or_part_id')
        gid=args['group_id']
        with self.db() as conn:
            row=conn.execute('SELECT * FROM groups WHERE id=? AND instance=?',(gid,self.instance)).fetchone()
        if row is None:
            raise ValueError('extraction_group_missing')
        if row['state']!='pending':
            return {'status':'ok','group_id':gid,'state':row['state']}
        sources=json.loads(row['sources'])
        ids=args.get('source_slice_ids')
        if ids is not None:
            if (not isinstance(ids,list) or not ids or len(ids)!=len(set(ids)) or
                    not set(ids)<={s['slice_id'] for s in sources}):
                raise ValueError('extraction_sources_outside_group')
            sources=[s for s in sources if s['slice_id'] in ids]
        elif row['payload']:
            material_ids={s['slice_id'] for s in json.loads(row['payload'])['slices']}
            sources=[s for s in sources if s['slice_id'] in material_ids]
        with self.db() as conn:
            done=self.completed_sources(conn)
        sources=[s for s in sources if (s['slice_id'],s['content_hash']) not in done]
        if args.get('part_id'):
            with self.db() as conn:
                part=conn.execute('SELECT * FROM parts WHERE id=? AND group_id=?',(args['part_id'],gid)).fetchone()
                if part is None or not part['delivered']:
                    raise ValueError('extraction_part_not_delivered')
                conn.execute("UPDATE parts SET conclusion='skipped' WHERE id=?",(part['id'],))
                pending=conn.execute('SELECT 1 FROM parts WHERE group_id=? AND conclusion IS NULL',(gid,)).fetchone()
            if pending:
                return {'status':'ok','group_id':gid,'part_id':part['id'],'state':'pending'}
            material=json.loads(row['payload'])
            used={s['slice_id'] for s in material['slices']}
            sources=[s for s in sources if s['slice_id'] in used]
        self.knowledge.read_sources(sources)
        self.record_sources(sources)
        with self.db() as conn:
            state=conn.execute('SELECT state FROM groups WHERE id=?',(gid,)).fetchone()[0]
        return {'status':'ok','group_id':gid,'state':state}


def tool_specs():
    return [{'type':'function','function':{'name':'get_memory_extraction_batch',
        'description':'分批读取最早的提取材料：归组说明、原文切片、相似认知/规律。写进记忆的来源切片自动结掉，只有决定不写才调用 finish_memory_extraction_group；剩余切片会继续返回，可随时停。超限单组分段，material_fragment 按 offset 拼接；完整来源可按切片下钻。',
        'parameters':{'type':'object','properties':{},'additionalProperties':False}}},
        {'type':'function','function':{'name':'finish_memory_extraction_group','description':'只用于决定不写的提取材料。group_id 单独使用表示整组不写；可用 source_slice_ids 指定几段切片，或 part_id 跳过已交付的超限分段。写入、改写或合并记忆会自动结账，无需再调用。已完成或已撤出的组返回 ok。',
        'parameters':{'type':'object','additionalProperties':False,'required':['group_id'],
            'properties':{'group_id':{'type':'string'},'part_id':{'type':'string'},
                          'source_slice_ids':{'type':'array','minItems':1,'maxItems':100,'uniqueItems':True,'items':{'type':'string'}}},
            'not':{'required':['part_id','source_slice_ids']}}}}]


TOOL_NAMES={'get_memory_extraction_batch','finish_memory_extraction_group'}


def dispatch(service,name,args):
    try:
        extraction=Extraction(service)
        return extraction.batch() if name=='get_memory_extraction_batch' else extraction.finish(args)
    except (ValueError,KeyError,TypeError,SliceEmbeddingError) as exc:
        return {'status':'failed','code':'invalid_extraction_material','error':str(exc)}


class ExtractionWorker:
    def __init__(self,registry):
        self.registry=registry;self.stop_event=asyncio.Event()

    async def run(self):
        while not self.stop_event.is_set():
            for name in self.registry.names:
                try:
                    await asyncio.to_thread(Extraction(self.registry.service(name)).discover)
                except Exception:
                    pass  # Unacknowledged sources remain candidates for retry.
            try:
                await asyncio.wait_for(self.stop_event.wait(),timeout=lifecycle_config()['extraction']['worker_seconds'])
            except asyncio.TimeoutError:
                pass
