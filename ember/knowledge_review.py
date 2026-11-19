"""Knowledge review with source evidence; independent of Sleep context."""
import json
import sqlite3
from pathlib import Path
from .generations import fingerprint
from .knowledge import KnowledgeStore
from .memory_lifecycle import Lifecycle

REVIEW_RETRY_HINT = "Call review_memory_knowledge with {} to retrieve the remaining material and review it again."


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


def review_knowledge(service, args, context, material=None):
    store = KnowledgeStore(service.store.root, service.source_instance)
    item = group(service, args['group_id'], context) if material is None else material
    material=item.get('material',{})
    allowed=set(material.get('memory_ids',[]))
    updates=args['items']
    if not updates or len({r.get('memory_id') for r in updates})!=len(updates):
        raise ValueError('distinct_review_items_required')
    rows={r['memory_id']:r for r in store.all()}
    if any(mid not in rows or (rows[mid]['content_hash'], rows[mid]['confidence']) != version
           for mid, version in item.get('versions', {}).items()):
        raise ValueError('review_material_changed')
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
    return _commit_review(service, store, item, rows, updates)


def _commit_review(service, store, item, rows, updates):
    from .extraction import Extraction
    extraction = Extraction(service)
    life = Lifecycle(store.root, store.instance)
    originals = store.read_sources(item['sources'])
    with life.db(), extraction.db():
        pass
    results, settled = [], []
    with store.db() as db:
        # These stores use the default rollback journal, enabling atomic attached writes.
        db.execute('ATTACH DATABASE ? AS lifecycle', (str(store.root / 'memory_lifecycle.sqlite3'),))
        db.execute('ATTACH DATABASE ? AS extraction', (str(store.root / 'extraction.sqlite3'),))
        db.execute('BEGIN IMMEDIATE')
        if 'material_hash' in item:
            current = db.execute('SELECT state,material_hash FROM extraction.groups WHERE instance=? AND id=?',
                                 (store.instance, item['group_id'])).fetchone()
            if current is None or current['state'] != 'pending' or current['material_hash'] != item['material_hash']:
                raise ValueError('review_material_changed')
            if db.execute('SELECT 1 FROM extraction.reviewed_groups WHERE instance=? AND group_id=? AND material_hash=?',
                          (store.instance, item['group_id'], item['material_hash'])).fetchone():
                raise ValueError('review_material_changed')
        for mid in item['material']['memory_ids']:
            row = db.execute('SELECT content_hash,confidence FROM knowledge WHERE memory_id=? AND instance=?',
                             (mid, store.instance)).fetchone()
            if not row or (row['content_hash'], row['confidence']) != (rows[mid]['content_hash'], rows[mid]['confidence']):
                raise ValueError('review_material_changed')
        for update in updates:
            mid, action, confidence = (update[key] for key in ('memory_id', 'action', 'confidence'))
            row = rows[mid]
            selected = set(update.get('source_slice_ids', []))
            if confidence <= 5:
                selected.update(original['slice_id'] for original in originals if any(
                    update['confidence_evidence'] in message['content'] for message in original['messages']))
            sources = {s['slice_id']: s for s in row['sources'] + [s for s in item['sources'] if s['slice_id'] in selected]}
            for old in update.get('merge_ids', []):
                sources.update({s['slice_id']: s for s in rows[old]['sources']})
            content = row['content'] if action == 'keep' else update['content']
            cues = row['cues'] if action == 'keep' else update['recall_cues']
            digest = fingerprint([row['kind'], content, cues, list(sources.values())])
            db.execute('UPDATE knowledge SET content=?,cues_json=?,sources_json=?,content_hash=?,vector_json=NULL,vector_hash=NULL,confidence=?,pinned=CASE WHEN ?<=5 THEN 0 ELSE pinned END WHERE memory_id=?',
                       (content,json.dumps(cues),json.dumps(list(sources.values())),digest,confidence,confidence,mid))
            db.execute('INSERT OR REPLACE INTO lifecycle.eligibility VALUES (?,?,?,?)',
                       (mid, 'no_recall' if confidence <= 5 else 'active', update.get('confidence_basis') or 'app_review',
                        json.dumps([update.get('confidence_evidence') or f'App AI confidence {confidence}'], ensure_ascii=False)))
            for old in update.get('merge_ids', []):
                db.execute('INSERT OR REPLACE INTO lifecycle.aliases VALUES (?,?)', (old, mid))
                db.execute('INSERT OR REPLACE INTO lifecycle.eligibility VALUES (?,?,?,?)',
                           (old, 'superseded', 'app_knowledge_merge', json.dumps([mid])))
                db.execute('UPDATE knowledge SET pinned=0 WHERE memory_id=? AND instance=?', (old, store.instance))
            if action in ('rewrite', 'merge'):
                used = set(update.get('source_slice_ids', []))
                settled.extend((extraction.instance, s['slice_id'], s['content_hash'], mid, action)
                               for s in item['sources'] if s['slice_id'] in used)
            results.append({'memory_id':mid,'action':action,'confidence':confidence})
        extraction._record_results(db, settled)
        if 'material_hash' in item:
            db.execute('INSERT INTO extraction.reviewed_groups VALUES (?,?,?)',
                       (store.instance, item['group_id'], item['material_hash']))
    return {'status':'ok','items':results,'embedding_status':'pending'}


