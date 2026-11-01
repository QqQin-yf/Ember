"""Promise exposure receipts, append-only logs, and local-midnight expiry."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from .generations import fingerprint
from .lifecycle_config import lifecycle_config
from .memory_lifecycle import Lifecycle


def instant(value):
    dt=datetime.fromisoformat(value.replace('Z','+00:00'))
    if dt.tzinfo is None:
        raise ValueError('due_requires_timezone')
    return dt


class Promises:
    def __init__(self,service):
        self.service=service

    @contextmanager
    def db(self):
        conn=sqlite3.connect(Path(self.service.store.root)/'promises.sqlite3',timeout=20)
        conn.row_factory=sqlite3.Row
        try:
            conn.executescript('''CREATE TABLE IF NOT EXISTS exposures (
                id TEXT PRIMARY KEY,event_id TEXT NOT NULL,route TEXT NOT NULL,at TEXT NOT NULL,
                span TEXT NOT NULL,processed INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS clock(id INTEGER PRIMARY KEY,day TEXT);''')
            with conn:
                yield conn
        finally:
            conn.close()

    def eligibility(self,event):
        if event.kind!='promise':
            return
        life=Lifecycle(self.service.store.root,self.service.source_instance)
        if event.significance<=lifecycle_config()['promise']['low_score_max'] and event.status!='open':
            life.mark(event.event_id,'no_recall','promise_completed',[event.status])
        else:
            with life.db() as conn:
                conn.execute("DELETE FROM eligibility WHERE id=? AND reason='promise_completed'",(event.event_id,))

    def written(self,event):
        if event.kind=='promise':
            if event.significance==10 and event.due is None:
                self.service.pin_store.add(event.event_id)
            self.eligibility(event)

    def enter(self,event_id,route,span,at=None):
        event=self.service.store.get_event(event_id)
        if event.kind!='promise':
            return
        at=at or datetime.now(timezone.utc).isoformat()
        key=fingerprint([event_id,route,span])
        with self.db() as conn:
            conn.execute('INSERT OR IGNORE INTO exposures(id,event_id,route,at,span) VALUES (?,?,?,?,?)',
                         (key,event_id,route,at,json.dumps(span)))

    def pending(self):
        with self.db() as conn:
            rows=[dict(r) for r in conn.execute('SELECT * FROM exposures WHERE processed=0 ORDER BY at,id')]
        result={}
        for row in rows:
            event=self.service.store.get_event(row['event_id'])
            # Recover a crash between durable Event log and exposure receipt.
            if any(row['id'] in entry.get('exposure_ids',[]) for entry in event.log):
                with self.db() as conn:
                    conn.execute('UPDATE exposures SET processed=1 WHERE id=?',(row['id'],))
                continue
            item=result.setdefault(event.event_id,{'event':{k:v for k,v in event.to_dict().items() if k!='focus'},'contexts':[]})
            row['span']=json.loads(row['span'])
            item['contexts'].append(row)
        return list(result.values())

    def process(self,args):
        with self.service._generation_lock:
            event=self.service.store.get_event(args['event_id'])
            pending=next((r for r in self.pending() if r['event']['event_id']==event.event_id),None)
            text=args.get('log')
            if not pending or not isinstance(text,str) or not text.strip():
                raise ValueError('exposed_promise_and_nonempty_log_required')
            status=args.get('status',event.status)
            if 'status' in args and status not in ('done','dropped'):
                raise ValueError('status_must_be_done_or_dropped')
            due=args.get('due',event.due)
            if due is not None:
                instant(due)
            entry={'at':datetime.now(timezone.utc).isoformat(),'text':text.strip(),
                   'spans':[r['span'] for r in pending['contexts']],
                   'exposure_ids':[r['id'] for r in pending['contexts']]}
            event.log=event.log+[entry]
            event.status=status;event.due=due
            self.service.store.write_event(event)
            self.eligibility(event)
            with self.db() as conn:
                conn.executemany('UPDATE exposures SET processed=1 WHERE id=?',[(i,) for i in entry['exposure_ids']])
            return {'status':'ok','event_id':event.event_id,'promise_status':status,'due':due,'log':entry}

    def sweep(self,now=None):
        now=(now or datetime.now(timezone.utc)).astimezone(ZoneInfo(lifecycle_config()['promise']['timezone']))
        day=now.date().isoformat()
        with self.db() as conn:
            row=conn.execute('SELECT day FROM clock WHERE id=1').fetchone()
        if row and row['day']==day:
            return
        with self.service._generation_lock:
            for event in self.service.store.all_events():
                if event.kind=='promise' and event.status=='open' and event.due:
                    if now>=instant(event.due)+timedelta(days=lifecycle_config()['promise']['lapse_days']):
                        event.status='lapsed'
                        event.log=event.log+[{'at':now.isoformat(),'text':'到期后仍无结论，自动转为 lapsed。','spans':[],'exposure_ids':[]}]
                        self.service.store.write_event(event)
                        self.eligibility(event)
            with self.db() as conn:
                conn.execute('INSERT OR REPLACE INTO clock VALUES (1,?)',(day,))

    def overdue(self,now=None):
        now=now or datetime.now(timezone.utc)
        return [e for e in self.service.store.all_events() if e.kind=='promise' and e.status=='open' and e.due and instant(e.due)<=now]


def tool_specs():
    def spec(name,description,props,required):
        return {'type':'function','function':{'name':name,'description':description,'parameters':{
            'type':'object','additionalProperties':False,'properties':props,'required':required}}}
    return [spec('list_pending_promises','读取所有进过上下文但尚未处理的约定及进入时的原文区间；未曝光的约定不出现。',{},[]),
            spec('process_promise','处理一条已进过上下文的约定，必须追加 log；即使状态没变也写明现状。完结可标 done 或 dropped，改期可更新 due。',
                 {'event_id':{'type':'string'},'log':{'type':'string','minLength':1},
                  'status':{'enum':['done','dropped']},'due':{'type':['string','null']}},['event_id','log'])]


TOOL_NAMES={'list_pending_promises','process_promise'}


def dispatch(service,name,args):
    store=Promises(service)
    try:
        if name=='list_pending_promises':
            return {'status':'ok','items':store.pending()}
        return store.process(args)
    except (ValueError,KeyError,TypeError) as exc:
        return {'status':'failed','code':'invalid_promise_update','error':str(exc)}
