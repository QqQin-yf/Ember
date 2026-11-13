"""Independent, resumable maintenance. Judgments never author memory prose."""
import asyncio
import json
import math
import os
import time
from datetime import datetime
from pathlib import Path
from .generations import fingerprint
from .memory_lifecycle import Lifecycle
from .knowledge import KnowledgeStore
from .turn_slices import TurnSliceStore
from .slice_embedding import SliceEmbeddingConfig, SliceEmbeddingClient
from .maintenance_client import MaintenanceConfig, MaintenanceClient


def cosine(a,b):
    if len(a)!=len(b):
        return -1
    norm=math.hypot(*a)*math.hypot(*b)
    return sum(x*y for x,y in zip(a,b))/norm if norm else -1


def catalog(service):
    """Typed originals, occurrence dates, provenance and Entity children."""
    from .maintenance_materials import event_document
    knowledge=KnowledgeStore(service.store.root,service.source_instance)
    events={e.event_id:{'id':e.event_id,'kind':'event','document':event_document(e.to_dict()),
        'sources':knowledge.event_sources(e.event_id) or []} for e in service.store.all_events()}
    from .event_slices import EventSliceLinks
    for link in EventSliceLinks(service.store.root).all(service.source_instance):
        if link['event_id'] in events:
            source=TurnSliceStore(service.store.root).get(link['slice_id'])
            if source:
                ref={k:source[k] for k in ('slice_id','content_hash','session_id','user_message_id','assistant_message_id')}
                if ref not in events[link['event_id']]['sources']:
                    events[link['event_id']]['sources'].append(ref)
    rows=list(events.values())
    from .store import EVENTS_FILE
    path=Path(service.store.root)/EVENTS_FILE
    latest={}
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                ev=json.loads(line);latest[ev['event_id']]=ev
    for ent in service.entity_store.all():
        children=[events[eid] for eid,e in latest.items() if ent.entity_id in e.get('entity_refs',[]) and eid in events]
        rows.append({'id':ent.entity_id,'kind':'entity','document':ent.to_dict(),'events':children,
                     'sources':list({s['slice_id']:s for e in children for s in e['sources']}.values())})
    for row in knowledge.all():
        rows.append({'id':row['memory_id'],'kind':'knowledge','document':{k:row[k] for k in
            ('kind','content','cues','pinned','content_hash','confidence')},'sources':row['sources']})
    store=TurnSliceStore(service.store.root)
    if store.path.exists():
        with store._db() as db:
            ids=[r[0] for r in db.execute("SELECT DISTINCT s.slice_id FROM turn_slices s JOIN slice_embeddings e ON s.slice_id=e.slice_id AND s.content_hash=e.content_hash WHERE s.instance=? AND e.state='ready'",(service.source_instance,))]
        for sid in ids:
            s=store.get(sid)
            rows.append({'id':sid,'kind':'slice','document':{'messages':s['messages']},
                         'sources':[{k:s[k] for k in ('slice_id','content_hash','session_id','user_message_id','assistant_message_id')}]})
    return rows


def index_text(row):
    doc=row['document']
    if row['kind']=='event':
        return '\n'.join(str(doc.get(k) or '') for k in ('summary','core','context','participants','objects'))
    if row['kind']=='entity':
        return '\n'.join([doc['name'],*doc.get('aliases',[]),doc.get('summary','')])
    if row['kind']=='knowledge':
        return doc['content']
    from .slice_embedding import slice_text
    return slice_text(doc)


