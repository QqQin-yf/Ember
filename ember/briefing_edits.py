"""App-authored display overrides. Recall records are independent of this store."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3

from .generations import fingerprint
from .knowledge import briefing_edit_receipt
from .memory_lifecycle import Lifecycle


def state(service):
    with Lifecycle(service.store.root,service.source_instance).db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS briefing_edits (revision INTEGER PRIMARY KEY, body TEXT NOT NULL)')
        row=db.execute('SELECT revision,body FROM briefing_edits ORDER BY revision DESC LIMIT 1').fetchone()
    return (row[0],json.loads(row[1])) if row else (0,{'event':{},'knowledge':{},'groups':[]})


def version(event):
    return fingerprint([event.event_id,event.version_id,event.event_time,event.core,event.summary,event.significance])


def protected(service,event):
    return event.significance==10 or service.pin_store.is_pinned(event.event_id)


def plan(service,events,edits,budget,near_hours,onstage=lambda e:False,now=None,count=len,slid=(),slide_threshold=None,slide_target=0.3):
    now=now or datetime.now(timezone.utc)
    cutoff=now-timedelta(hours=near_hours)
    events={e.event_id:e for e in events}
    slid=set(slid)
    overrides=edits['event']
    groups={}; covered=set()
    for group in edits['groups']:
        refs=group['sources']
        if any(mid not in events or protected(service,events[mid]) or version(events[mid])!=rev
               or mid in overrides or mid in slid or onstage(events[mid]) for mid,rev in refs.items()):
            continue
        groups[next(iter(refs))]=group
        covered.update(refs)
    rows=[]
    candidates=[]
    compressible=[]
    for mid,event in events.items():
        if onstage(event) or (mid in slid and not protected(service,event)):
            continue
        if mid in groups:
            group=groups[mid]
            rows.append((mid,group['text'],'pointed',list(group['sources'])))
        if mid in covered:
            continue
        action=None if protected(service,event) else overrides.get(mid)
        if action=='hide':
            continue
        try:
            old=datetime.fromisoformat(event.event_time.replace('Z','+00:00'))<=cutoff
        except (AttributeError,TypeError,ValueError):
            old=False
        short=action=='summary' and event.summary.strip() and not protected(service,event)
        body=(service._briefing_source_block(event) if service.pin_store.is_pinned(mid)
              else event.summary if short else event.core)
        rows.append((mid,body,'compressed' if short else 'original',[mid]))
        if old and not short and not protected(service,event) and 0<len(event.summary.strip())<len(body):
            compressible.append(mid)
        if old and event.significance<8 and not protected(service,event):
            candidates.append(mid)
    def size(items):
        return sum(count(row[1]) for row in items)+max(0,len(items)-1)
    used=size(rows)
    positions={row[0]:index for index,row in enumerate(rows)}
    for mid in sorted(compressible,key=lambda mid:events[mid].significance):
        if used<=budget:
            break
        index=positions[mid]
        used+=count(events[mid].summary)-count(rows[index][1])
        rows[index]=(mid,events[mid].summary,'compressed',[mid])
    before=used
    dropped=[]
    by_id={row[0]:row for row in rows}
    for mid in sorted(candidates,key=lambda mid:events[mid].significance):
        if used<=budget:
            break
        used-=count(by_id[mid][1])+(len(rows)-len(dropped)>1)
        dropped.append(mid)
    dropped_set=set(dropped)
    rows=[row for row in rows if row[0] not in dropped_set]
    task=None
    if dropped:
        ids=list(events)
        runs=[]; current=[]
        for mid in ids:
            if mid in candidates and mid not in overrides:
                current.append(mid)
            elif current:
                runs.append(current);current=[]
        if current: runs.append(current)
        for run in runs:
            hit=next((index for index,mid in enumerate(run) if mid in dropped_set),None)
            if hit is None:
                continue
            first=min(hit,max(0,len(run)-2))
            run=run[first:first+12]
            displaced=dropped_set.intersection(run)
            upper=size([by_id[mid] for mid in run])-1
            if len(run)<2 or not displaced or upper<1:
                continue
            sources={mid:version(events[mid]) for mid in run}
            task={'task_id':'BT-'+fingerprint(run)[:16],'sources':sources,
                  'start':events[run[0]].event_time,'end':events[run[-1]].event_time,
                  'expected_dropped':len(displaced),'max_units':min(upper,max(min(32,upper), (upper+1)//3, upper-max(0,before-budget-1))),
                  'max_chars':sum(len(by_id[mid][1]) for mid in run)+len(run)-2,
                  'count_method':'tokens' if count is not len else 'characters'}
            break
    slid_now=[]
    # Use the same character ratio as the timeline health report.
    total_chars=sum(len(row[1]) for row in rows)
    short_chars=sum(len(body) for _,body,mode,_ in rows if mode!='original')
    if (slide_threshold is not None and used>budget and total_chars
            and short_chars/total_chars>=slide_threshold):
        for mid,body,mode,ids in rows:
            if total_chars==0 or short_chars/total_chars<=slide_target:
                break
            if any(protected(service,events[eid]) for eid in ids):
                continue
            slid_now.extend(ids)
            total_chars-=len(body)
            if mode!='original':
                short_chars-=len(body)
        slid_set=set(slid_now)
        rows=[row for row in rows if not slid_set.intersection(row[3])]
        if task and slid_set.intersection(task['sources']):
            task=None
    return rows,task,dropped,slid_now


def summary_updates(service,edits):
    eligible=Lifecycle(service.store.root,service.source_instance).eligibility_checker()
    events={event.event_id:event for event in service.store.all_events()}
    updates=[]
    for group in edits['groups']:
        changed=[mid for mid,rev in group['sources'].items()
                 if mid not in events or not eligible(mid) or version(events[mid])!=rev
                 or protected(service,events[mid])]
        if changed:
            updates.append({'event_ids':list(group['sources']),'changed_ids':changed,
                            'hint':'来源已变动，简报按单条显示。可重新读取来源并提交合段。'})
    return updates


def task_for(service,edits=None):
    from .timeline import Timeline
    from .lifecycle_config import lifecycle_config
    cfg=lifecycle_config()
    from .briefing_budget import tokens
    budget=cfg['briefing'].get('token_budget')
    timeline=Timeline(service)
    events=timeline.events()
    from .briefing import get
    relationship_limit=get(service).get('relationship_token_budget',budget['relationship']) if budget else cfg['timeline']['upper_chars']
    return plan(service,events,edits if edits is not None else state(service)[1],
                relationship_limit,
                cfg['briefing']['recent_hours'],count=tokens if budget else len,slid=timeline.slid_ids())[1]


def task_hint(service):
    task=task_for(service)
    if task:
        return {k:v for k,v in task.items() if k!='sources'}


def write_hint(service,event,*,created=False):
    receipt=briefing_edit_receipt()
    if event.kind=='relationship':
        try:
            task=task_hint(service)
        except (OSError,sqlite3.Error):
            task=None
        if task:
            receipt['briefing_summary_task']=task
        if created:
            from .timeline import Timeline
            try:
                slide=Timeline(service).take_slide_notice()
            except (OSError,sqlite3.Error):
                slide=None
            if slide:
                receipt['timeline_slide']=slide
    return receipt


def tool_specs():
    string={'type':'string'}
    ids={'type':'array','items':string}
    def spec(name,description,properties,required=()):
        return {'type':'function','function':{'name':name,'description':description,'parameters':{
            'type':'object','properties':properties,'required':list(required),'additionalProperties':False}}}
    return [
        spec('read_briefing_summary_task','读取当前总结待办，或用 event_ids 读取指定段的完整来源和版本。',{'event_ids':ids}),
        spec('edit_briefing','批量预览或修改下次简报。Event、Knowledge 的召回规则继续独立生效。撤销最近一次提交时传当前 revision。',
             {'revision':{'type':'integer'},'preview':{'type':'boolean'},'undo':{'type':'boolean'},
              'operations':{'type':'array','items':{'type':'object','additionalProperties':False,
                  'required':['action','ids'],'properties':{'action':{'enum':['hide_event','summary','hide_knowledge','group','restore']},
                      'ids':ids,'text':string,'task_id':string,'sources':{'type':'object','additionalProperties':string}}}}},
             ('revision',))]
TOOL_NAMES={'read_briefing_summary_task','edit_briefing'}


def dispatch(service,name,args):
    from .knowledge import KnowledgeStore
    from .timeline import Timeline
    from .briefing import get
    with service._generation_lock:
        revision,edits=state(service)
        if name=='read_briefing_summary_task':
            task=task_for(service,edits)
            ids=args.get('event_ids') or list((task or {}).get('sources',{}))
            events=[service.store.get_event(mid) for mid in ids]
            snapshot=get(service)
            return {'status':'ok','revision':revision,'task':task,
                    'last_slide_event_ids':snapshot.get('slid_event_ids',[]),
                    'summary_updates':summary_updates(service,edits),
                    'groups':[{'event_ids':list(group['sources']),'text':group['text']}
                              for group in edits['groups'] if not ids or set(ids).intersection(group['sources'])],
                    'events':[
                {'event_id':event.event_id,'event_time':event.event_time,'core':event.core,
                 'summary':event.summary,'version':version(event)} for event in events]}
        if type(args.get('revision')) is not int or args['revision']!=revision:
            return {'status':'failed','code':'briefing_revision_changed','revision':revision,
                    'hint':'请重新调用 read_briefing_summary_task 获取当前版本，再预览或提交。'}
        original=json.dumps(edits)
        ops=args.get('operations',[])
        if args.get('undo'):
            if not revision or ops:
                raise ValueError('briefing_undo_requires_latest_revision')
            with Lifecycle(service.store.root,service.source_instance).db() as db:
                previous=db.execute('SELECT body FROM briefing_edits WHERE revision<? ORDER BY revision DESC LIMIT 1',(revision,)).fetchone()
            edits=json.loads(previous[0]) if previous else {'event':{},'knowledge':{},'groups':[]}
        elif not ops:
            raise ValueError('briefing_operations_required')
        life=Lifecycle(service.store.root,service.source_instance)
        events={e.event_id:e for e in service.store.all_events() if life.eligible(e.event_id)}
        order=[e.event_id for e in Timeline(service).events()]
        knowledge={r['memory_id']:r for r in KnowledgeStore(service.store.root,service.source_instance).all()}
        affected=set()
        if args.get('undo'):
            old=json.loads(original)
            for key in ('event','knowledge'):
                affected.update(mid for mid in old[key].keys()|edits[key].keys()
                                if old[key].get(mid)!=edits[key].get(mid))
            for group in old['groups']+edits['groups']:
                if (group in old['groups'])!=(group in edits['groups']):
                    affected.update(group['sources'])
        for op in ops:
            ids=op['ids'];action=op['action']
            if not ids or len(ids)!=len(set(ids)):
                raise ValueError('distinct_briefing_ids_required')
            affected.update(ids)
            if action=='restore':
                for mid in ids:
                    edits['event'].pop(mid,None);edits['knowledge'].pop(mid,None)
                edits['groups']=[g for g in edits['groups'] if not set(ids).intersection(g['sources'])]
                continue
            if action=='hide_knowledge':
                if any(mid not in knowledge for mid in ids):
                    raise ValueError('knowledge_not_found')
                edits['knowledge'].update(dict.fromkeys(ids,True))
                continue
            if action not in ('hide_event','summary','group') or any(mid not in events for mid in ids):
                raise ValueError('invalid_briefing_event_operation')
            blocked=[mid for mid in ids if protected(service,events[mid])]
            if blocked:
                return {'status':'rejected','code':'protected_briefing_events','blocked_ids':blocked}
            if any(set(ids).intersection(group['sources']) for group in edits['groups']
                   if all(mid in events and version(events[mid])==rev and not protected(service,events[mid])
                          for mid,rev in group['sources'].items())):
                return {'status':'rejected','code':'restore_existing_group_first','event_ids':ids,
                        'conflicting_groups':[list(group['sources']) for group in edits['groups']
                            if set(ids).intersection(group['sources'])],
                        'hint':'先对冲突段使用 restore，再提交合段。'}
            if action=='group':
                if len(ids)<2 or ids[0] not in order or order[order.index(ids[0]):][:len(ids)]!=ids:
                    positions=[order.index(mid) for mid in ids if mid in order]
                    between=order[min(positions):max(positions)+1] if positions else []
                    return {'status':'rejected','code':'group_requires_contiguous_relationship_events',
                            'event_ids':ids,'expected_order':between,
                            'non_relationship_ids':[mid for mid in ids if mid not in order]}
                current={mid:version(events[mid]) for mid in ids}
                if op.get('sources')!=current:
                    raise ValueError('briefing_group_sources_changed')
                task=task_for(service,edits) if op.get('task_id') else None
                if op.get('task_id') and (not task or task['task_id']!=op['task_id'] or task['sources']!=current):
                    raise ValueError('briefing_task_changed')
                text=op.get('text','').strip()
                cap=min(sum(len(events[mid].summary or events[mid].core) for mid in ids)+len(ids)-2,
                        task['max_chars'] if task else 10000)
                from .briefing_budget import tokens
                count=tokens if task and task['count_method']=='tokens' else len
                if not text or len(text)>cap or (task and count(text)>task['max_units']):
                    return {'status':'rejected','code':'briefing_summary_length_invalid',
                            'event_ids':ids,'actual_chars':len(text),'max_chars':cap,
                            'actual_units':count(text),'max_units':task['max_units'] if task else cap,
                            'count_method':task['count_method'] if task else 'characters'}
                for mid in ids: edits['event'].pop(mid,None)
                edits['groups']=[g for g in edits['groups'] if not set(ids).intersection(g['sources'])]
                edits['groups'].append({'sources':current,'text':text})
            else:
                edits['event'].update(dict.fromkeys(ids,'hide' if action=='hide_event' else 'summary'))
        from .lifecycle_config import lifecycle_config
        cfg=lifecycle_config()
        def size(edits):
            rows,_,_,_=plan(service,Timeline(service).events(),edits,10**20,cfg['briefing']['recent_hours'],slid=Timeline(service).slid_ids())
            return len('\n'.join(row[1] for row in rows))+sum(
                len(KnowledgeStore.line(r)) for mid,r in knowledge.items() if r['pinned'] and mid not in edits['knowledge'])
        saved_chars=size(json.loads(original))-size(edits)
        if args.get('preview'):
            return {'status':'preview','revision':revision,'affected_ids':sorted(affected),'estimated_saved_chars':saved_chars}
        with life.db() as db:
            db.execute('INSERT INTO briefing_edits VALUES (?,?)',(revision+1,json.dumps(edits,ensure_ascii=False)))
        return {'status':'ok','revision':revision+1,'affected_ids':sorted(affected),
                'estimated_saved_chars':saved_chars,**briefing_edit_receipt()}
