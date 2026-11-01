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
from .maintenance_models import ask, prose


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
                CREATE TABLE IF NOT EXISTS points(id TEXT PRIMARY KEY,text TEXT,event_ids TEXT,intervals TEXT,invalid INTEGER DEFAULT 0);
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
        return sorted([e for e in self.service.store.all_events() if e.kind=='relationship'],key=order)

    @staticmethod
    def digest(event):
        return fingerprint([event.core,event.summary,event.event_time])

    def synchronize(self,conn):
        events={e.event_id:e for e in self.events()}
        for event in events.values():
            row=conn.execute('SELECT * FROM timeline WHERE event_id=?',(event.event_id,)).fetchone()
            digest=self.digest(event)
            if row and (row['hash']!=digest or (row['mode']=='pointed' and self.service.pin_store.is_pinned(event.event_id))):
                # Invalid derived prose remains resolvable by its old point ID,
                # but is no longer rendered. Rebuild from corrected originals.
                for point in conn.execute('SELECT * FROM points WHERE invalid=0').fetchall():
                    members=json.loads(point['event_ids'])
                    if event.event_id in members:
                        conn.execute('UPDATE points SET invalid=1 WHERE id=?',(point['id'],))
                        conn.executemany("UPDATE timeline SET mode='original',published_mode=NULL WHERE event_id=?",[(i,) for i in members])
                conn.execute("UPDATE timeline SET hash=?,compressed=?,mode='original',published_mode=NULL,error=NULL,retry_at=0 WHERE event_id=?",(digest,row['compressed'] if row['hash']==digest else None,event.event_id))
            elif row is None:
                conn.execute('INSERT INTO timeline(event_id,hash) VALUES (?,?)',(event.event_id,digest))
        # Changing kind removes a derived timeline entry, never the Event.
        for row in conn.execute('SELECT event_id FROM timeline').fetchall():
            if row['event_id'] not in events:
                for point in conn.execute('SELECT * FROM points WHERE invalid=0').fetchall():
                    members=json.loads(point['event_ids'])
                    if row['event_id'] in members:
                        conn.execute('UPDATE points SET invalid=1 WHERE id=?',(point['id'],))
                        conn.executemany("UPDATE timeline SET mode='original',published_mode=NULL WHERE event_id=?",[(i,) for i in members])
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
                for r in conn.execute('SELECT * FROM points WHERE invalid=0 ORDER BY rowid')]

    def cost(self,event,row):
        if self.service.pin_store.is_pinned(event.event_id):
            return len(self.service._briefing_source_block(event))
        return len(row['compressed'] if row['mode']=='compressed' and row['compressed'] else event.core)

    def choose(self,conn,events):
        cfg=lifecycle_config()['timeline']
        rows={r['event_id']:dict(r) for r in conn.execute('SELECT * FROM timeline')}
        total=sum(len(p['text']) for p in self.points(conn))+sum(self.cost(e,rows[e.event_id]) for e in events.values() if rows[e.event_id]['mode']!='pointed')
        if total>cfg['upper_chars']:
            for eid,event in events.items():
                row=rows[eid]
                if row['mode']!='original' or not row['compressed'] or self.service.pin_store.is_pinned(eid):
                    continue
                total+=len(row['compressed'])-len(event.core)
                row['mode']='compressed'
                conn.execute("UPDATE timeline SET mode='compressed' WHERE event_id=?",(eid,))
                if total<cfg['lower_chars']:
                    break

    def extract(self,conn,events):
        cfg=lifecycle_config()['timeline']
        pending=[dict(r) for r in conn.execute("SELECT * FROM timeline WHERE mode='compressed'")]
        ordered=[r for eid in events for r in pending if r['event_id']==eid and not self.service.pin_store.is_pinned(eid)]
        if sum(len(r['compressed']) for r in ordered)<=cfg['compressed_upper_chars']:
            conn.execute("DELETE FROM metadata WHERE key IN ('point_error','point_retry')")
            return
        while sum(len(r['compressed']) for r in ordered)>=cfg['compressed_lower_chars'] and ordered:
            batch=ordered[:cfg['point_batch']]
            ids={r['event_id'] for r in batch}
            previous=self.points(conn)
            result=ask('relationship_points',
                '接着已有要点往下写，只写这一批中已有要点没有的新内容；后事改变前事时写“后来……”，不回头改已有文字。每条要点列出来源 event_ids。重复事实可用 existing_point_id 补来源，text 必须为空。必须覆盖这批全部 Event，不能把不相干来源塞进去。',
                {'previous':previous,'batch':[{'event_id':r['event_id'],'text':r['compressed']} for r in batch]},
                {'type':'object','required':['points'],'properties':{'points':{'type':'array','items':{
                    'type':'object','required':['text','event_ids'],'properties':{'text':{'type':'string'},
                        'event_ids':{'type':'array','minItems':1,'uniqueItems':True,'items':{'type':'string'}},
                        'existing_point_id':{'type':'string'}}}}}})
            coverage=set();known={p['id']:p for p in previous}
            for item in result['points']:
                members=set(item['event_ids']);coverage|=members
                if not members<=ids:
                    raise ValueError('point_source_outside_batch')
                if item.get('existing_point_id'):
                    if item['existing_point_id'] not in known or item['text']:
                        raise ValueError('point_text_is_append_only')
                elif not item['text'].strip():
                    raise ValueError('empty_new_point')
            if coverage!=ids or any(self.digest(self.service.store.get_event(r['event_id']))!=r['hash'] for r in batch):
                raise ValueError('point_sources_changed_or_incomplete')
            for item in result['points']:
                pid=item.get('existing_point_id')
                members=set(item['event_ids'])
                if pid:
                    members.update(known[pid]['event_ids'])
                    conn.execute('UPDATE points SET event_ids=?,intervals=? WHERE id=?',
                                 (json.dumps(sorted(members)),json.dumps(self.intervals(members)),pid))
                else:
                    pid='PT-'+fingerprint([item['text'],sorted(members)])[:16]
                    conn.execute('INSERT OR IGNORE INTO points(id,text,event_ids,intervals) VALUES (?,?,?,?)',
                                 (pid,item['text'],json.dumps(sorted(members)),json.dumps(self.intervals(members))))
            conn.executemany("UPDATE timeline SET mode='pointed' WHERE event_id=?",[(eid,) for eid in ids])
            ordered=ordered[len(batch):]
        conn.execute("DELETE FROM metadata WHERE key IN ('point_error','point_retry')")

    def prepare(self,refresh=False):
        cfg=lifecycle_config()['timeline']
        with self.lock,self.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            events=self.synchronize(conn)
            input_hash=fingerprint([[(eid,self.digest(e)) for eid,e in events.items()],self.service.pin_store.all(),cfg])
            previous=conn.execute("SELECT value FROM metadata WHERE key='input_hash'").fetchone()
            cached=conn.execute("SELECT value FROM metadata WHERE key='health'").fetchone()
            retry=conn.execute('SELECT 1 FROM timeline WHERE compressed IS NULL AND (? OR retry_at<=?) LIMIT 1',(refresh,time.time())).fetchone()
            point_error=conn.execute("SELECT value FROM metadata WHERE key='point_retry'").fetchone()
            retry_point=point_error and (refresh or float(point_error[0])<=time.time())
            if previous and previous[0]==input_hash and cached and not retry and not retry_point:
                return json.loads(cached[0])

            for row in conn.execute('SELECT * FROM timeline WHERE compressed IS NULL').fetchall():
                if not refresh and row['retry_at']>time.time():
                    continue
                event=events[row['event_id']]
                for _ in range(cfg['retries']+1):
                    try:
                        text=prose('relationship_compress','把这一件关系事件压到它自己还能说准的最短，不给目标长度，不为了预算牺牲意思。',
                                   {'event_id':event.event_id,'summary':event.summary,'core':event.core})
                        if self.digest(self.service.store.get_event(event.event_id))!=row['hash']:
                            raise ValueError('relationship_changed_during_compression')
                        conn.execute('UPDATE timeline SET compressed=?,error=NULL,retry_at=0 WHERE event_id=?',(text,event.event_id))
                        break
                    except Exception as exc:
                        conn.execute('UPDATE timeline SET error=?,retry_at=? WHERE event_id=?',
                                     (type(exc).__name__,time.time()+lifecycle_config()['slice']['retry_seconds'],event.event_id))
            self.choose(conn,events)
            try:
                self.extract(conn,events)
            except Exception as exc:
                conn.execute("INSERT OR REPLACE INTO metadata VALUES ('point_error',?)",(type(exc).__name__,))
                conn.execute("INSERT OR REPLACE INTO metadata VALUES ('point_retry',?)",(str(time.time()+lifecycle_config()['slice']['retry_seconds']),))
            health=self.health(conn,events)
            conn.execute("INSERT OR REPLACE INTO metadata VALUES ('health',?)",(json.dumps(health),))
            conn.execute("INSERT OR REPLACE INTO metadata VALUES ('input_hash',?)",(input_hash,))
            return health

    def health(self,conn,events):
        rows={r['event_id']:dict(r) for r in conn.execute('SELECT * FROM timeline')}
        old=sum(len(p['text']) for p in self.points(conn))+sum(len(r['compressed'] or '') for r in rows.values() if r['mode']=='compressed' and not self.service.pin_store.is_pinned(r['event_id']))
        original=sum(self.cost(e,rows[eid]) for eid,e in events.items() if rows[eid]['mode']=='original' or self.service.pin_store.is_pinned(eid))
        codes=[]
        if old and old/(old+original)>=lifecycle_config()['timeline']['health_ratio']:
            codes.append('relationship_timeline_budget_ratio')
        if any(r['error'] for r in rows.values()):
            codes.append('relationship_compression_pending')
        if conn.execute("SELECT 1 FROM metadata WHERE key='point_error'").fetchone():
            codes.append('relationship_points_failed')
        if old+original>lifecycle_config()['timeline']['upper_chars']:
            codes.append('relationship_timeline_over_budget')
        return {'codes':codes,'compressed_and_points_chars':old,'original_chars':original}

    def render(self,onstage=lambda event:False):
        with self.lock:
            self.prepare(refresh=True)
            with self.db() as conn:
                events={e.event_id:e for e in self.events()}
                rows={r['event_id']:dict(r) for r in conn.execute('SELECT * FROM timeline')}
                conn.execute('UPDATE timeline SET published_mode=NULL')
                blocks=[]
                order={eid:i for i,eid in enumerate(events)}
                for point in self.points(conn):
                    if all(onstage(events[eid]) for eid in point['event_ids'] if eid in events):
                        continue
                    blocks.append((min(order[i] for i in point['event_ids'] if i in order),point['text']))
                    conn.executemany("UPDATE timeline SET published_mode='pointed' WHERE event_id=?",[(i,) for i in point['event_ids']])
                for eid,event in events.items():
                    if onstage(event) or rows[eid]['mode']=='pointed':
                        continue
                    mode='original' if self.service.pin_store.is_pinned(eid) else rows[eid]['mode']
                    text=(self.service._briefing_source_block(event) if self.service.pin_store.is_pinned(eid) else
                          rows[eid]['compressed'] if mode=='compressed' else event.core)
                    blocks.append((order[eid],text))
                    conn.execute('UPDATE timeline SET published_mode=? WHERE event_id=?',(mode,eid))
                health=self.health(conn,events)
                return {'text':'\n'.join(text for _,text in sorted(blocks,key=lambda item:item[0])),'health':health}

    def mode(self,eid):
        if not self.path.exists():
            return 'pending'
        with closing(sqlite3.connect(self.path,timeout=120)) as conn:
            row=conn.execute('SELECT published_mode FROM timeline WHERE event_id=?',(eid,)).fetchone()
        return row[0] if row and row[0] else 'pending'

    def link(self):
        """Relationships connect history; they never supersede earlier Events."""
        from .relation import EventRelation
        ordered=self.events()
        for index,event in enumerate(ordered):
            digest=self.digest(event)
            with self.db() as conn:
                row=conn.execute('SELECT * FROM link_jobs WHERE event_id=?',(event.event_id,)).fetchone()
            if row and row['hash']==digest and (not row['error'] or row['retry_at']>time.time()):
                continue
            try:
                earlier=ordered[:index][-lifecycle_config()['grouping']['batch_size']:]
                result=ask('relationship_links','判断这件关系事件对前面的哪些事件构成延续或回应；不做取代，不提 Entity，可以没有关系。',
                    {'current':{'event_id':event.event_id,'core':event.core},
                     'earlier':[{'event_id':e.event_id,'core':e.core} for e in earlier]},
                    {'type':'object','required':['relations'],'properties':{'relations':{'type':'array','items':{
                        'type':'object','required':['event_id','type'],'properties':{'event_id':{'type':'string'},
                            'type':{'enum':['continues','responds_to']}}}}}}) if earlier else {'relations':[]}
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
            return

    def read_point(self,point):
        with self.db() as conn:
            row=conn.execute('SELECT * FROM points WHERE id=? OR text=?',(point,point)).fetchone()
        if not row:
            return {'status':'failed','code':'relationship_point_not_found'}
        return {'status':'ok','point_id':row['id'],'intervals':json.loads(row['intervals']),'invalidated':bool(row['invalid']),
                'events':[{'event_id':eid,'event_time':self.service.store.get_event(eid).event_time,
                           'summary':self.service.store.get_event(eid).summary} for eid in json.loads(row['event_ids'])]}


def tool_specs():
    return [{'type':'function','function':{'name':'read_relationship_point','description':'给出 briefing 中一条要点的完整文字或 point_id，返回底下的 Event 和各自时间；原话再按 Event 下钻。',
        'parameters':{'type':'object','additionalProperties':False,'required':['point'],'properties':{'point':{'type':'string','minLength':1}}}}}]


class TimelineWorker:
    def __init__(self,registry):
        self.registry=registry;self.stop_event=asyncio.Event()

    async def run(self):
        while not self.stop_event.is_set():
            for name in self.registry.names:
                service=self.registry.service(name)
                if any(e.kind=='relationship' for e in service.store.all_events()):
                    try:
                        await asyncio.to_thread(Timeline(service).prepare)
                        await asyncio.to_thread(Timeline(service).link)
                    except Exception:
                        pass
            try:
                await asyncio.wait_for(self.stop_event.wait(),timeout=1)
            except asyncio.TimeoutError:
                pass
