"""Shared recall eligibility, durable review materials and temporal relationships."""
from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
from .generations import fingerprint


class Lifecycle:
    def __init__(self, root, instance='default'):
        self.root, self.instance = Path(root), instance

    @contextmanager
    def db(self):
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.root/'memory_lifecycle.sqlite3', timeout=20)
        db.row_factory = sqlite3.Row
        db.executescript('''
        CREATE TABLE IF NOT EXISTS eligibility(id TEXT PRIMARY KEY,state TEXT,reason TEXT,evidence TEXT);
        CREATE TABLE IF NOT EXISTS aliases(id TEXT PRIMARY KEY,target TEXT);
        CREATE TABLE IF NOT EXISTS relations(id TEXT PRIMARY KEY,body TEXT);
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,body TEXT,state TEXT DEFAULT 'pending',attempts INTEGER DEFAULT 0,error TEXT,retry_at REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS holds(session TEXT PRIMARY KEY,body TEXT);
        CREATE TABLE IF NOT EXISTS hold_receipts(session TEXT,turn INTEGER,body TEXT,PRIMARY KEY(session,turn));
        CREATE TABLE IF NOT EXISTS review_materials(id TEXT PRIMARY KEY,body TEXT);
        CREATE TABLE IF NOT EXISTS vectors(id TEXT PRIMARY KEY,hash TEXT,space TEXT,vector TEXT);
        ''')
        try:
            with db:
                yield db
        finally:
            db.close()

    def canonical(self, mid):
        with self.db() as db:
            seen=set()
            while mid not in seen:
                seen.add(mid)
                row=db.execute('SELECT target FROM aliases WHERE id=?',(mid,)).fetchone()
                if not row:
                    return mid
                mid=row[0]
        raise ValueError('memory_alias_cycle')

    def eligible(self, mid):
        with self.db() as db:
            row=db.execute('SELECT state FROM eligibility WHERE id=?',(mid,)).fetchone()
        return (not row or row[0]=='active') and self.canonical(mid)==mid

    def eligibility_checker(self):
        """One local snapshot per recall, not two SQLite opens per candidate."""
        with self.db() as db:
            blocked={r[0] for r in db.execute("SELECT id FROM eligibility WHERE state!='active'")}
            blocked.update(r[0] for r in db.execute('SELECT id FROM aliases'))
        return lambda mid: mid not in blocked

    def mark(self, mid, state, reason, evidence):
        if state not in ('active','no_recall','superseded') or not reason or not evidence:
            raise ValueError('eligibility_requires_reason_and_evidence')
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO eligibility VALUES (?,?,?,?)',
                       (mid,state,reason,json.dumps(evidence,ensure_ascii=False)))

    def material(self, gid):
        with self.db() as db:
            row=db.execute('SELECT body FROM review_materials WHERE id=?',(gid,)).fetchone()
        return json.loads(row[0]) if row else {}

    def propose(self, kind, sources, reason, material):
        from .knowledge import KnowledgeStore
        sources=sorted({s['slice_id']:s for s in sources}.values(),key=lambda s:s['slice_id'])
        versions=sorted((r['id'],r['document'].get('content_hash')) for r in material.get('items',[])) if kind=='knowledge_review' else []
        gid='GR-'+fingerprint([self.instance,kind,sources,material.get('direction',kind),versions])[:24]
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO review_materials VALUES (?,?)',(gid,json.dumps(material,ensure_ascii=False)))
        with KnowledgeStore(self.root,self.instance).db() as db:
            db.execute('INSERT OR IGNORE INTO memory_groups(group_id,instance,kind,sources_json,reason) VALUES (?,?,?,?,?)',
                       (gid,self.instance,kind,json.dumps(sources),reason))
        return gid

    def hold(self, session, turn, result, allowed=lambda r:True):
        """Receipt makes retries idempotent. Expiry never bans future retrieval."""
        with self.db() as db:
            receipt=db.execute('SELECT body FROM hold_receipts WHERE session=? AND turn=?',(session,turn)).fetchone()
            if receipt:
                return json.loads(receipt[0])
            row=db.execute('SELECT body FROM holds WHERE session=?',(session,)).fetchone()
            previous=json.loads(row[0]) if row else None
            presentations=result.get('presentations',[])
            ids=sorted({p.get('entity_id') or p.get('event_id') for p in presentations if p.get('entity_id') or p.get('event_id')})
            oldids=previous['ids'] if previous else []
            if ids and (not previous or any(i not in oldids for i in ids)):
                previous={'ids':ids,'started':turn,'result':result}
            elif previous and turn-previous['started']<3:
                if not all(self.eligible(i) for i in previous['ids']) or not allowed(previous['result']):
                    previous=None
            else:
                previous=None
            # Repeated fresh matches may be recalled after expiry, but do not
            # silently renew an existing lease on rounds 2 and 3.
            if previous:
                result=previous['result'].copy()
                result['presence']={'started_turn':previous['started'],'age':turn-previous['started']+1,'max_rounds':3}
                db.execute('INSERT OR REPLACE INTO holds VALUES (?,?)',(session,json.dumps(previous)))
            else:
                db.execute('DELETE FROM holds WHERE session=?',(session,))
            db.execute('INSERT INTO hold_receipts VALUES (?,?,?)',(session,turn,json.dumps(result)))
            db.execute('DELETE FROM hold_receipts WHERE session=? AND turn<?',(session,turn-10))
        return result


def sources_in_context(sources, session, start, message_ids=None):
    if message_ids is not None:
        ids={str(i) for i in message_ids}
        return bool(sources and all(s.get('assistant_message_id') is not None and s['session_id']==session and all(str(i) in ids for i in
            (s.get('user_message_id'),s.get('assistant_message_id')) if i is not None) for s in sources))
    return bool(start and sources and all(s['session_id']==session and
        min(int(s.get('user_message_id') or s['assistant_message_id']),int(s['assistant_message_id']))>=start for s in sources))


def filter_recall(service, result, session, start, message_ids=None):
    """Filter before rendering, including disjoint-source Events and Entity units."""
    from .knowledge import KnowledgeStore
    life=Lifecycle(service.store.root,service.source_instance)
    store=KnowledgeStore(service.store.root,service.source_instance)
    eligible=life.eligibility_checker()
    events=[]
    for ev in service.store.all_events():
        sources=store.event_sources(ev.event_id)
        if sources is None and message_ids is not None:
            refs=service._presented_event_refs([{'event_id':ev.event_id,'presentation':'event'}])
            if refs:
                ref=refs[0]
                sources=[dict(s,session_id=ref.get('source_session_id')) for s in ref.get('turn_messages',[])]
        if not eligible(ev.event_id) or sources_in_context(sources,session.session_id,start,message_ids):
            events.append(ev.event_id)
    # Legacy route accepts a state lookup; callers install a scoped overlay.
    return set(events)
