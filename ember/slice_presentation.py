"""Rerank raw candidates only, then organize by actually presented ownership."""
import json
import sqlite3

from .knowledge import KnowledgeStore
from .slice_embedding import SliceEmbeddingConfig, SliceEmbeddingError
from .slice_recall import SliceRecall
from .slice_reranker import RerankerConfig, SliceReranker, RerankerError


def append_slice_presentations(service,result,query,*,blocked_events=()):
    recall=SliceRecall(service.store.root,service.source_instance)
    metadata=dict(result['slice_recall'])
    trace=recall.trace(metadata.get('trace_id')) if metadata.get('trace_id') else None
    if trace is None:
        return result
    config=None
    selection={'status':'no_candidates','selected':[],'rankings':[]}
    try:
        config=RerankerConfig.load()
        if trace['candidate_pool'] and not config.enabled:
            selection['status']='reranker_not_configured'
        elif trace['candidate_pool']:
            embed_config=SliceEmbeddingConfig.load(recall.config_path)
            knowledge=KnowledgeStore(service.store.root,service.source_instance)
            candidates=[]
            sources=[]
            stale=[]
            for candidate in trace['candidate_pool']:
                try:
                    source=knowledge.read_sources([candidate])[0]
                except ValueError:
                    stale.append(candidate['slice_id']);continue
                candidates.append(candidate);sources.append(source)
            ranks=SliceReranker(config,embed_config).rank(query,sources) if sources else []
            selection.update(status='ranked',model=config.model,min_score=config.score_floor('text'),modality='text',
                source_budget_chars=config.source_budget_chars,stale_sources=stale,
                rankings=[{'slice_id':candidates[r['index']]['slice_id'],'score':r['relevance_score']} for r in ranks])
            presented={r['event_id'] for r in result.get('recall_refs',{}).get('events',[])}
            from .memory_lifecycle import Lifecycle
            eligible = Lifecycle(service.store.root,service.source_instance).eligibility_checker()
            blocks=[];used=0
            for rank in ranks:
                if rank['relevance_score']<config.score_floor('text'):
                    continue
                source=sources[rank['index']];candidate=candidates[rank['index']]
                if not eligible(source['slice_id']):
                    continue
                try:
                    knowledge.read_sources([candidate])
                except ValueError:
                    selection['stale_sources'].append(candidate['slice_id']);continue
                owners=list(dict.fromkeys(m['event_id'] for m in
                    candidate.get('span_memberships',[])+candidate.get('source_memberships',[])))
                parents=candidate.get('parent_event_ids')
                if parents is not None:
                    for eid in parents:
                        if (eid in presented or eid in blocked_events or service.pin_store.is_pinned(eid)
                                or not service.sleep_state.recall_allowed(eid)):
                            continue
                        from .organization import Organization
                        replacement=Organization(service).replacement(eid)
                        if replacement:
                            if any(p.get('memory_id')==replacement['id'] for p in result.get('presentations',[])):
                                continue
                            blocks.append(f"[{replacement['id']}] {replacement['text']}")
                            item={'presentation':'theme','memory_id':replacement['id'],'via_slice_id':source['slice_id']}
                            result.setdefault('presentations',[]).append(item);selection['selected'].append(item)
                            continue
                        if not eligible(eid):
                            continue
                        from .lifecycle_config import lifecycle_config
                        if len(presented)>=lifecycle_config()['recall']['event_limit']:
                            continue
                        event=service.store.get_event(eid)
                        block=f'[{eid} {event.kind}] {event.core}'
                        if used+len(block)>config.source_budget_chars:
                            continue
                        blocks.append(block);used+=len(block)+2;presented.add(eid)
                        item={'presentation':'event_text','event_id':eid,'version_id':event.version_id,
                              'via_slice_id':source['slice_id'],'rerank_score':rank['relevance_score'],'text':block}
                        result.setdefault('presentations',[]).append(item)
                        # Diagnostics survive response expiry and carry no body.
                        selection['selected'].append({k:v for k,v in item.items() if k!='text'})
                        refs=service._presented_event_refs([item])
                        result.setdefault('recall_refs',{}).setdefault('events',[]).extend(refs)
                    continue
                matched=next((eid for eid in owners if eid in presented),None)
                label=(f'补充 {matched} 的命中原文' if matched else
                       '原文切片'+('（来源 '+', '.join(owners)+'，仅呈现命中片段）' if owners else ''))
                header=f'[{source["slice_id"]}] {label}\n'
                body='\n'.join(f"{m['role']}: {m['content']}" for m in source['messages'])
                hint='\n需要完整原文可用 read_memory_slices 按切片 ID 下钻。'
                remaining=config.source_budget_chars-used
                if remaining<len(header)+len(hint)+40:
                    continue
                clipped=len(header)+len(body)+len(hint)>remaining
                block=header+body[:remaining-len(header)-len(hint)]+hint
                blocks.append(block);used+=len(block)+2
                item={'presentation':'source_slice','slice_id':source['slice_id'],'content_hash':source['content_hash'],
                    'owner_event_ids':owners,'merged_into':matched,'source_clipped':clipped,'rerank_score':rank['relevance_score']}
                result.setdefault('presentations',[]).append(item)
                selection['selected'].append(item)
                if matched:
                    for ref in result.get('recall_refs',{}).get('events',[]):
                        if ref['event_id']==matched:
                            ref.setdefault('source_slices',[]).append({k:source[k] for k in
                                ('slice_id','content_hash','session_id','user_message_id','assistant_message_id')})
            if blocks:
                result['prefix']='\n\n'.join(p for p in [result.get('prefix',''),'【命中原文】\n'+'\n\n'.join(blocks)] if p)
    except (RerankerError,SliceEmbeddingError) as exc:
        selection.update(status='unavailable',error_code=str(exc))
    except (OSError,sqlite3.Error,ValueError,KeyError,TypeError):
        selection.update(status='unavailable',error_code='slice_presentation_unavailable')
    metadata.update(mode='hybrid',injected=bool(selection['selected']),rerank=selection,
                    presentation_status=selection['status'],presentation_threshold=config.score_floor('text') if config else None,
                    selection_reason='rerank_then_ownership')
    stored={**trace,**metadata}
    stored.pop('rankings',None);stored.pop('created_at',None)
    with recall.db() as db:
        db.execute('UPDATE queries SET metadata_json=? WHERE trace_id=? AND instance=?',
            (json.dumps(stored,ensure_ascii=False),trace['trace_id'],service.source_instance))
    result['slice_recall']=metadata
    return result
