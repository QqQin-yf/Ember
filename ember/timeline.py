"""Append-only relationship history with cached per-Event compression and points."""
import asyncio
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import time

from .generations import fingerprint
from .lifecycle_config import lifecycle_config
from .maintenance_models import ask


class Timeline:
    def __init__(self,service):
        self.service=service
        self.path=Path(service.store.root)/'relationship_timeline.sqlite3'
        with service._generation_lock:
            if not hasattr(service,'_timeline_lock'):
                service._timeline_lock=threading.RLock()
        self.lock=service._timeline_lock

    @contextmanager
    def db(self):
        conn=sqlite3.connect(self.path,timeout=120)
        conn.row_factory=sqlite3.Row
        try:
            conn.executescript('''CREATE TABLE IF NOT EXISTS timeline (
                event_id TEXT PRIMARY KEY,hash TEXT,compressed TEXT,mode TEXT DEFAULT 'original',
                published_mode TEXT,error TEXT,retry_at REAL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS points(id TEXT PRIMARY KEY,text TEXT,event_ids TEXT,intervals TEXT);
                CREATE TABLE IF NOT EXISTS link_jobs(event_id TEXT PRIMARY KEY,hash TEXT,error TEXT,retry_at REAL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);''')
            with conn:
                yield conn
        finally:
            conn.close()

    def events(self):
        def order(event):
            try:
                stamp=datetime.fromisoformat(event.event_time.replace('Z','+00:00'))
                if stamp.tzinfo is None:
                    raise ValueError()
                return (0,stamp.astimezone(timezone.utc),event.event_id)
            except (ValueError,TypeError,AttributeError):
                return (1,event.event_id)
        from .memory_lifecycle import Lifecycle
        eligible=Lifecycle(self.service.store.root,self.service.source_instance).eligibility_checker()
        return sorted([e for e in self.service.store.all_events() if e.kind=='relationship' and eligible(e.event_id)],key=order)

    @staticmethod
    def digest(event):
        return fingerprint([event.core,event.summary,event.event_time,event.significance])

    def synchronize(self,conn):
        events={e.event_id:e for e in self.events()}
        for event in events.values():
            row=conn.execute('SELECT * FROM timeline WHERE event_id=?',(event.event_id,)).fetchone()
            digest=self.digest(event)
            summary = event.summary.strip()
            compact = summary if event.significance < 10 and 0 < len(summary) < len(event.core) else None
            if row is None:
                conn.execute('INSERT INTO timeline(event_id,hash,compressed) VALUES (?,?,?)',
                             (event.event_id,digest,compact))
            elif row['mode'] != 'pointed':
                mode = row['mode'] if compact else 'original'
                conn.execute('UPDATE timeline SET hash=?,compressed=?,mode=?,error=NULL,retry_at=0 WHERE event_id=?',
                             (digest,compact,mode,event.event_id))
        for row in conn.execute('SELECT event_id,mode FROM timeline').fetchall():
            if row['event_id'] not in events and row['mode']!='pointed':
                conn.execute('DELETE FROM timeline WHERE event_id=?',(row['event_id'],))
        return events

    def intervals(self,ids):
        events=self.events();indices=[i for i,e in enumerate(events) if e.event_id in ids]
        ranges=[]
        for index in indices:
            if ranges and ranges[-1][-1]+1==index:
                ranges[-1].append(index)
            else:
                ranges.append([index])
        return [{'start':events[r[0]].event_time or None,'end':events[r[-1]].event_time or None} for r in ranges]

    def points(self,conn):
        return [dict(r,event_ids=json.loads(r['event_ids']),intervals=json.loads(r['intervals']))
                for r in conn.execute('SELECT * FROM points ORDER BY rowid')]

    def cost(self,event,row):
        if self.service.pin_store.is_pinned(event.event_id):
            return len(self.service._briefing_source_block(event))
        return len(row['compressed'] if row['mode']=='compressed' and row['compressed'] else event.core)

    def choose(self,conn,events):
        cfg=lifecycle_config()['timeline']
        rows={r['event_id']:dict(r) for r in conn.execute('SELECT * FROM timeline')}
        total=sum(len(p['text']) for p in self.points(conn))+sum(self.cost(e,rows[e.event_id]) for e in events.values() if rows[e.event_id]['mode']!='pointed' or self.service.pin_store.is_pinned(e.event_id))
        if total>cfg['upper_chars']:
            for eid,event in sorted(events.items(),key=lambda pair:pair[1].significance):
                row=rows[eid]
                if row['mode']!='original' or event.significance==10 or not row['compressed'] or self.service.pin_store.is_pinned(eid):
                    continue
                total+=len(row['compressed'])-len(event.core)
                row['mode']='compressed'
                conn.execute("UPDATE timeline SET mode='compressed' WHERE event_id=?",(eid,))
                if total<=cfg['lower_chars']:
                    break

    def prepare(self):
        cfg=lifecycle_config()['timeline']
        with self.lock,self.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            events=self.synchronize(conn)
            input_hash=fingerprint([[(eid,self.digest(e)) for eid,e in events.items()],self.service.pin_store.all(),cfg])
            previous=conn.execute("SELECT value FROM metadata WHERE key='input_hash'").fetchone()
            cached=conn.execute("SELECT value FROM metadata WHERE key='health'").fetchone()
            if previous and previous[0]==input_hash and cached:
                return json.loads(cached[0])
            self.choose(conn,events)
            health=self.health(conn,events)
            conn.execute("INSERT OR REPLACE INTO metadata VALUES ('health',?)",(json.dumps(health),))
            conn.execute("INSERT OR REPLACE INTO metadata VALUES ('input_hash',?)",(input_hash,))
            return health

    def health(self,conn,events):
        rows={r['event_id']:dict(r) for r in conn.execute('SELECT * FROM timeline')}
        old=sum(len(p['text']) for p in self.points(conn))+sum(len(r['compressed'] or '') for r in rows.values() if r['mode']=='compressed' and r['compressed'] and not self.service.pin_store.is_pinned(r['event_id']))
        original=sum(self.cost(e,rows[eid]) for eid,e in events.items() if (rows[eid]['mode']!='pointed' and not rows[eid]['compressed']) or rows[eid]['mode']=='original' or self.service.pin_store.is_pinned(eid))
        codes=[]
        if old and old/(old+original)>=lifecycle_config()['timeline']['health_ratio']:
            codes.append('relationship_timeline_budget_ratio')
        if any(r['error'] for r in rows.values()):
            codes.append('relationship_compression_pending')
        if old+original>lifecycle_config()['timeline']['upper_chars']:
            codes.append('relationship_timeline_over_budget')
        return {'codes':codes,'compressed_and_points_chars':old,'original_chars':original}

    def slid_ids(self,conn=None):
        if conn is None:
            if not self.path.exists():
                return []
            with self.db() as db:
                return self.slid_ids(db)
        row=conn.execute("SELECT value FROM metadata WHERE key='slid_event_ids'").fetchone()
        return json.loads(row[0]) if row else []

    def take_slide_notice(self):
        if not self.path.exists():
            return None
        with self.db() as conn:
            row=conn.execute("DELETE FROM metadata WHERE key='pending_slide_notice' RETURNING value").fetchone()
            return json.loads(row[0]) if row else None

    def render(self,onstage=lambda event:False,edits=None,*,budget=None):
        from .briefing_edits import plan, state, summary_updates, row_text
        from .briefing_budget import tokens
        cfg=lifecycle_config()
        with self.lock,self.db() as conn:
            events=self.synchronize(conn)
            limit=budget if budget is not None else cfg['timeline']['upper_chars']
            count=tokens if budget is not None else len
            slid=self.slid_ids(conn)
            edits=state(self.service)[1] if edits is None else edits
            rows,task,dropped,slid_now=plan(self.service,events.values(),edits,
                                   limit,cfg['briefing']['recent_hours'],onstage,count=count,slid=slid,
                                   slide_threshold=cfg['timeline']['health_ratio'],
                                   slide_target=cfg['timeline'].get('slide_target_ratio',0.3))
            slide_notice=None
            if slid_now:
                times=sorted(events[eid].event_time for eid in slid_now if events[eid].event_time)
                slide_notice={'start':times[0] if times else None,'end':times[-1] if times else None,
                              'count':len(slid_now),'event_ids':slid_now}
                row=conn.execute("SELECT value FROM metadata WHERE key='pending_slide_notice'").fetchone()
                pending=slide_notice
                if row:
                    previous=json.loads(row[0])
                    bounds=sorted(value for notice in (previous,slide_notice)
                                  for value in (notice['start'],notice['end']) if value)
                    ids=list(dict.fromkeys(previous['event_ids']+slid_now))
                    pending={'start':bounds[0] if bounds else None,'end':bounds[-1] if bounds else None,
                             'count':len(ids),'event_ids':ids}
                conn.execute("INSERT OR REPLACE INTO metadata VALUES ('pending_slide_notice',?)",
                             (json.dumps(pending),))
                conn.execute("INSERT OR REPLACE INTO metadata VALUES ('slid_event_ids',?)",
                             (json.dumps(list(dict.fromkeys(slid+slid_now))),))
            conn.execute('UPDATE timeline SET published_mode=NULL')
            for _,_,mode,ids in rows:
                conn.executemany('UPDATE timeline SET published_mode=?,mode=? WHERE event_id=?',
                                 [(mode,mode,eid) for eid in ids])
            text='\n'.join(row_text(row) for row in rows)
            original=[eid for eid,_,mode,_ in rows if mode=='original']
            old=sum(len(row_text(row)) for row in rows if row[2]!='original')
            codes=[]
            if old and old/max(1,sum(len(row_text(row)) for row in rows))>=cfg['timeline']['health_ratio']:
                codes.append('relationship_timeline_budget_ratio')
            if slid_now:
                codes.append('relationship_timeline_window_slid')
            if count(text)>limit:
                codes.append('relationship_timeline_over_budget')
            health={'codes':codes,'compressed_and_points_chars':old,'original_chars':sum(len(row_text(row)) for row in rows)-old}
            conn.execute("INSERT OR REPLACE INTO metadata VALUES ('health',?)",(json.dumps(health),))
            return {'text':text,'rows':rows,'original_event_ids':original,'dropped_event_ids':dropped+slid_now,'slid_event_ids':slid_now,'slide_notice':slide_notice,
                    'summary_task':{k:v for k,v in task.items() if k!='sources'} if task else None,
                    'summary_updates':summary_updates(self.service,edits),'health':health}

    def mode(self,eid):
        if not self.path.exists():
            return 'pending'
        with closing(sqlite3.connect(self.path,timeout=120)) as conn:
            row=conn.execute('SELECT published_mode FROM timeline WHERE event_id=?',(eid,)).fetchone()
        return row[0] if row and row[0] else 'pending'

    def link(self):
        """Relationships connect history; they never supersede earlier Events."""
        from .relation import EventRelation
        from .privacy import Privacy
        policy = Privacy(self.service.store.root)
        ordered = [e for e in self.events() if not policy.event_blocked(e.to_dict(), self.service.source_instance)]
        for index,event in enumerate(ordered):
            digest=self.digest(event)
            with self.db() as conn:
                row=conn.execute('SELECT * FROM link_jobs WHERE event_id=?',(event.event_id,)).fetchone()
            if row and row['hash']==digest and (not row['error'] or row['retry_at']>time.time()):
                continue
            try:
                earlier=ordered[:index][-lifecycle_config()['grouping']['batch_size']:]
                sources = [source for item in [event, *earlier]
                           for source in policy.event_sources(item.event_id, self.service.source_instance)]
                result=ask('relationship_links','判断这件关系事件对前面的哪些事件构成延续或回应；不做取代，不提 Entity，可以没有关系。',
                    {'current':{'event_id':event.event_id,'core':event.core},
                     'earlier':[{'event_id':e.event_id,'core':e.core} for e in earlier]},
                    {'type':'object','required':['relations'],'properties':{'relations':{'type':'array','items':{
                        'type':'object','required':['event_id','type'],'properties':{'event_id':{'type':'string'},
                            'type':{'enum':['continues','responds_to']}}}}}},
                    source_root=self.service.store.root, sources=sources) if earlier else {'relations':[]}
                if any(r['event_id'] not in {e.event_id for e in earlier} for r in result['relations']):
                    raise ValueError('relationship_link_outside_history')
                with self.service._generation_lock:
                    if self.digest(self.service.store.get_event(event.event_id))!=digest:
                        raise ValueError('relationship_changed_during_linking')
                    for item in result['relations']:
                        rid='rel_'+fingerprint([item['event_id'],event.event_id,item['type']])[:16]
                        if not any(r.relation_id==rid and r.status=='active' for r in self.service.relation_store.all()):
                            self.service.relation_store.add(EventRelation(rid,item['event_id'],event.event_id,item['type'],provenance_refs=['relationship-timeline']))
                with self.db() as conn:
                    conn.execute('INSERT OR REPLACE INTO link_jobs VALUES (?,?,NULL,0)',(event.event_id,digest))
            except Exception as exc:
                with self.db() as conn:
                    conn.execute('INSERT OR REPLACE INTO link_jobs VALUES (?,?,?,?)',
                        (event.event_id,digest,type(exc).__name__,time.time()+lifecycle_config()['slice']['retry_seconds']))
            return True
        return False

    def next_retry(self):
        with self.db() as conn:
            deadlines=[r[0] for r in conn.execute("SELECT retry_at FROM timeline WHERE error IS NOT NULL UNION ALL SELECT retry_at FROM link_jobs WHERE error IS NOT NULL")]
        return min(deadlines) if deadlines else None

    def read_point(self,point):
        with self.db() as conn:
            row=conn.execute('SELECT * FROM points WHERE id=? OR text=?',(point,point)).fetchone()
        if not row:
            return {'status':'failed','code':'relationship_point_not_found'}
        return {'status':'ok','point_id':row['id'],'intervals':json.loads(row['intervals']),
                'events':[{'event_id':eid,'event_time':self.service.store.get_event(eid).event_time,
                           'summary':self.service.store.get_event(eid).summary} for eid in json.loads(row['event_ids'])]}


