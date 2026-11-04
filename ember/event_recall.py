"""Event seats are independent of Entity overviews; Direction is retired."""
import json

from .core_semantic import CoreVectorStore, cosine
from .lifecycle_config import lifecycle_config
from .memory_lifecycle import Lifecycle, filter_recall
from .organization import Organization
from .slice_embedding import SliceEmbeddingConfig
from .slice_recall import SliceRecall


def append_events(service,result,session,turn,text,start=None,entries=None,context_ids=None):
    life=Lifecycle(service.store.root,service.source_instance)
    blocked=filter_recall(service,{},session,start,context_ids)
    trace_id=result['slice_recall'].get('trace_id')
    query=None;space=None
    if trace_id:
        recall=SliceRecall(service.store.root,service.source_instance)
        with recall.db() as db:
            row=db.execute('SELECT vector_json,metadata_json FROM queries WHERE trace_id=?',(trace_id,)).fetchone()
        if row and row['vector_json']:
            query=json.loads(row['vector_json']);space=json.loads(row['metadata_json']).get('space')
    cfg=lifecycle_config()['recall']
    vectors=CoreVectorStore(service.store.root)
    routes={}
    with life.db() as conn:
        cue_vectors={r['id']:dict(r) for r in conn.execute("SELECT * FROM vectors WHERE id LIKE 'cue:%'")}
    for route in service.store.all_routes():
        if route.status!='active':
            continue
        hit=bool(route.cue.strip() and route.cue.casefold() in text.casefold())
        vector=cue_vectors.get('cue:'+route.route_id)
        if not hit and query and vector and vector['space']==space and vector['hash']==route.cue:
            value=json.loads(vector['vector'])
            hit=len(value)==len(query) and cosine(query,value)>=cfg['cue_threshold']
        if hit:
            routes.setdefault(route.event_id,[]).append(route.cue)
    selected=[]
    for event in service.store.all_events():
        if event.event_id in blocked or service.pin_store.is_pinned(event.event_id) or not service.sleep_state.recall_allowed(event.event_id):
            continue
        if event.kind=='relationship':
            from pathlib import Path
            if (Path(service.store.root)/'relationship_timeline.sqlite3').exists():
                from .timeline import Timeline
                if Timeline(service).mode(event.event_id)=='original':
                    continue
        row=vectors.get(event.event_id)
        score=0.
        if query and row and row['model']==space and row['text']==event.core and len(row['vector'])==len(query):
            score=cosine(query,row['vector'])
        cues=routes.get(event.event_id,[])
        if cues or score>=cfg['core_threshold']:
            selected.append((event,cues,score))
    selected.sort(key=lambda item:(-len(set(item[1])),-item[2],item[0].event_id))
    blocks=[];presentations=[];entities={}
    for event,cues,score in selected[:cfg['event_limit']]:
        blocks.append(f'[{event.event_id} {event.kind} {event.significance}/10] {event.core or event.summary}\n触点：'+('、'.join(cues) if cues else 'core 向量')+f'；原文可用 read_memory_source("{event.event_id}") 下钻。')
        presentations.append({'presentation':'event_text','event_id':event.event_id,'version_id':event.version_id,
                              'route':'cue' if cues else 'recall','text':blocks[-1]})
        if event.kind!='relationship':
            for eid in event.entity_refs:
                entities[life.canonical(eid)]=True
        entry=service.recall_ledger.make_entry(session.session_id,turn,'cue' if cues else 'core',[event.event_id],cue=cues[0] if cues else None)
        if entries is not None:
            entries.append(entry)
        else:
            service.recall_ledger.record(session.session_id,turn,entry['route'],entry['event_ids'],cue=entry.get('cue'))
    for card in service.entity_store.all():
        if life.canonical(card.entity_id)!=card.entity_id:
            continue
        if any(name.strip() and name.casefold() in text.casefold() for name in [card.name,*card.aliases]):
            entities[card.entity_id]=True
    names=[]
    for eid in entities:
        try:
            card=service.entity_store.get(eid)
        except KeyError:
            continue
        blocks.append(f'关于〈{card.name}〉 [{eid}]：{card.summary}')
        names.append(card.name)
        presentations.append({'presentation':'entity_overview','entity_id':eid,'text':blocks[-1]})
    for theme in Organization(service).themes():
        hit=theme['name'].casefold() in text.casefold()
        if query and theme['space']==space and len(theme['vector'])==len(query):
            hit=hit or cosine(query,theme['vector'])>=cfg['core_threshold']
        if hit:
            blocks.append(f'[{theme["id"]}] {theme["text"]}')
            presentations.append({'presentation':'theme','memory_id':theme['id']})
    result.update(prefix=('[记忆浮现]\n'+'\n'.join(blocks)) if blocks else '',presentations=presentations,
                  recall_refs={'events':service._presented_event_refs(presentations)},
                  recalled_count=min(len(selected),cfg['event_limit']),entity_recalled=len(names),entity_names=names,
                  filtered_by_sleep=len(blocked),filtered_by_window=0,resolved_brought=[],semantic_matched=sum(s>=cfg['core_threshold'] for _,_,s in selected),
                  semantic_reason='shared_query_vector',dropped_by_seats=max(0,len(selected)-cfg['event_limit']),
                  weak_pooled=0,strong_dropped=0,observe_extra=None)
    return result
