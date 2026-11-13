"""Five fixed layers, refreshed only at an explicit context boundary."""
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3

from .knowledge import KnowledgeStore
from .lifecycle_config import lifecycle_config
from .memory_lifecycle import Lifecycle, sources_in_context
from .pin_policy import Pins
from .promises import Promises
from .source_refs import event_reference
from .source_sync import sync_increment
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
    used = len(lines[0]) + 1 + len(f'展示 {len(cards)}/{len(cards)} 个 Entity，省略 {len(cards)} 个较不活跃的 Entity。')
    for card in cards:
        summary = ' '.join(card.summary.split())
        first = summary.partition('。')[0]
        limit = cfg.get('entity_directory_line_chars', 100)
        description = first if len(first) <= limit else first[:limit - 1] + '…'
        line = f'• {" ".join(card.name.split())} · {description or "待补概述"} · {card.entity_id}'
        if used + len(line) + 1 > cfg.get('entity_directory_chars', 1000):
            break
        lines.append(line)
        used += len(line) + 1
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
    source_sync=sync_increment(service)
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
    timeline=Timeline(service).render(onstage)
    pinned=[e for e in pins.events() if e.kind!='relationship' and not onstage(e)]
    pin_ids={e.event_id for e in pinned}
    pin_blocks=[pins.event_block(e) for e in pinned]
    store=KnowledgeStore(service.store.root,service.source_instance)
    knowledge=store.briefing()
    profiles=store.identity_profiles()
    profile_lines=[]
    limit=lifecycle_config()['pin']['knowledge_chars']
    cognition={row['memory_id']:row for row in store.all() if row['kind']=='cognition'}
    for profile in profiles:
        profile_lines.append(f"【{profile['label']}】{profile['overview'] or '待 App AI 整理'}")
        for mid in profile['cognition_ids']:
            item=cognition.get(mid)
            if item:
                content=item['content']
                profile_lines.append(store.line(dict(item,content=content[:limit]+('…' if len(content)>limit else ''))))
    profile_text='\n'.join(profile_lines)
    promises=Promises(service)
    due=[e for e in promises.overdue(now) if not onstage(e)]
    due_blocks=[f"约定到期 [{e.event_id}] {e.summary if e.event_id in pin_ids else e.core}（due={e.due}，open）" for e in due]
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
    for event in {e.event_id:e for e in pinned+due if e.kind=='promise'}.values():
        promises.enter(event.event_id,'briefing',span)
    pending=len(promises.pending())
    pending_line=f'还有 {pending} 条进过上下文的约定没处理' if pending else ''
    directory = entity_directory(service, cfg)
    fixed=[directory,profile_text,timeline['text'],*pin_blocks,knowledge['text'],*due_blocks,pending_line]
    remaining=max(0,cfg['total_chars']-sum(len(text)+1 for text in fixed if text))
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
        core=event.significance in (8,9,10)
        line=f"• [{service._briefing_when(event)}] {event.core if core else event.summary}"
        recent.append({'event':event,'line':line,'core':core,'created':created})
    recent.sort(key=lambda row:(-row['event'].significance,-row['created'].timestamp(),row['event'].event_id))
    chosen=list(recent);dropped={'core':0,'summary':0}
    used=sum(len(row['line'])+1 for row in chosen)
    while chosen and used>remaining:
        row=chosen.pop();used-=len(row['line'])+1
        dropped['core' if row['core'] else 'summary']+=1
    text='\n'.join(part for part in [profile_text,timeline['text'],*pin_blocks,
                     '\n'.join(row['line'] for row in chosen),knowledge['text'],*due_blocks,pending_line] if part)
    text = '\n'.join(part for part in (text, directory) if part)
    with Timeline(service).db() as conn:
        relationship_ids=[r[0] for r in conn.execute('SELECT event_id FROM timeline WHERE published_mode IS NOT NULL')]
    included=list(dict.fromkeys(relationship_ids+[e.event_id for e in pinned]+[row['event'].event_id for row in chosen]+[e.event_id for e in due]))
    visible_ids=list(dict.fromkeys(timeline['original_event_ids']+[e.event_id for e in pinned]+[row['event'].event_id for row in chosen]+[e.event_id for e in due]))
    if included:
        service.recall_ledger.record(sid,0,'briefing',included)
    report={'status':'ok','text':text,'chars':len(text),'session_id':sid,'revision':revision,
            'refreshed_at':now.isoformat(),'source_sync':source_sync,'timeline':timeline['health'],'knowledge':knowledge,
            'entity_directory':directory,'identity_profiles':profiles,
            'visible_event_ids':visible_ids,
            'stats':{'relationship':len(relationship_ids),'pinned':len(pinned),'new72':len(recent),'kept_recent':len(chosen),
                     'due_promises':len(due),'pending_promises':pending,'dropped':dropped}}
    path=service._briefing_path()
    with open(path+'.tmp','w',encoding='utf-8') as stream:
        json.dump(report,stream,ensure_ascii=False)
    os.replace(path+'.tmp',path)
    return report