def merge_exact_duplicates(service, rows):
    """Consolidate only identical formal records with the same evidence."""
    life = Lifecycle(service.store.root, service.source_instance)
    seen = {}
    with service._generation_lock:
        for row in rows:
            if row['kind'] not in ('event', 'knowledge') or not row['sources'] or not life.eligible(row['id']):
                continue
            doc = row['document']
            if row['kind'] == 'event':
                event = service.store.get_event(row['id'])
                if event.kind != 'daily' or service.pin_store.is_pinned(event.event_id):
                    continue
                fields = [event.summary, event.core, event.entity_refs,
                          event.confidence, event.significance]
            else:
                if doc['pinned']:
                    continue
                fields = [doc['kind'], doc['content'], doc['cues'], doc['confidence']]
            key = fingerprint([row['kind'], fields, sorted(
                (ref['slice_id'], ref['content_hash']) for ref in row['sources'])])
            target = seen.setdefault(key, row['id'])
            if target == row['id']:
                continue
            with life.db() as db:
                db.execute('INSERT OR IGNORE INTO aliases VALUES (?,?)', (row['id'], target))


def knowledge_reviews(service, rows, vectors):
    life=Lifecycle(service.store.root,service.source_instance)
    items=[r for r in rows if r['kind']=='knowledge' and life.eligible(r['id'])]
    for index,a in enumerate(items):
        for b in items[index+1:]:
            score=cosine(vectors[a['id']],vectors[b['id']])
            if score>=0.65:
                life.propose('knowledge_review',a['sources']+b['sources'],'相似度达到 0.65，待 App 自主判断',
                    {'memory_ids':[a['id'],b['id']],'items':[a,b],'score':score,
                     'instruction':'判断改写合并、结合近期经历补写或维持不变；每条最终记忆评置信度。只有近期经历明显打破或证明过时才能给5分及以下。'})


def strings(value):
    if isinstance(value,str):
        yield value
    elif isinstance(value,dict):
        for v in value.values():
            yield from strings(v)
    elif isinstance(value,list):
        for v in value:
            yield from strings(v)


def body_strings(row):
    document=row['document']
    if row['kind']=='slice':
        yield from (m['content'] for m in document.get('messages',[]))
    elif row['kind']=='entity':
        yield from strings({k:document.get(k) for k in ('name','aliases','summary')})
        for event in row.get('events',[]):
            yield from body_strings(event)
    else:
        yield from strings({k:v for k,v in document.items() if k not in
            ('event_id','version_id','event_time','ingest_time','content_hash','confidence','pinned','kind')})