def tool_specs():
    return [{'type':'function','function':{'name':'read_relationship_point','description':'给出 briefing 中一条要点的完整文字或 point_id，返回底下的 Event 和各自时间；原话再按 Event 下钻。',
        'parameters':{'type':'object','additionalProperties':False,'required':['point'],'properties':{'point':{'type':'string','minLength':1}}}}}]


class TimelineWorker:
    def __init__(self,registry):
        self.registry=registry;self.stop_event=asyncio.Event();self.recovered=set()

    def process_one(self,name):
        from .timeline_work import request, ready, finish
        service=self.registry.service(name)
        root=service.store.root
        if name not in self.recovered:
            # One startup recovery scan, never one scan per idle tick.
            if any(e.kind=='relationship' for e in service.store.all_events()):
                request(root)
            self.recovered.add(name)
        revision=ready(root)
        if revision is None:
            return False
        timeline=Timeline(service)
        started_at=time.time()
        try:
            timeline.prepare()
            while timeline.link():
                pass
            retry=timeline.next_retry()
        except Exception:
            retry=time.time()+lifecycle_config()['slice']['retry_seconds']
        finish(root,revision,retry,started_at=started_at)
        return True

    async def run(self):
        while not self.stop_event.is_set():
            for name in self.registry.names:
                await asyncio.to_thread(self.process_one,name)
            try:
                await asyncio.wait_for(self.stop_event.wait(),timeout=1)
            except asyncio.TimeoutError:
                pass
