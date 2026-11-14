"""Five fixed layers, refreshed only at an explicit context boundary."""
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3

from .briefing_budget import fit, join, tokens
from .knowledge import KnowledgeStore, PROFILE_VISIBLE_CHARS
from .lifecycle_config import lifecycle_config
from .memory_lifecycle import Lifecycle, sources_in_context
from .pin_policy import Pins
from .promises import Promises
from .source_refs import event_reference
from .timeline import Timeline


def source_records(service,event):
    """Exact disjoint provenance, including all originals of a merged Event."""
    knowledge=KnowledgeStore(service.store.root,service.source_instance)
    members=Pins(service).sources(event.event_id) or [event.event_id]
    sources=[]
    for eid in members:
        explicit=knowledge.event_sources(eid)
        if explicit is not None:
            try:
                knowledge.read_sources(explicit)
            except ValueError:
                return None
            sources.extend(explicit)
            continue
        original=service.store.get_event(eid)
        ref=service.source_completions.enrich(event_reference(original,'expanded',service.event_sources,
                                            service.turn_spans.span_of_event(original)))
        if ref.get('source_evidence') not in ('frozen_messages','generation_completed') or not ref.get('turn_messages'):
            return None
        sources.extend(dict(row,session_id=ref['source_session_id']) for row in ref['turn_messages'])
    return sources


def get(service):
    try:
        with open(service._briefing_path(),encoding='utf-8') as stream:
            report=json.load(stream)
            return report if isinstance(report,dict) and isinstance(report.get('text',''),str) else {'status':'failed','code':'briefing_snapshot_unreadable'}
    except FileNotFoundError:
        return {'status':'empty','text':'','knowledge':{'text':'','memory_ids':[]}}
    except (ValueError,OSError):
        return {'status':'failed','code':'briefing_snapshot_unreadable'}


def entity_directory(service, cfg):
    from .promises import instant
    life = Lifecycle(service.store.root)
    eligible = life.eligibility_checker()
    activity = {}
    for event in service.store.all_events():
        try:
            timestamp = instant(event.ingest_time)
        except (TypeError, ValueError, AttributeError):
            continue
        for entity_id in event.entity_refs:
            canonical = life.canonical(entity_id)
            if canonical not in activity or timestamp > activity[canonical]:
                activity[canonical] = timestamp
    cards = [card for card in service.entity_store.all()
             if card.status == 'open' and eligible(card.entity_id)]
    cards.sort(key=lambda card: (
        -activity[card.entity_id].timestamp() if card.entity_id in activity else float('inf'),
        card.entity_id,
    ))
    lines = ['【Entity 目录】写 Event 时可引用已有 ID；需要细节可查 read_entity_events。']
    if not cards:
        return ''
    status = f'已展示全部 {len(cards)} 个 Entity。'
    budget=cfg.get('token_budget')
    count=tokens if budget and 'entity_directory_chars' not in cfg else len
    limit_total=cfg.get('entity_directory_chars',budget['entities'] if budget else 1000)
    used = count(lines[0]) + 1 + count(f'展示 {len(cards)}/{len(cards)} 个 Entity，省略 {len(cards)} 个较不活跃的 Entity。')
    for card in cards:
        summary = ' '.join(card.summary.split())
        first = summary.partition('。')[0]
        limit = cfg.get('entity_directory_line_chars', 100)
        description = first if len(first) <= limit else first[:limit - 1] + '…'
        line = f'• {" ".join(card.name.split())} · {description or "待补概述"} · {card.entity_id}'
        if used + count(line) + 1 > limit_total:
            break
        lines.append(line)
        used += count(line) + 1
    shown = len(lines) - 1
    if shown < len(cards):
        status = f'展示 {shown}/{len(cards)} 个 Entity，省略 {len(cards)-shown} 个较不活跃的 Entity。'
    lines.insert(1,status)
    return '\n'.join(lines)


def refresh(service,args):
    args=args or {}
    sid=args.get('session_id')
    if not isinstance(sid,str) or not sid.strip():
        return {'status':'failed','code':'invalid_briefing_context','error':'缺少 session_id'}
    try:
        mid=int(args.get('keep_from_message_id') or 0)
        ids=args.get('context_message_ids')
        if isinstance(args.get('keep_from_message_id'),bool) or mid<0:
            raise ValueError()
        if ids is not None and (not isinstance(ids,list) or any(isinstance(i,bool) or not str(i).isdecimal() or int(i)<=0 for i in ids)):
            raise ValueError()
    except (ValueError,TypeError):
        return {'status':'failed','code':'invalid_briefing_context'}
    with service._briefing_lock:
        try:
            return _refresh(service,args,sid,mid,ids)
        except (OSError,sqlite3.Error,ValueError,KeyError,TypeError):
            return {'status':'failed','code':'briefing_refresh_unavailable'}