def _next_material(service, selected):
    from .extraction import Extraction
    from .lifecycle_config import lifecycle_config
    from .slice_embedding import SliceEmbeddingConfig
    extraction = Extraction(service)
    config = SliceEmbeddingConfig.load()
    scope = {'source_session_id': selected['session_id'], 'assistant_message_ids': selected['assistant_message_ids']}
    for row in extraction.pending_groups(config, scope):
        sources = row['remaining_sources']
        digest = fingerprint(sources)
        with extraction.db() as db:
            seen = db.execute('SELECT 1 FROM reviewed_groups WHERE instance=? AND group_id=? AND material_hash=?',
                              (extraction.instance, row['id'], digest)).fetchone()
        if seen:
            continue
        extraction.materialize(row, config, lifecycle_config()['extraction']['batch_chars'] - 2)
        with extraction.db() as db:
            payload_row = db.execute('SELECT payload FROM groups WHERE id=? AND material_hash=?',
                                     (row['id'], digest)).fetchone()
        if not payload_row:
            continue
        data = json.loads(payload_row['payload'])
        similar = extraction.similar(sources, config)
        rows = {item['memory_id']: item for item in extraction.knowledge.all()}
        similar = [item for item in similar if item['memory_id'] in rows]
        if similar:
            ids = [item['memory_id'] for item in similar]
            versions = {mid: (rows[mid]['content_hash'], rows[mid]['confidence']) for mid in ids}
            return {'group_id': row['id'], 'material_hash': digest, 'kind': 'knowledge_review',
                    'sources': sources, 'material': {'memory_ids': ids}, 'versions': versions,
                    'display': {'description': data['description'], 'slices': data['slices'],
                                'similar_knowledge': similar}}
    return None


def review_next(service, args, context):
    scope = (context or {}).get('extraction_scope')
    if not scope or scope.get('mode', 'raw') != 'raw':
        return {'status': 'failed', 'code': 'review_maintenance_required',
                'hint': 'Open the raw memory maintenance session first.'}
    current = context.get('knowledge_review_group')
    if 'items' not in args:
        context.pop('knowledge_review_requires_pickup', None)
        if not current:
            current = _next_material(service, scope)
            context['knowledge_review_group'] = current
        return {'status': 'ok', 'done': current is None, **({'material': current['display']} if current else {})}
    if not current or context.get('knowledge_review_requires_pickup'):
        return {'status': 'failed', 'code': 'review_material_required', 'hint': REVIEW_RETRY_HINT}
    try:
        receipt = review_knowledge(service, args, context, current)
    except (ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        if str(exc) in ('review_material_changed', 'source_slice_changed_or_missing'):
            context.pop('knowledge_review_group', None)
        context['knowledge_review_requires_pickup'] = True
        return {'status': 'failed', 'code': 'invalid_knowledge_review', 'error': 'review_storage_error' if isinstance(exc, sqlite3.Error) else str(exc),
                'hint': REVIEW_RETRY_HINT}
    context.pop('knowledge_review_group', None)
    try:
        return {**receipt, **review_next(service, {}, context)}
    except Exception:
        # The previous review is committed even if fetching the next one fails.
        return {**receipt, 'done': False, 'hint': REVIEW_RETRY_HINT}


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
