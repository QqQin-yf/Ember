"""Author-controlled replacement and source-based revision cooldown."""
import json

from .memory_lifecycle import Lifecycle

COOLDOWN_HINT='已过冷静期，不能修改；写一条新的 Event 取代它（关系事件写一件新的接在后面）。'


def remember_boundary(service,sid,start,ids):
    turn=service.sessions[sid].turn_counter if sid in service.sessions else service.turn_ledger.max_turn(sid)
    with Lifecycle(service.store.root).db() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS context_windows(session TEXT PRIMARY KEY,start INTEGER,ids TEXT,turn INTEGER)')
        conn.execute('INSERT OR REPLACE INTO context_windows VALUES (?,?,?,?)',
                     (sid,start,json.dumps(ids) if ids is not None else None,turn))
    from .briefing import source_records
    from .service import Session
    session=service.sessions.get(sid) or Session(sid)
    context={'session':session,'turn':turn}
    closed=[]
    for event in service.store.all_events():
        sources=source_records(service,event)
        belongs=(any(s.get('session_id')==sid for s in sources) if sources else event.source_ref==f'conversation/session-{sid}')
        if belongs and not visible(service,event,context):
            closed.append(event.event_id)
    seal(service,closed)


def seal(service,event_ids):
    with Lifecycle(service.store.root).db() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS closed_events(event_id TEXT PRIMARY KEY)')
        conn.executemany('INSERT OR IGNORE INTO closed_events VALUES (?)',[(eid,) for eid in event_ids])


def visible(service,event,context=None):
    from .briefing import source_records
    context=context or service._live_turn or getattr(service,'_last_context',None)
    if not context or not context.get('session'):
        return False
    with Lifecycle(service.store.root).db() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS closed_events(event_id TEXT PRIMARY KEY)')
        if conn.execute('SELECT 1 FROM closed_events WHERE event_id=?',(event.event_id,)).fetchone():
            return False
    sid=context['session'].session_id
    sources=source_records(service,event)
    if not sources or any(s.get('session_id')!=sid for s in sources):
        return False
    required={str(s[key]) for s in sources for key in ('user_message_id','assistant_message_id') if s.get(key) is not None}
    if not required:
        return False
    if 'cc_session' in context:
        from . import source_client
        from .source_paths import QuoteSourceError
        anchor = context['anchor']
        if anchor is None:
            return False
        try:
            for rows in source_client.iter_source_branch(sid, anchor, instance=service.source_instance):
                required.difference_update(str(row['message_id']) for row in rows)
                if not required:
                    return True
        except source_client.SourceUnavailable as exc:
            raise QuoteSourceError(exc.code) from exc
        return False
    request=context.get('request_context',{})
    with Lifecycle(service.store.root).db() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS context_windows(session TEXT PRIMARY KEY,start INTEGER,ids TEXT,turn INTEGER)')
        window=conn.execute('SELECT * FROM context_windows WHERE session=?',(sid,)).fetchone()
    # A cut occurring after this generation began overrides its older window.
    if request.get('context_message_ids') is not None and (not window or window['turn']<context['turn']):
        present={str(i) for i in request['context_message_ids']}
        if request.get('user_message_id') is not None:
            present.add(str(request['user_message_id']))
        return required<=present
    messages=[m for m in context['session'].messages if m.get('_source_message_id') is not None]
    start=request.get('context_start_id') or 0
    if window and window['turn']>=context['turn']:
        start=window['start'] or 0
    if window and window['ids'] is not None:
        present={str(i) for i in json.loads(window['ids'])}
        present.update(str(m['_source_message_id']) for m in messages if m.get('turn',0)>window['turn'])
    elif window and not start:
        present={str(m['_source_message_id']) for m in messages if m.get('turn',0)>window['turn']}
    else:
        present={str(m['_source_message_id']) for m in messages if not start or int(m['_source_message_id'])>=start}
    return required<=present


class SupersessionError(ValueError):
    def __init__(self,field,message):
        self.field=field
        super().__init__(message)

    def as_result(self):
        return {'status':'failed','code':'invalid_tool_arguments','field':self.field,'error':str(self),
                'hint':'请提供可查到的旧日常、项目或约定 Event ID；关系事件只接续，不作取代。'}


def replacement_targets(service,args):
    if 'supersedes' in args and args['event']['kind']=='relationship':
        raise SupersessionError('supersedes','关系事件不接受 supersedes。')
    targets={}
    for index,eid in enumerate(args.get('supersedes',[])):
        resolved=service._resolve_event_id(eid)
        if not resolved:
            raise SupersessionError(f'supersedes[{index}]','找不到被取代的 Event。')
        resolved=Lifecycle(service.store.root).canonical(resolved)
        event=service.store.get_event(resolved)
        if event.kind=='relationship':
            raise SupersessionError(f'supersedes[{index}]','关系 Event 不能被取代。')
        targets[resolved]=event
    return list(targets.values())


def apply_replacement(service,event,targets):
    from .relation import EventRelation
    from .generations import fingerprint
    for old in targets:
        old.superseded_by=event.event_id
        service.store.write_event(old)
        Lifecycle(service.store.root).mark(old.event_id,'superseded','authored_supersession',[event.event_id])
        service.sleep_state.mark(old.event_id,'superseded',event.event_id,'memory-writer')
        rid='rel_'+fingerprint([old.event_id,event.event_id,'resolves'])[:16]
        service.relation_store.add(EventRelation(rid,old.event_id,event.event_id,'resolves',provenance_refs=['memory-writer']))