def validate_result(result, payload):
    known={r['id']:r for r in payload['materials']}
    if not isinstance(result,list) or len(result)!=1 or not isinstance(result[0],dict):
        raise ValueError('maintenance_result_invalid')
    output=result[0]
    if set(output)-{'labels','relations','extensions','proposals','entity_merges','attachments'}:
        raise ValueError('unexpected_maintenance_action')
    for key in ('labels','relations','extensions','proposals','entity_merges','attachments'):
        if not isinstance(output.get(key,[]),list):
            raise ValueError('maintenance_result_invalid')
    def evidence(item, ids):
        refs=item.get('evidence',[])
        if not item.get('reason') or not isinstance(refs,list) or not refs or not ids:
            raise ValueError('maintenance_evidence_missing')
        for mid in ids:
            if mid not in known or not any(isinstance(e,dict) and e.get('id')==mid and isinstance(e.get('quote'),str) and e['quote'] and
                any(e['quote'] in value for value in body_strings(known[mid])) for e in refs):
                raise ValueError('maintenance_evidence_invalid')
    for item in output.get('labels',[]):
        evidence(item,[item.get('id')])
        if known[item['id']]['kind']=='knowledge' or item.get('state') not in ('active','no_recall'):
            raise ValueError('third_party_cannot_score_knowledge')
        if item['state']=='no_recall' and item.get('basis') not in ('meaningless','outdated','contradicted'):
            raise ValueError('no_recall_basis_missing')
        if item['state']=='no_recall' and item.get('basis')!='meaningless':
            participants={e.get('id') for e in item['evidence']}
            if len(participants)<2:
                raise ValueError('outdated_or_conflicting_requires_comparison_evidence')
            evidence(item,list(participants))
    for item in output.get('relations',[]):
        evidence(item,[item.get('source'),item.get('target')])
        if item.get('type') not in ('related_to','continues','contrasts','resolves','same_information') or item['source']==item['target']:
            raise ValueError('relation_type_invalid')
        if item['type']=='resolves':
            old,new=known[item['source']],known[item['target']]
            oldtime=old['document'].get('event_time');newtime=new['document'].get('event_time')
            try:
                before=datetime.fromisoformat(oldtime.replace('Z','+00:00'))
                after=datetime.fromisoformat(newtime.replace('Z','+00:00'))
                ordered=before<after
            except (TypeError,ValueError,AttributeError):
                ordered=False
            if old['kind']!='event' or new['kind']!='event' or not ordered:
                raise ValueError('resolution_requires_later_event_time')
    for item in output.get('extensions',[]):
        evidence(item,item.get('ids',[]))
        if item.get('group_id') not in {g['group_id'] for g in payload['known_groups']}:
            raise ValueError('group_extension_invalid')
    for item in output.get('proposals',[]):
        evidence(item,item.get('ids',[]))
        if len(set(item.get('ids',[])))<2 or item.get('kind') not in ('theme','pattern','habit','preference','entity_extract'):
            raise ValueError('review_proposal_invalid')
        if not item.get('group_id') and (not isinstance(item.get('direction'),str) or not item['direction'].strip()):
            raise ValueError('proposal_direction_required')
        if item['kind']=='entity_extract' and any(known[i]['kind']!='event' for i in item['ids']):
            raise ValueError('entity_requires_events')
        if item.get('group_id') and item['group_id'] not in {g['group_id'] for g in payload['known_groups']}:
            raise ValueError('proposal_group_unknown')
        if item.get('change_evidence'):
            if not isinstance(item['change_evidence'],list) or any(not isinstance(e,dict) for e in item['change_evidence']):
                raise ValueError('change_evidence_invalid')
            evidence({'reason':item['reason'],'evidence':item['change_evidence']},
                list({e.get('id') for e in item['change_evidence']}))
            if len({e['id'] for e in item['change_evidence']})<2:
                raise ValueError('reopening_requires_comparison_evidence')
    for item in output.get('entity_merges',[]):
        evidence(item,[item.get('source'),item.get('target')])
        if any(known[i]['kind']!='entity' for i in (item['source'],item['target'])) or item['source']==item['target']:
            raise ValueError('entity_merge_invalid')
    for item in output.get('attachments',[]):
        evidence(item,[item.get('event_id'),item.get('entity_id')])
        if known[item['event_id']]['kind']!='event' or known[item['entity_id']]['kind']!='entity':
            raise ValueError('attachment_invalid')
    return output


