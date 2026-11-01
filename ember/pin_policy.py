"""Four pin classes, focus-centred originals and one reminder per overage."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

from .generations import fingerprint
from .knowledge import KnowledgeStore
from .lifecycle_config import lifecycle_config
from .memory_lifecycle import Lifecycle

REMINDER='钉住的约定超出预算，可以整理：unpin 相对不重要的，合并意思相同的。'


def category(event):
    if event.kind=='relationship':
        return 'relationship'
    if event.kind=='promise' and event.significance==10 and event.due is None:
        return 'promise'
    return 'ordinary'


def centred(messages,focus,limit,fallback=''):
    parts=[];target=None;offset=0
    for message in messages:
        text=f"{message['role']}: {message['content']}"
        if str(message.get('message_id',message.get('_source_message_id')))==str(focus) and target is None:
            target=offset+len(text)//2
        parts.append(text);offset+=len(text)+1
    text='\n'.join(parts) if parts else fallback
    if len(text)<=limit:
        return text
    middle=target if target is not None else len(text)//2
    lo=max(0,min(middle-limit//2,len(text)-limit));hi=lo+limit
    return ('前面还有\n' if lo else '')+text[lo:hi]+('\n后面还有' if hi<len(text) else '')


class Pins:
    def __init__(self,service):
        self.service=service

    @contextmanager
    def db(self):
        conn=sqlite3.connect(Path(self.service.store.root)/'pin_policy.sqlite3',timeout=20)
        conn.row_factory=sqlite3.Row
        try:
            conn.executescript('''CREATE TABLE IF NOT EXISTS blocks(event_id TEXT PRIMARY KEY,hash TEXT,body TEXT,source_status TEXT,retry_at REAL);
                CREATE TABLE IF NOT EXISTS budget(id INTEGER PRIMARY KEY,over INTEGER,notified INTEGER,episode INTEGER);
                CREATE TABLE IF NOT EXISTS merged(event_id TEXT PRIMARY KEY,source_ids TEXT);''')
            with conn:
                yield conn
        finally:
            conn.close()

    def sources(self,eid):
        with self.db() as conn:
            row=conn.execute('SELECT source_ids FROM merged WHERE event_id=?',(eid,)).fetchone()
        return json.loads(row[0]) if row else []

    def events(self):
        life=Lifecycle(self.service.store.root)
        ids=list(dict.fromkeys(life.canonical(eid) for eid in self.service.pin_store.all()))
        return [self.service.store.get_event(eid) for eid in ids if eid in self.service.store._events]

    def event_block(self,event):
        members=self.sources(event.event_id) or [event.event_id]
        refs=self.service._presented_event_refs([{'event_id':eid,'presentation':'event_text'} for eid in members])
        digest=fingerprint([event.core,event.event_time,event.focus,refs,lifecycle_config()['pin']['event_chars']])
        with self.db() as conn:
            row=conn.execute('SELECT * FROM blocks WHERE event_id=?',(event.event_id,)).fetchone()
        if row and row['hash']==digest and (row['source_status']=='ok' or row['retry_at']>time.time()):
            return row['body']
        try:
            source=self.service.read_source_event(event.event_id,truncate=False)
        except Exception:
            source={'status':'unavailable'}
        header=f'◆ [{self.service._briefing_when(event)}] {event.core.strip()}'
        body=header
        if source.get('transcript'):
            messages=source.get('messages') or [m for s in source.get('slices',[]) for m in s['messages']]
            text=centred(messages,event.focus,lifecycle_config()['pin']['event_chars'],source['transcript'])
            body+='\n  【原文】\n'+text
        with self.db() as conn:
            conn.execute('INSERT OR REPLACE INTO blocks VALUES (?,?,?,?,?)',
                         (event.event_id,digest,body,source.get('status','unavailable'),time.time()+lifecycle_config()['slice']['retry_seconds']))
        return body

    def listing(self):
        cfg=lifecycle_config()['pin']
        groups={name:{'items':[],'used':0,'limit':cfg[name+'_limit']} for name in ('relationship','ordinary','knowledge')}
        groups['promise']={'items':[],'used_chars':0,'budget_chars':cfg['promise_chars']}
        for event in self.events():
            name=category(event);item={'event_id':event.event_id,'summary':event.summary,'kind':event.kind}
            if name=='promise':
                item['chars']=len(self.event_block(event));groups[name]['used_chars']+=item['chars']
            groups[name]['items'].append(item)
        for row in KnowledgeStore(self.service.store.root,self.service.source_instance).all():
            if row['pinned']:
                groups['knowledge']['items'].append({'memory_id':row['memory_id'],'content':row['content']})
        for name in ('relationship','ordinary','knowledge'):
            groups[name]['used']=len(groups[name]['items'])
        return {'status':'ok','classes':groups}

    def pin(self,eid):
        eid=Lifecycle(self.service.store.root).canonical(eid)
        event=self.service.store.get_event(eid);name=category(event)
        cfg=lifecycle_config()['pin']
        if not self.service.pin_store.is_pinned(eid) and name!='promise':
            items=[e for e in self.events() if category(e)==name]
            if len(items)>=cfg[name+'_limit']:
                return {'status':'rejected','code':'pin_class_full','class':name,'limit':cfg[name+'_limit'],
                        'hint':'这类满了，先 unpin 一条。','items':[{'event_id':e.event_id,'summary':e.summary} for e in items]}
        added=self.service.pin_store.add(eid)
        self.budget()
        return {'status':'ok','event_id':eid,'pinned':True,'class':name,'already':not added}

    def unpin(self,eid):
        eid=Lifecycle(self.service.store.root).canonical(eid)
        if not eid:
            raise ValueError('event_id_required')
        removed=False
        for raw in self.service.pin_store.all():
            if Lifecycle(self.service.store.root).canonical(raw)==eid:
                removed=self.service.pin_store.remove(raw) or removed
        self.budget()
        return {'status':'ok','event_id':eid,'pinned':False,'was_pinned':removed}

    def budget(self):
        used=sum(len(self.event_block(e)) for e in self.events() if category(e)=='promise')
        over=used>lifecycle_config()['pin']['promise_chars']
        with self.db() as conn:
            row=conn.execute('SELECT * FROM budget WHERE id=1').fetchone()
            episode=(row['episode'] if row else 0)+(1 if over and (not row or not row['over']) else 0)
            notified=bool(row and row['notified'] and row['over'] and over)
            conn.execute('INSERT OR REPLACE INTO budget VALUES (1,?,?,?)',(over,notified,episode))
        return {'used_chars':used,'budget_chars':lifecycle_config()['pin']['promise_chars'],
                'episode':episode,'reminder':REMINDER if over and not notified else None}

    def acknowledge(self,episode):
        with self.db() as conn:
            conn.execute('UPDATE budget SET notified=1 WHERE id=1 AND episode=? AND over=1',(episode,))

    def merge(self,args):
        life=Lifecycle(self.service.store.root)
        ids=list(dict.fromkeys(life.canonical(eid) for eid in args['event_ids']))
        if len(ids)<2 or any(not isinstance(args.get(key),str) or not args[key].strip() for key in ('summary','core')):
            raise ValueError('distinct_events_and_rewritten_summary_core_required')
        events=[self.service.store.get_event(eid) for eid in ids]
        if any(category(e)!='promise' for e in events):
            raise ValueError('merge_requires_ten_without_due_promises')
        def created(event):
            try:
                value=datetime.fromisoformat(event.ingest_time.replace('Z','+00:00'))
                return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
            except (ValueError,TypeError):
                return datetime.min.replace(tzinfo=timezone.utc)
        order={event.event_id:i for i,event in enumerate(self.service.store.all_events())}
        events.sort(key=lambda event:(created(event),order[event.event_id]));main=events[0]
        sources=list(dict.fromkeys(eid for event in events for eid in (self.sources(event.event_id) or [event.event_id])))
        log=list(main.log);seen={fingerprint(entry) for entry in log}
        for event in events[1:]:
            for entry in event.log:
                if fingerprint(entry) not in seen:
                    seen.add(fingerprint(entry));log.append(entry)
        main.summary=args['summary'].strip();main.core=args['core'].strip();main.log=log
        main.entity_refs=list(dict.fromkeys(eid for e in events for eid in e.entity_refs))
        self.service.store.write_event(main)
        with self.db() as conn:
            conn.execute('INSERT OR REPLACE INTO merged VALUES (?,?)',(main.event_id,json.dumps(sources)))
        pinned=any(self.service.pin_store.is_pinned(e.event_id) for e in events)
        for event in events[1:]:
            event.merged_into=main.event_id
            self.service.store.write_event(event)
            with life.db() as conn:
                conn.execute('INSERT OR REPLACE INTO aliases VALUES (?,?)',(event.event_id,main.event_id))
            self.service.pin_store.remove(event.event_id)
        if pinned:
            self.service.pin_store.add(main.event_id)
        self.budget()
        return {'status':'ok','event_id':main.event_id,'merged_ids':[e.event_id for e in events[1:]],'source_event_ids':sources}


def tool_specs():
    return [
        {'type':'function','function':{'name':'list_memory_pins','description':'按四类列出 pin 清单与已用/上限；10 分无 due 约定显示字数和预算。','parameters':{'type':'object','additionalProperties':False,'properties':{}}}},
        {'type':'function','function':{'name':'merge_memory_events','description':'合并意思相同的 10 分无 due 约定；留最早一条，合并原文区间与 log。由你重写 summary 和 core，旧 ID 仍可找到主条。',
            'parameters':{'type':'object','additionalProperties':False,'required':['event_ids','summary','core'],'properties':{
                'event_ids':{'type':'array','minItems':2,'uniqueItems':True,'items':{'type':'string'}},
                'summary':{'type':'string','minLength':1},'core':{'type':'string','minLength':1}}}}}]
