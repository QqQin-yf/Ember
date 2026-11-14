"""Three slice classes and two independent similarity gates; no LLM labels."""
import json
import re
from contextlib import closing

from .core_semantic import CoreVectorStore, cosine
from .event_slices import EventSliceLinks, sync_from_files
from .lifecycle_config import lifecycle_config
from .memory_lifecycle import Lifecycle
from .slice_embedding import SliceEmbeddingClient, slice_text
from .turn_slices import TurnSliceStore


from .slice_prefilter import courtesy_turn


def informative(source, config=None):
    if courtesy_turn(source):
        return False
    cfg=(config or lifecycle_config())['slice']
    texts=[re.sub(r'[\W_]+','',m['content']).casefold() for m in source['messages']]
    fillers={re.sub(r'[\W_]+','',s).casefold() for s in cfg['filler_texts']}
    return any(len(t)>=cfg['min_chars'] and t not in fillers for t in texts)


def parent_allowed(event, root):
    from .pin_store import PinStore
    if PinStore(str(root)).is_pinned(event.event_id):
        return False
    if event.private or event.kind not in ('daily','promise'):
        return False
    if event.kind=='promise' and event.significance<=lifecycle_config()['promise']['low_score_max'] and event.status!='open':
        return False
    if Lifecycle(root).eligible(event.event_id):
        return True
    if event.kind=='daily':
        with Lifecycle(root).db() as conn:
            row=conn.execute("SELECT reason FROM eligibility WHERE id=?",(event.event_id,)).fetchone()
        if row and row[0]=='daily_aggregated':
            import sqlite3
            from pathlib import Path
            path=Path(root)/'organization.sqlite3'
            if path.exists():
                with closing(sqlite3.connect(path)) as conn:
                    return conn.execute("SELECT 1 FROM replacements WHERE event_id=?",(event.event_id,)).fetchone() is not None
    return False


def passing_parents(root, candidate, query, config, events):
    """None means orphan; [] means owned but no eligible parent passed."""
    owners={m['event_id'] for m in candidate.get('span_memberships',[])+candidate.get('source_memberships',[])}
    if not owners:
        return None
    vectors=CoreVectorStore(str(root))
    result=[]
    for eid in sorted({Lifecycle(root).canonical(eid) for eid in owners}):
        ev=events.get(eid);row=vectors.get(eid)
        if not ev or not row or not parent_allowed(ev,root):
            continue
        if row['model']!=config.space or row['text']!=ev.core or len(row['vector'])!=len(query):
            continue
        if cosine(query,row['vector'])>=lifecycle_config()['slice']['parent_threshold']:
            result.append(eid)
    return result


def prepare_slices(service,config):
    """Bindings are range-derived, including old orphans newly covered by Events."""
    root=service.store.root
    sync_from_files(root,service.source_instance)
    links=EventSliceLinks(root).all(service.source_instance)
    owned={m['slice_id'] for m in links}
    store=TurnSliceStore(root)
    if not store.path.exists():
        return
    cfg=lifecycle_config()['slice'];client=SliceEmbeddingClient(config)
    cores=CoreVectorStore(root)
    from .privacy import Privacy, PrivateMaterial, seconds_until_eligible
    privacy = Privacy(root)
    for ev in service.store.all_events():
        if privacy.event_blocked(ev.to_dict(), service.source_instance):
            continue
        row=cores.get(ev.event_id)
        if not row or row['text']!=ev.core or row['model']!=config.space:
            try:
                vector = client.embed(ev.core, source_root=root,
                    sources=privacy.event_sources(ev.event_id, service.source_instance), private=ev.private)
            except PrivateMaterial:
                continue
            cores.upsert(ev.event_id, ev.core, vector, config.space)
    # Prototypes share the exact embedding namespace, never a different model.
    life=Lifecycle(root,service.source_instance)
    prototypes=[]
    from .generations import fingerprint
    for text in cfg['filler_texts']:
        key='filler:'+fingerprint(text)
        with life.db() as db:
            row=db.execute('SELECT vector FROM vectors WHERE id=? AND space=?',(key,config.space)).fetchone()
        if row:
            vector=json.loads(row[0])
        else:
            vector=client.embed(text, static_input=True)
            with life.db() as db:
                db.execute('INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)',
                           (key,fingerprint(text),config.space,json.dumps(vector)))
        prototypes.append(vector)
    for source in store.for_instance(service.source_instance, privacy):
        if seconds_until_eligible(source):
            continue
        sid = source['slice_id']
        keep=sid in owned or informative(source)
        if keep:
            ready=store.embedding(sid,config.space,policy=privacy)
            try:
                vector = ready['vector'] if ready else client.embed(
                    slice_text(source), source_root=root, sources=[source], delayed=True)
            except PrivateMaterial:
                continue
            keep=sid in owned or not any(len(p)==len(vector) and cosine(p,vector)>=cfg['filler_threshold'] for p in prototypes)
            if keep and not ready:
                store.save_embedding(source,config.space,vector)
        if not keep:
            with store._db() as db:
                db.execute('DELETE FROM slice_embeddings WHERE slice_id=?',(sid,))
            life.mark(sid,'no_recall','uninformative_slice',['deterministic_rules_and_filler_vectors'])
        else:
            # Only reverse our own information filter, never an explicit correction.
            with life.db() as db:
                db.execute("DELETE FROM eligibility WHERE id=? AND reason='uninformative_slice'",(sid,))