def apply_result(service,payload,output):
    from .knowledge_review import patch_members
    life=Lifecycle(service.store.root,service.source_instance)
    store=KnowledgeStore(service.store.root,service.source_instance)
    known={r['id']:r for r in payload['materials']}
    for item in output.get('labels',[]):
        life.mark(item['id'],item['state'],item['reason'],item['evidence'])
    for item in output.get('relations',[]):
        rid='rel_'+fingerprint([item['source'],item['target'],item['type']])[:24]
        with life.db() as db:
            db.execute('INSERT OR IGNORE INTO relations VALUES (?,?)',(rid,json.dumps(item)))
        if all(known[i]['kind']=='event' for i in (item['source'],item['target'])) and item['type']!='same_information':
            from .relation import EventRelation
            if rid not in {r.relation_id for r in service.relation_store.all()}:
                service.relation_store.add(EventRelation(rid,item['source'],item['target'],item['type'],
                    evidence_refs=[json.dumps(item['evidence'],ensure_ascii=False)],provenance_refs=['third-party-maintenance']))
            if item['type']=='resolves':
                life.mark(item['source'],'superseded',item['reason'],item['evidence'])
                service.sleep_state.mark(item['source'],'superseded',item['reason'],'third-party-maintenance')
    for item in output.get('entity_merges',[]):
        source=service.entity_store.get(item['source']);target=service.entity_store.get(life.canonical(item['target']))
        if source.entity_id==target.entity_id:
            continue
        if source.summary and source.summary != target.summary:
            target.summary = '\n'.join(filter(None,(target.summary,source.summary)))
        target.aliases=list(dict.fromkeys(target.aliases+[source.name]+source.aliases))
        target.directions=list({d.direction_id:d for d in target.directions+source.directions}.values())
        target.provenance_refs=list(dict.fromkeys(target.provenance_refs+source.provenance_refs+['merged:'+source.entity_id]))
        service.entity_store.upsert(target);service.entity_store.save()
        with life.db() as db:
            db.execute('INSERT OR REPLACE INTO aliases VALUES (?,?)',(source.entity_id,target.entity_id))
        patch_members(service,source.entity_id,target.entity_id)
    for item in output.get('extensions',[]):
        group=next(g for g in payload['known_groups'] if g['group_id']==item['group_id'])
        material=life.material(item['group_id'])
        material.pop('change_evidence',None) # An extension is not a new change verdict.
        items={r['id']:r for r in material.get('items',[])}
        items.update({mid:known[mid] for mid in item['ids']})
        material['items']=list(items.values())
        material['group_id']=group['group_id']
        life.propose(group['kind'],group['sources']+[s for mid in item['ids'] for s in known[mid]['sources']],group['reason'],material)
    for item in output.get('proposals',[]):
        life.propose(item['kind'],[s for mid in item['ids'] for s in known[mid]['sources']],item['reason'],
            {'items':[known[mid] for mid in item['ids']],'evidence':item['evidence'],
             'direction':item.get('direction',''),'group_id':item.get('group_id'),
             'change_evidence':item.get('change_evidence',[]),
             'memory_ids':[mid for mid in item['ids'] if known[mid]['kind']=='knowledge'],
             'instruction':'只总结有依据的部分；可不提炼。过时或与当前信息矛盾的部分标低置信度并停止召回。Event材料只提取确定的Entity，可不提取。'})


def legacy_backfill(service, rows, vectors, config):
    """Refresh existing content-search indexes in a named VL vector space."""
    model='vl:'+config.space
    for row in rows:
        if row['kind'] not in ('event','entity') or not index_text(row).strip():
            continue
        store=service.core_vectors if row['kind']=='event' else service.entity_vectors
        old=store.get(row['id'])
        if not old or old['text']!=index_text(row) or old['model']!=model:
            store.upsert(row['id'],index_text(row),vectors[row['id']],model)


def content_query(row,query,legacy,cache):
    """Never compare old and VL vectors, even when dimensions happen to match."""
    model=row.get('model','')
    if not model.startswith('vl:'):
        return legacy
    config=SliceEmbeddingConfig.load(os.environ.get('EMBER_SLICE_EMBED_CONFIG'))
    if model!='vl:'+config.space:
        return None
    if model not in cache:
        cache[model]=SliceEmbeddingClient(config).embed(query)
    return cache[model]


