"""Orphan-only source grouping and a durable, acknowledged extraction queue."""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

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
                state TEXT DEFAULT 'pending',payload TEXT);
                CREATE TABLE IF NOT EXISTS parts(
                id TEXT PRIMARY KEY,group_id TEXT,ordinal INTEGER,payload TEXT,
                conclusion TEXT,result_ids TEXT,delivered INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,error TEXT);''')
            with conn:
                yield conn
        finally:
            conn.close()

    def candidates(self,config):
        sync_from_files(self.root,self.instance)
        owners={r['slice_id'] for r in EventSliceLinks(self.root).all(self.instance)}
        slices=TurnSliceStore(self.root)
        if not slices.path.exists():
            return []
        with slices._db() as conn:
            ids=[r[0] for r in conn.execute('SELECT slice_id FROM turn_slices WHERE instance=? ORDER BY saved_at,slice_id',(self.instance,))]
        eligible=Lifecycle(self.root,self.instance).eligibility_checker()
        result=[]
        for sid in ids:
            source=slices.get(sid)
            vector=slices.embedding(sid,config.space)
            if sid not in owners and informative(source) and eligible(sid) and vector:
                result.append(dict(source,vector=vector['vector']))
        return result

    def discover(self,config=None):
        """Vector-near candidates go to a grouping model, never a recall trace."""
        config=config or SliceEmbeddingConfig.load()
        candidates=self.candidates(config);current={s['slice_id']:s for s in candidates}
        with self.db() as conn:
            rows=conn.execute('SELECT * FROM groups WHERE instance=?',(self.instance,)).fetchall()
            occupied=set()
            for row in rows:
                sources=json.loads(row['sources'])
                valid=all(s['slice_id'] in current and current[s['slice_id']]['content_hash']==s['content_hash'] for s in sources)
                if not valid and row['state']=='pending':
                    conn.execute("UPDATE groups SET state='withdrawn' WHERE id=?",(row['id'],))
                elif valid and row['state']!='withdrawn':
                    occupied.update(s['slice_id'] for s in sources)
        pending=[s for s in candidates if s['slice_id'] not in occupied]
        if not pending:
            return False
        cfg=lifecycle_config()['extraction'];seed=pending[0]
        selected=[seed]+[s for s in pending[1:] if len(s['vector'])==len(seed['vector']) and cosine(s['vector'],seed['vector'])>=cfg['group_threshold']][:cfg['group_size']-1]
        key=fingerprint([(s['slice_id'],s['content_hash']) for s in selected])
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
                conn.execute('DELETE FROM attempts WHERE id=?',(key,))
            return True
        except Exception as exc:
            with self.db() as conn:
                conn.execute('INSERT OR REPLACE INTO attempts VALUES (?,?)',(key,type(exc).__name__))
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
        sources=json.loads(row['sources']);self.knowledge.read_sources(sources)
        return dict(row,sources=sources)

    def materialize(self,row,config,limit):
        gid=row['id']
        with self.db() as conn:
            exists=conn.execute('SELECT 1 FROM parts WHERE group_id=?',(gid,)).fetchone()
        if exists:
            return
        sources=json.loads(row['sources']);originals=self.knowledge.read_sources(sources)
        payload={'group_id':gid,'description':row['description'],
                 'slices':[{'slice_id':s['slice_id'],'messages':s['messages']} for s in originals],
                 'similar_knowledge':self.similar(sources,config)}
        pid=gid+'-1';whole=dict(payload,part_id=pid)
        parts=[]
        if len(encoded(whole))<=limit:
            parts=[whole]
        else:
            text=encoded(payload);offset=0
            while offset<len(text):
                part={'group_id':gid,'part_id':gid+'-'+str(len(parts)+1),'offset':offset,'total_chars':len(text),'material_fragment':''}
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
            for i,part in enumerate(parts):
                conn.execute('INSERT OR IGNORE INTO parts(id,group_id,ordinal,payload) VALUES (?,?,?,?)',
                             (part['part_id'],gid,i,encoded(part)))
            conn.execute('UPDATE groups SET payload=? WHERE id=?',(encoded(payload),gid))

    def batch(self):
        config=SliceEmbeddingConfig.load();limit=lifecycle_config()['extraction']['batch_chars']
        # Revalidate ownership without asking the grouping model during a tool call.
        current={s['slice_id']:s for s in self.candidates(config)}
        with self.db() as conn:
            rows=conn.execute("SELECT * FROM groups WHERE instance=? AND state='pending' ORDER BY created,rowid",(self.instance,)).fetchall()
        valid=[]
        for row in rows:
            sources=json.loads(row['sources'])
            if not all(s['slice_id'] in current and current[s['slice_id']]['content_hash']==s['content_hash'] for s in sources):
                with self.db() as conn:conn.execute("UPDATE groups SET state='withdrawn' WHERE id=?",(row['id'],))
                continue
            self.materialize(row,config,limit-2);valid.append(row)
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
            errors=conn.execute('SELECT count(*) FROM attempts').fetchone()[0]
        return {'status':'ok','groups':chosen,'material_chars':len(encoded(chosen)),
                'remaining_groups':len({u['group_id'] for u in remaining}),
                'remaining_chars':sum(len(u['payload']) for u in remaining),'grouping_errors':errors,
                'instruction':'每组或超限分段处理后调用 finish_memory_extraction_group 给出 written / merged / skipped；未确认的下次仍返回。'}

    def finish(self,args):
        if args.get('conclusion') not in ('written','merged','skipped'):
            raise ValueError('extraction_conclusion_required')
        row=self.live_group(args['group_id'])
        ids=args.get('result_ids',[])
        if args['conclusion']!='skipped' and not ids:
            raise ValueError('extraction_result_ids_required')
        known={r['memory_id'] for r in self.knowledge.all()}|set(self.service.store._events)
        if not isinstance(ids,list) or any(mid not in known for mid in ids):
            raise ValueError('extraction_result_missing')
        with self.db() as conn:
            part=conn.execute('SELECT * FROM parts WHERE id=? AND group_id=?',(args['part_id'],row['id'])).fetchone()
            if part is None or not part['delivered']:
                raise ValueError('extraction_part_not_delivered')
            if part['conclusion'] and (part['conclusion']!=args['conclusion'] or json.loads(part['result_ids'])!=ids):
                raise ValueError('extraction_conclusion_conflict')
            conn.execute('UPDATE parts SET conclusion=?,result_ids=? WHERE id=?',(args['conclusion'],encoded(ids),part['id']))
            pending=conn.execute('SELECT 1 FROM parts WHERE group_id=? AND conclusion IS NULL',(row['id'],)).fetchone()
            if not pending:conn.execute("UPDATE groups SET state='completed' WHERE id=?",(row['id'],))
        return {'status':'ok','group_id':row['id'],'part_id':part['id'],'state':'pending' if pending else 'completed','conclusion':args['conclusion']}


def tool_specs():
    return [{'type':'function','function':{'name':'get_memory_extraction_batch',
        'description':'分批读取最早的提取材料：归组说明、原文切片、相似认知/规律。自行决定新写、改写、合并或不写，可随时停；未给结论下次仍会出现。超限单组分段，material_fragment 按 offset 拼接；完整来源可按切片下钻。',
        'parameters':{'type':'object','properties':{},'additionalProperties':False}}},
        {'type':'function','function':{'name':'finish_memory_extraction_group','description':'为已交付的组/分段记录写了、并进旧的或不写的结论；written/merged 须附已存在的结果 ID。全部分段确认才完成一组。',
        'parameters':{'type':'object','additionalProperties':False,'required':['group_id','part_id','conclusion'],
            'properties':{'group_id':{'type':'string'},'part_id':{'type':'string'},'conclusion':{'enum':['written','merged','skipped']},
                          'result_ids':{'type':'array','uniqueItems':True,'items':{'type':'string'}}}}}}]


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