def _refresh(service,args,sid,mid,ids):
    cfg=lifecycle_config()['briefing']
    from .event_mutation import remember_boundary
    remember_boundary(service,sid,mid,ids)
    source_sync={'status':'background'}
    keep_turn=service.turn_ledger.turn_at_or_after(sid,mid) if mid else 0
    pins=Pins(service)

    def onstage(event):
        sources=source_records(service,event)
        if sources and all(s.get('assistant_message_id') is not None for s in sources):
            return sources_in_context(sources,sid,mid,ids)
        if ids is not None or pins.sources(event.event_id):
            return False
        return bool(keep_turn and event.source_ref==f'conversation/session-{sid}' and
                    any(end>=keep_turn for _,end in service.turn_spans.span_of_event(event)))

    now=datetime.now(timezone.utc)
    horizon=now-timedelta(hours=cfg['recent_hours'])
    from .briefing_edits import state
    _,edits=state(service)
    budget=cfg.get('token_budget') if 'total_chars' not in cfg else None
    pinned=[e for e in pins.events() if e.kind!='relationship' and not onstage(e)]
    pin_ids={e.event_id for e in pinned}
    pin_blocks=[pins.event_block(e,local_only=True) for e in pinned]
    store=KnowledgeStore(service.store.root,service.source_instance)
    knowledge=store.briefing(excluded=edits['knowledge'],budget=budget['knowledge'] if budget else None)
    profiles=store.identity_profiles()
    profile_lines=[]
    for profile in profiles:
        overview=profile['overview']
        chars=len(overview)
        count=f'{chars}/{PROFILE_VISIBLE_CHARS} 字' if chars<=PROFILE_VISIBLE_CHARS else f'{chars} 字，显示前 {PROFILE_VISIBLE_CHARS} 字'
        profile['overview']=overview[:PROFILE_VISIBLE_CHARS]
        profile['chars']=chars
        profile_lines.append(f"【{profile['label']}（{count}）】{profile['overview'] or '待 App AI 整理'}")
    profile_text='\n'.join(profile_lines)
    promises=Promises(service)
    from .briefing_edits import protected
    overrides=edits['event']
    due=[e for e in promises.overdue(now) if not onstage(e)
         and (protected(service,e) or overrides.get(e.event_id)!='hide')]
    due_blocks=[f"约定到期 [{e.event_id}] {e.summary if e.event_id in pin_ids or (not protected(service,e) and overrides.get(e.event_id)=='summary') else e.core}（due={e.due}，open）" for e in due]
    previous=get(service)
    revision=int(previous.get('revision',0))+1
    known=[(turn,value) for (session,turn),value in service.turn_ledger.user_msg.items() if session==sid]
    latest=[max(known)[1]] if known else []
    raw_ids=[str(i) for i in (ids if ids is not None else latest)]
    span={'session_id':sid,'message_ids':sorted(set(raw_ids),key=int),'briefing_revision':revision,
          'keep_from_message_id':mid or None}
    if known and os.path.exists(service.generations.path):
        with service.generations._db() as conn:
            row=conn.execute('SELECT recall_id FROM generations WHERE session_id=? AND turn=? ORDER BY rowid DESC LIMIT 1',
                             (sid,max(known)[0])).fetchone()
        if row:
            span['recall_id']=row[0]
    directory = entity_directory(service, cfg)
    if budget:
        selected=[]
        for event,block in zip(pinned,pin_blocks):
            core=block.split('\n  【原文】',1)[0]
            for candidate in (block,core):
                if tokens(join([*(body for _,body in selected),candidate]))<=budget['pins']:
                    selected.append((event,candidate))
                    break
        pinned=[event for event,_ in selected]
        pin_blocks=[block for _,block in selected]
        reminders=list(zip(due,due_blocks))
        reminders=fit(reminders,max(0,budget['promises']-32),lambda item:item[1])
        due=[event for event,_ in reminders]
        due_blocks=[block for _,block in reminders]
    for event in {e.event_id:e for e in pinned+due if e.kind=='promise'}.values():
        promises.enter(event.event_id,'briefing',span)
    pending=len(promises.pending())
    pending_line=f'还有 {pending} 条进过上下文的约定没处理' if pending else ''
    other=join([*pin_blocks,knowledge['text'],*due_blocks,pending_line,directory])
    life=Lifecycle(service.store.root,service.source_instance)
    recent=[]
    for event in service.store.all_events():
        if (event.kind in ('relationship','promise') or event.event_id in pin_ids or
                not life.eligible(event.event_id) or not service.sleep_state.recall_allowed(event.event_id) or onstage(event)
                or event.origin=='observe'):
            continue
        try:
            created=datetime.fromisoformat(event.ingest_time.replace('Z','+00:00'))
            if created.tzinfo is None or not horizon<=created<=now:
                continue
        except (ValueError,TypeError,AttributeError):
            continue
        action=None if protected(service,event) else overrides.get(event.event_id)
        if action=='hide':
            continue
        core=event.significance in (8,9,10) and action!='summary'
        line=f"• [{service._briefing_when(event)}] {event.core if core else event.summary}"
        recent.append({'event':event,'line':line,'core':core,'created':created})
    recent.sort(key=lambda row:(-row['event'].significance,-row['created'].timestamp(),row['event'].event_id))
    recent_demand=tokens(join(row['line'] for row in recent)) if budget else 0
    relationship_budget=None
    if budget:
        fixed_targets=sum(budget[key] for key in ('pins','knowledge','entities','promises'))
        spare=max(0,fixed_targets-tokens(other)+max(0,budget['recent']-recent_demand))
        relationship_budget=max(0,min(budget['relationship']+spare,
                                     budget['total']-budget['reserve']-tokens(other)))
    timeline=Timeline(service).render(onstage,edits,budget=relationship_budget)
    if budget:
        fixed_tokens=tokens(join([timeline['text'],other]))
        remaining=max(0,min(budget['recent']+max(0,relationship_budget-tokens(timeline['text'])),
                            budget['total']-budget['reserve']-fixed_tokens))
    else:
        fixed=[directory,profile_text,timeline['text'],*pin_blocks,knowledge['text'],*due_blocks,pending_line]
        remaining=max(0,cfg.get('total_chars',6000)-sum(len(text)+1 for text in fixed if text))
    chosen=list(recent);dropped={'core':0,'summary':0}
    used=sum(len(row['line'])+1 for row in chosen)
    if budget:
        chosen=fit(chosen,remaining,lambda row:row['line'])
        for row in recent[len(chosen):]:
            dropped['core' if row['core'] else 'summary']+=1
    while not budget and chosen and used>remaining:
        row=chosen.pop();used-=len(row['line'])+1
        dropped['core' if row['core'] else 'summary']+=1
    slide_ids=timeline['slid_event_ids']
    slide_notice=''
    if slide_ids:
        start=service.store.get_event(slide_ids[0]).event_time
        end=service.store.get_event(slide_ids[-1]).event_time
        slide_notice=f'关系时间线已滑出 {len(slide_ids)} 条旧事件（{start[:10]} 至 {end[:10]}），仍可查询。'
    task=timeline['summary_task']
    task_notice=(f"有关系时间线总结待办 {task['task_id']}，可调用 read_briefing_summary_task 读取。"
                 if task else '')
    update_notice=('有关系时间线合段的来源已修订，可调用 read_briefing_summary_task 查看。'
                   if timeline['summary_updates'] else '')

    from .privacy import Privacy
    reminder=Privacy(service.store.root).status(service.source_instance,sid).get('reminder')

    def body_text():
        return join([timeline['text'],*pin_blocks,join(row['line'] for row in chosen),
                     knowledge['text'],*due_blocks,pending_line,slide_notice,task_notice,update_notice,reminder,directory])

    body=body_text()
    if budget:
        while chosen and tokens(body)>budget['total']:
            row=chosen.pop()
            dropped['core' if row['core'] else 'summary']+=1
            body=body_text()
        if tokens(body)>budget['total']:
            return {'status':'failed','code':'briefing_protected_content_over_token_budget',
                    'body_tokens':tokens(body),'token_limit':budget['total']}
    text=join([profile_text,body])
    with Timeline(service).db() as conn:
        relationship_ids=[r[0] for r in conn.execute('SELECT event_id FROM timeline WHERE published_mode IS NOT NULL')]
    included=list(dict.fromkeys(relationship_ids+[e.event_id for e in pinned]+[row['event'].event_id for row in chosen]+[e.event_id for e in due]))
    visible_ids=list(dict.fromkeys(timeline['original_event_ids']+[e.event_id for e in pinned]+[row['event'].event_id for row in chosen]+[e.event_id for e in due]))
    if included:
        service.recall_ledger.record(sid,0,'briefing',included)
    report={'status':'ok','text':text,'chars':len(text),'session_id':sid,'revision':revision,
            'refreshed_at':now.isoformat(),'source_sync':source_sync,'timeline':timeline['health'],'knowledge':knowledge,
            'entity_directory':directory,'identity_profiles':profiles,'summary_task':timeline['summary_task'],
            'slid_event_ids':timeline['slid_event_ids'],'timeline_summary_updates':timeline['summary_updates'],
            'visible_event_ids':visible_ids,
            **({'body_tokens':tokens(body),'token_budget':budget,'tokenizer':'o200k_base',
                'profile_text':profile_text,'relationship_token_budget':relationship_budget} if budget else {}),
            'stats':{'relationship':len(relationship_ids),'pinned':len(pinned),'new72':len(recent),'kept_recent':len(chosen),
                     'due_promises':len(due),'pending_promises':pending,'dropped':dropped}}
    path=service._briefing_path()
    with open(path+'.tmp','w',encoding='utf-8') as stream:
        json.dump(report,stream,ensure_ascii=False)
    os.replace(path+'.tmp',path)
    return report