class MemoryMaintenanceWorker:
    def __init__(self,registry):
        self.registry=registry;self.stop_event=asyncio.Event();self.last_backfill=0
        self.config=None

    def process_one(self):
        config=MaintenanceConfig.load(os.environ.get('EMBER_SLICE_MAINTENANCE_CONFIG') or self.config)
        embedding=SliceEmbeddingConfig.load(os.environ.get('EMBER_SLICE_EMBED_CONFIG'))
        for name in self.registry.names:
            service=self.registry.service(name)
            life=Lifecycle(service.store.root,name)
            with service._generation_lock:
                rows=catalog(service)
            vectors={}
            for row in rows:
                digest=fingerprint(row)
                with life.db() as db:
                    old=db.execute('SELECT * FROM vectors WHERE id=?',(row['id'],)).fetchone()
                if not old or old['hash']!=digest or old['space']!=embedding.space:
                    vector=None
                    if row['kind']=='slice':
                        with TurnSliceStore(service.store.root)._db() as db:
                            ready=db.execute("SELECT vector_json FROM slice_embeddings WHERE slice_id=? AND space=? AND content_hash=? AND state='ready'",
                                (row['id'],embedding.space,row['sources'][0]['content_hash'])).fetchone()
                        if ready:
                            vector=json.loads(ready[0])
                    if vector is None:
                        vector=SliceEmbeddingClient(embedding).embed(index_text(row))
                    with life.db() as db:
                        db.execute('INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)',
                            (row['id'],digest,embedding.space,json.dumps(vector)))
                    return True
                vectors[row['id']]=json.loads(old['vector'])
            with service._generation_lock:
                legacy_backfill(service,rows,vectors,embedding)
            merge_exact_duplicates(service, rows)
            active=[r for r in rows if life.canonical(r['id'])==r['id']]
            for row in active:
                if row['kind'] not in ('event','entity'):
                    continue
                nearby=sorted([r for r in active if r['id']!=row['id'] and
                    r['kind'] in ({'event','entity'} if row['kind'] in ('event','entity') else {'slice','knowledge'}) and
                    cosine(vectors[row['id']],vectors[r['id']])>=0.65],
                    key=lambda r:-cosine(vectors[row['id']],vectors[r['id']]))[:config.batch_size]
                payload={'phase':'memory_pool','subject':row['id'],'materials':[row]+nearby,
                    'known_groups':KnowledgeStore(service.store.root,name).groups(False)}
                with life.db() as db:
                    payload['existing_labels']=[dict(r) for r in db.execute('SELECT * FROM eligibility')
                        if r['id'] in {m['id'] for m in payload['materials']}]
                    payload['existing_relations']=[json.loads(r[0]) for r in db.execute('SELECT body FROM relations')]
                if row['kind']=='event':
                    own_ids={s['slice_id'] for s in row['sources']}
                    payload['known_groups']=[g for g in payload['known_groups'] if not any(s['slice_id'] in own_ids for s in g['sources'])]
                    from .maintenance_materials import prepare, MaintenancePending
                    try:
                        payload['event_recall_history']=prepare(service.store.root,name,row['id'])
                    except MaintenancePending:
                        payload['event_recall_history']={'status':'not_available'}
                key=fingerprint([row,nearby])
                with life.db() as db:
                    db.execute('INSERT OR IGNORE INTO tasks(id,body) VALUES (?,?)',(key,json.dumps(payload)))
                    task=db.execute('SELECT * FROM tasks WHERE id=?',(key,)).fetchone()
                if task['state']=='done' or task['retry_at']>time.time():
                    continue
                try:
                    result=validate_result(MaintenanceClient(config).judge('memory_pool',payload),payload)
                    with service._generation_lock:
                        current={r['id']:r for r in catalog(service)}
                        if any(fingerprint(current.get(r['id']))!=fingerprint(r) for r in payload['materials']):
                            raise ValueError('maintenance_source_changed')
                        maintenance = {key: result[key] for key in ('relations','entity_merges') if key in result}
                        maintenance['relations'] = [r for r in maintenance.get('relations', [])
                                                    if r['type'] in ('continues','related_to','contrasts')]
                        apply_result(service,payload,maintenance)
                    with life.db() as db:
                        db.execute("UPDATE tasks SET state='done',error=NULL WHERE id=?",(key,))
                except Exception as exc:
                    with life.db() as db:
                        db.execute("UPDATE tasks SET state='retry',attempts=attempts+1,error=?,retry_at=? WHERE id=?",
                            (type(exc).__name__,time.time()+60,key))
                return True
        return False

    async def run(self):
        while not self.stop_event.is_set():
            try:
                processed=await asyncio.to_thread(self.process_one)
            except Exception:
                processed=False
            try:
                await asyncio.wait_for(self.stop_event.wait(),timeout=0.1 if processed else 5)
            except asyncio.TimeoutError:
                pass
