"""Knowledge review with source evidence; independent of Sleep context."""
import json
from pathlib import Path
from .generations import fingerprint
from .knowledge import KnowledgeStore
from .memory_lifecycle import Lifecycle


def group(service, gid, context=None):
    from .extraction import Extraction
    extraction=Extraction(service);row=extraction.live_group(gid)
    with extraction.db() as conn:
        delivered=conn.execute('SELECT 1 FROM parts WHERE group_id=? AND delivered=1',(gid,)).fetchone()
    if not row['payload'] or not delivered:
        raise ValueError('extraction_group_not_delivered')
    payload=json.loads(row['payload'])
    return {'group_id':gid,'kind':'knowledge_review','sources':row['sources'],
            'material':{'memory_ids':[r['memory_id'] for r in payload['similar_knowledge']]}}


def score(args):
    confidence=args.get('confidence',8)
    if type(confidence) is not int or not 0<=confidence<=10:
        raise ValueError('confidence_must_be_0_to_10')
    if confidence<=5 and (args.get('confidence_basis') not in ('recently_disproved','outdated') or
                          not isinstance(args.get('confidence_evidence'),str) or not args['confidence_evidence'].strip()):
        raise ValueError('low_confidence_requires_recent_disproof_or_outdated_evidence')
    return confidence


def set_confidence(store, mid, args):
    confidence=score(args)
    if confidence<=5:
        row=next(r for r in store.all() if r['memory_id']==mid)
        # Evidence must be attributable, not merely a free-form assertion.
        originals=store.read_sources(row['sources'])
        if not any(args['confidence_evidence'] in m['content'] for s in originals for m in s['messages']):
            raise ValueError('confidence_evidence_not_in_sources')
    with store.db() as db:
        db.execute('UPDATE knowledge SET confidence=? WHERE memory_id=?',(confidence,mid))
        if confidence<=5:
            db.execute('UPDATE knowledge SET pinned=0 WHERE memory_id=?',(mid,))
    life=Lifecycle(store.root,store.instance)
    life.mark(mid,'no_recall' if confidence<=5 else 'active',args.get('confidence_basis') or 'app_review',
              [args.get('confidence_evidence') or 'App AI confidence '+str(confidence)])
    return confidence


def review_knowledge(service,args,context):
    store=KnowledgeStore(service.store.root,service.source_instance)
    item=group(service,args['group_id'],context)
    material=item.get('material',{})
    allowed=set(material.get('memory_ids',[]))
    updates=args['items']
    if not updates or len({r.get('memory_id') for r in updates})!=len(updates):
        raise ValueError('distinct_review_items_required')
    rows={r['memory_id']:r for r in store.all()}
    # Validate the whole review before any accepted write.
    for update in updates:
        mid=update['memory_id']
        if mid not in allowed or mid not in rows:
            raise ValueError('knowledge_outside_review')
        if update['action'] not in ('keep','rewrite','merge'):
            raise ValueError('invalid_review_action')
        score(update)
        if update['action']!='keep' and (not update.get('content','').strip() or not update.get('recall_cues')):
            raise ValueError('rewritten_content_and_cues_required')
        if update['action']=='rewrite' and not update.get('source_slice_ids'):
            raise ValueError('rewrite_requires_selected_sources')
        if not set(update.get('source_slice_ids',[]))<={s['slice_id'] for s in item['sources']}:
            raise ValueError('review_source_outside_material')
        absorbed=update.get('merge_ids',[])
        if not isinstance(absorbed,list) or not set(absorbed)<=allowed-{mid}:
            raise ValueError('merge_outside_review')
        if update['action']=='merge' and not absorbed:
            raise ValueError('merge_members_required')
        if update['confidence']<=5:
            originals=store.read_sources(item['sources'])
            if not any(update['confidence_evidence'] in m['content'] for s in originals for m in s['messages']):
                raise ValueError('confidence_evidence_not_in_sources')
    targets={r['memory_id'] for r in updates}
    merged=[mid for r in updates for mid in r.get('merge_ids',[])]
    if len(set(merged))!=len(merged) or targets.intersection(merged):
        raise ValueError('conflicting_merge_targets')
    if item['kind']=='knowledge_review' and targets.union(merged)!=allowed:
        raise ValueError('all_review_items_must_be_accounted_for')
    results=[]
    life=Lifecycle(store.root,store.instance)
    with store.db() as db:
        for update in updates:
            mid=update['memory_id'];row=rows[mid]
            selected=set(update.get('source_slice_ids',[]))
            if update['confidence']<=5:
                for original in store.read_sources(item['sources']):
                    if any(update['confidence_evidence'] in m['content'] for m in original['messages']):
                        selected.add(original['slice_id'])
            sources={s['slice_id']:s for s in row['sources']+[s for s in item['sources'] if s['slice_id'] in selected]}
            for old in update.get('merge_ids',[]):
                sources.update({s['slice_id']:s for s in rows[old]['sources']})
            content=update.get('content',row['content']) if update['action']!='keep' else row['content']
            cues=update.get('recall_cues',row['cues']) if update['action']!='keep' else row['cues']
            digest=fingerprint([row['kind'],content,cues,list(sources.values())])
            db.execute('UPDATE knowledge SET content=?,cues_json=?,sources_json=?,content_hash=?,vector_json=NULL,vector_hash=NULL,confidence=?,pinned=CASE WHEN ?<=5 THEN 0 ELSE pinned END WHERE memory_id=?',
                (content,json.dumps(cues),json.dumps(list(sources.values())),digest,update['confidence'],update['confidence'],mid))
            results.append({'memory_id':mid,'action':update['action'],'confidence':update['confidence']})
    for update in updates:
        mid=update['memory_id']
        set_confidence(store,mid,update)
        for old in update.get('merge_ids',[]):
            with life.db() as db:
                db.execute('INSERT OR REPLACE INTO aliases VALUES (?,?)',(old,mid))
            life.mark(old,'superseded','app_knowledge_merge',[mid])
            store.pin(old,False)
    return {'status':'ok','items':results,'embedding_status':'pending'}


def patch_members(service,source,target,event_id=None):
    from .store import EVENTS_FILE
    path=Path(service.store.root)/EVENTS_FILE
    if not path.exists():
        return
    rows={}
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.strip():
            e=json.loads(line);rows[e['event_id']]=e
    updates={}
    for eid,e in rows.items():
        refs=e.get('entity_refs',[])
        if (source and source in refs) or eid==event_id:
            updates[eid]={'entity_refs':list(dict.fromkeys([r for r in refs if r!=source]+[target]))}
    service._patch_event_rows(updates)
