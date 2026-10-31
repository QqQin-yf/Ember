"""App-authored knowledge and exact, possibly disjoint slice provenance."""
from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3

from .generations import fingerprint
from .turn_slices import TurnSliceStore

KNOWLEDGE_THRESHOLD = 0.5
KNOWLEDGE_BUDGET = 1000


def token_bound(text):
    """Conservative UTF-8 byte-token upper bound; no App tokenizer assumed."""
    return len(text.encode('utf-8'))


def knowledge_space(config):
    return fingerprint(['knowledge-content-v1', config.space])


class KnowledgeStore:
    def __init__(self, root, instance='default'):
        self.root, self.instance = Path(root), instance
        self.path = self.root / 'knowledge.sqlite3'

    @contextmanager
    def db(self):
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.executescript('''CREATE TABLE IF NOT EXISTS knowledge (
                memory_id TEXT PRIMARY KEY,instance TEXT NOT NULL,kind TEXT NOT NULL,
                content TEXT NOT NULL,cues_json TEXT NOT NULL,sources_json TEXT NOT NULL,
                pinned INTEGER NOT NULL DEFAULT 0,content_hash TEXT NOT NULL,
                vector_json TEXT,space TEXT,vector_hash TEXT);
                CREATE TABLE IF NOT EXISTS slice_memory_sources (
                memory_id TEXT PRIMARY KEY,instance TEXT NOT NULL,sources_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS memory_groups (
                group_id TEXT PRIMARY KEY,instance TEXT NOT NULL,kind TEXT NOT NULL,
                sources_json TEXT NOT NULL,reason TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending');''')
            if 'confidence' not in {r[1] for r in db.execute('PRAGMA table_info(knowledge)')}:
                db.execute('ALTER TABLE knowledge ADD COLUMN confidence INTEGER NOT NULL DEFAULT 8')
            with db:
                yield db
        finally:
            db.close()

    def sources(self, slice_ids):
        if (not isinstance(slice_ids, list) or not slice_ids or len(slice_ids)>100 or
                any(not isinstance(s, str) or not s for s in slice_ids) or len(set(slice_ids))!=len(slice_ids)):
            raise ValueError('source_slice_ids must contain 1..100 distinct slice IDs')
        result = []
        store = TurnSliceStore(str(self.root))
        for sid in slice_ids:
            row = store.get(sid)
            if row is None or row['instance'] != self.instance:
                raise ValueError('source_slice_missing')
            result.append({k:row[k] for k in ('slice_id','content_hash','session_id','user_message_id','assistant_message_id')})
        return result

    def read_sources(self, sources):
        store = TurnSliceStore(str(self.root))
        result = []
        for source in sources:
            row = store.get(source['slice_id'])
            if row is None or row['instance']!=self.instance or row['content_hash']!=source['content_hash']:
                raise ValueError('source_slice_changed_or_missing')
            result.append(row)
        return result

    @staticmethod
    def line(item):
        return f"[{item['memory_id']} {item['kind']} 置信度{item.get('confidence',8)}/10] {item['content']}"

    def all(self):
        if not self.path.exists():
            return []
        with self.db() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM knowledge WHERE instance=? ORDER BY rowid',(self.instance,))]
        for row in rows:
            row['cues'] = json.loads(row.pop('cues_json'))
            row['sources'] = json.loads(row.pop('sources_json'))
            row['vector'] = json.loads(row.pop('vector_json')) if row['vector_json'] else None
            row['pinned'] = bool(row['pinned'])
        return rows

    def create(self, kind, content, cues, source_ids):
        if kind not in ('cognition','pattern') or not isinstance(content,str) or not content.strip():
            raise ValueError('kind and nonempty content required')
        if not isinstance(cues,list) or not cues or any(not isinstance(c,str) or not c.strip() for c in cues):
            raise ValueError('At least one nonempty recall cue required')
        sources = self.sources(source_ids)
        digest = fingerprint([kind,content.strip(),cues,sources])
        mid = 'KN-' + fingerprint([self.instance,digest])[:24]
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO knowledge(memory_id,instance,kind,content,cues_json,sources_json,content_hash) VALUES (?,?,?,?,?,?,?)',
                (mid,self.instance,kind,content.strip(),json.dumps(cues,ensure_ascii=False),json.dumps(sources),digest))
        pin = self.pin(mid, True)
        return {'status':'created','memory_id':mid,'pin':pin,'source_slice_ids':source_ids,
                'embedding_status':'pending' if not next(r for r in self.all() if r['memory_id']==mid)['vector'] else 'ready'}

    def pin(self, mid, pinned):
        if type(pinned) is not bool:
            raise ValueError('pinned must be a boolean')
        from .memory_lifecycle import Lifecycle
        if pinned and not Lifecycle(self.root,self.instance).eligible(mid):
            raise ValueError('memory_not_recallable')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = [dict(r) for r in db.execute('SELECT * FROM knowledge WHERE instance=?',(self.instance,))]
            target = next((r for r in rows if r['memory_id']==mid),None)
            if target is None:
                raise ValueError('knowledge_not_found')
            chosen = [r for r in rows if (r['memory_id']==mid and pinned) or (r['memory_id']!=mid and r['pinned'])]
            text = '\n'.join(self.line(r) for r in chosen)
            used = token_bound(text)
            if pinned and used>KNOWLEDGE_BUDGET:
                return {'status':'budget_review_required','budget_tokens':KNOWLEDGE_BUDGET,
                    'token_upper_bound':used,'count_method':'utf8_bytes_upper_bound',
                    'pinned':bool(target['pinned']),'items':[
                        {'memory_id':r['memory_id'],'content':r['content'],'pinned':bool(r['pinned'])} for r in rows],
                    'hint':'由 App AI 斟酌重要程度，先 unpin 再 pin；后台不会自动移除任何条目。'}
            db.execute('UPDATE knowledge SET pinned=? WHERE memory_id=? AND instance=?',(int(pinned),mid,self.instance))
        return {'status':'ok','memory_id':mid,'pinned':pinned,'budget_tokens':KNOWLEDGE_BUDGET,
                'token_upper_bound':used,'count_method':'utf8_bytes_upper_bound'}

    def briefing(self):
        from .memory_lifecycle import Lifecycle
        chosen = [r for r in self.all() if r['pinned'] and Lifecycle(self.root,self.instance).eligible(r['memory_id'])]
        text = '\n'.join(self.line(r) for r in chosen)
        return {'text':text,'memory_ids':[r['memory_id'] for r in chosen], 'budget_tokens':KNOWLEDGE_BUDGET,
                'token_upper_bound':token_bound(text),'count_method':'utf8_bytes_upper_bound'}

    def event_sources(self, eid):
        if not self.path.exists():
            return None
        with self.db() as db:
            row=db.execute('SELECT sources_json FROM slice_memory_sources WHERE instance=? AND memory_id=?',(self.instance,eid)).fetchone()
        return json.loads(row[0]) if row else None

    def save_event_sources(self, eid, sources):
        with self.db() as db:
            db.execute('INSERT INTO slice_memory_sources VALUES (?,?,?)',(eid,self.instance,json.dumps(sources)))

    def groups(self, pending=True):
        if not self.path.exists():
            return []
        with self.db() as db:
            rows=[dict(r) for r in db.execute('SELECT * FROM memory_groups WHERE instance=? ORDER BY rowid',(self.instance,))]
        result=[]
        for row in rows:
            if row['state']=='merged':
                continue
            if pending and row['state']!='pending':
                continue
            row['sources']=json.loads(row.pop('sources_json'))
            from .memory_lifecycle import Lifecycle
            row['material']=Lifecycle(self.root,self.instance).material(row['group_id'])
            try:
                self.read_sources(row['sources'])
            except ValueError:
                continue
            result.append(row)
        return result

    def finish_group(self, gid, state):
        if state not in ('reviewed','dismissed'):
            raise ValueError('invalid_group_state')
        with self.db() as db:
            changed=db.execute('UPDATE memory_groups SET state=? WHERE instance=? AND group_id=?',(state,self.instance,gid)).rowcount
        if not changed:
            raise ValueError('group_not_found')
        from .memory_lifecycle import Lifecycle
        life=Lifecycle(self.root,self.instance)
        material=life.material(gid)
        if material.get('memory_ids'):
            material['reviewed_versions']={r['memory_id']:r['content_hash'] for r in self.all()
                if r['memory_id'] in material['memory_ids']}
            with life.db() as db:
                db.execute('INSERT OR REPLACE INTO review_materials VALUES (?,?)',(gid,json.dumps(material)))
        return {'status':'ok','group_id':gid,'state':state}

    def match(self, text, vector=None, space=None):
        from .memory_lifecycle import Lifecycle
        life=Lifecycle(self.root,self.instance)
        eligible=life.eligibility_checker()
        hits=[]
        for row in self.all():
            if row['pinned'] or not eligible(row['memory_id']):
                continue
            cue=any(c.casefold() in text.casefold() for c in row['cues'])
            score=None
            v=row['vector']
            if vector and v and row['space']==space and row['vector_hash']==row['content_hash'] and len(v)==len(vector):
                left,right=math.hypot(*vector),math.hypot(*v)
                if left and right:
                    score=sum((a/left)*(b/right) for a,b in zip(vector,v))
            if cue or (score is not None and score>=KNOWLEDGE_THRESHOLD):
                hits.append({**row,'cue_hit':cue,'score':score})
        cue_hits=sorted([h for h in hits if h['cue_hit']],key=lambda h:h['memory_id'])
        semantic=sorted([h for h in hits if h['score'] is not None and h['score']>=KNOWLEDGE_THRESHOLD],
                        key=lambda h:(-h['score'],h['memory_id']))
        cue_ids={r['memory_id'] for r in cue_hits}
        # Choose the one best vector match before deduplication with cue hits.
        return cue_hits+[r for r in semantic[:1] if r['memory_id'] not in cue_ids]


def embed_knowledge(root, instance, config):
    from .slice_embedding import SliceEmbeddingClient
    store=KnowledgeStore(root,instance)
    for row in store.all():
        space=knowledge_space(config)
        if row['vector'] and row['space']==space and row['vector_hash']==row['content_hash']:
            continue
        vector=SliceEmbeddingClient(config).embed(row['content'])
        with store.db() as db:
            db.execute('UPDATE knowledge SET vector_json=?,space=?,vector_hash=? WHERE memory_id=? AND content_hash=?',
                (json.dumps(vector),space,row['content_hash'],row['memory_id'],row['content_hash']))
        return True
    return False
