"""Raw-slice automatic recall: fresh query embeddings and complete diagnostic ranks.

Candidates require cosine similarity >= 0.6. Complete rankings and the candidate
pool are durable. The hybrid caller adds reranking and source ownership presentation.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import uuid

from .slice_embedding import SliceEmbeddingConfig, SliceEmbeddingClient, SliceEmbeddingError
from .source_refs import message_seq
from .turn_slices import TurnSliceStore


CANDIDATE_MIN_SCORE = 0.6


class SliceRecall:
    def __init__(self, root, instance='default', config_path=None):
        self.root = Path(root)
        self.instance = instance
        self.path = self.root / 'slice_recall.sqlite3'
        self.config_path = config_path or os.environ.get('EMBER_SLICE_EMBED_CONFIG')

    @contextmanager
    def db(self):
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('''CREATE TABLE IF NOT EXISTS queries (
                trace_id TEXT PRIMARY KEY, instance TEXT NOT NULL,
                metadata_json TEXT NOT NULL, vector_json TEXT,
                rankings_json TEXT NOT NULL, created_at TEXT NOT NULL
            )''')
            with db:
                yield db
        finally:
            db.close()

    def match(self, text, session_id=None, turn=None, anchor_message_id=None, context_start_id=None, purpose='preview',context_message_ids=None):
        if not isinstance(text, str) or not text.strip():
            raise ValueError('A nonempty input is required')
        trace_id = uuid.uuid4().hex
        metadata = {'trace_id': trace_id, 'instance': self.instance,
            'purpose': purpose,
            'session_id': session_id, 'turn': turn,
            'input_hash': hashlib.sha256(text.encode()).hexdigest(),
            'anchor_message_id': message_seq(anchor_message_id),
            'context_start_id': message_seq(context_start_id),
            'status': 'ranked', 'mode': 'diagnostic', 'injected': False,
            'candidate_threshold': CANDIDATE_MIN_SCORE, 'candidate_count': 0,
            'candidate_pool': [], 'presentation_threshold': None,
            'presentation_limit': None, 'ranked_count': 0,
            'filtered_by_window': 0, 'filtered_by_anchor': 0,
            'invalid_vectors': 0, 'selection_reason': 'presentation_rules_not_set'}
        from .lifecycle_config import lifecycle_config
        threshold=lifecycle_config()['slice']['threshold']
        metadata['candidate_threshold']=threshold
        query_vector = None
        ranked = []
        try:
            config = SliceEmbeddingConfig.load(self.config_path)
            metadata.update(model=config.model, space=config.space, query_format='raw-input-v1')
            store = TurnSliceStore(str(self.root))
            rows = []
            if store.path.exists():
                with store._db() as db:
                    rows = db.execute('''SELECT s.slice_id,s.session_id,s.user_message_id,
                        s.assistant_message_id,s.content_hash,s.user_is_system,s.messages_json,
                        e.dimensions,e.vector_json FROM turn_slices s JOIN slice_embeddings e
                        ON s.slice_id=e.slice_id AND s.content_hash=e.content_hash
                        WHERE s.instance=? AND e.space=? AND e.state='ready'
                        ORDER BY s.slice_id''', (self.instance, config.space)).fetchall()
            from .knowledge import KnowledgeStore, knowledge_space
            knowledge_ready = any(not r['pinned'] and r['vector'] and r['space']==knowledge_space(config)
                                  for r in KnowledgeStore(self.root,self.instance).all())
            from .core_semantic import CoreVectorStore
            event_ready=any(r['model']==config.space for r in CoreVectorStore(str(self.root)).all())
            if not event_ready:
                # Themes live in a separate layer; reading them does not construct a service.
                if (self.root/'organization.sqlite3').exists():
                    db=sqlite3.connect(self.root/'organization.sqlite3')
                    try:
                        event_ready=bool(db.execute('SELECT 1 FROM themes WHERE space=? LIMIT 1',(config.space,)).fetchone())
                    finally:
                        db.close()
            if not rows and not knowledge_ready and not event_ready:
                metadata['status'] = 'no_vectors'
            else:
                # Exactly one fresh embedding for this whole input; no old Cue/Direction calls.
                query_vector = SliceEmbeddingClient(config).embed(text)
                from .event_slices import EventSliceLinks
                memberships = {}
                for membership in EventSliceLinks(self.root).all(self.instance):
                    memberships.setdefault(membership['slice_id'], []).append(membership)
                from .slice_maintenance import MaintenanceQueue
                relations = {}
                for relation in MaintenanceQueue(self.root).relations(self.instance):
                    relations.setdefault(relation['old_slice_id'], []).append(relation)
                    if relation['new_slice_id']:
                        relations.setdefault(relation['new_slice_id'], []).append(relation)
                qnorm = math.hypot(*query_vector)
                if not qnorm or not math.isfinite(qnorm):
                    raise SliceEmbeddingError('embedding_response_invalid')
                from .memory_lifecycle import Lifecycle
                eligible=Lifecycle(self.root,self.instance).eligibility_checker()
                from .models import Event
                from .maintenance_materials import events as event_rows
                from .slice_policy import passing_parents, informative
                events={}
                if purpose!='search':
                    for eid,document in event_rows(self.root).items():
                        try:
                            events[eid]=Event.from_dict(document)
                        except (TypeError,ValueError):
                            pass
                excluded = set()
                if purpose == 'automatic':
                    from .proposals import recall_exclusions
                    excluded = recall_exclusions(self.root,self.instance)
                for row in rows:
                    if (row['slice_id'],row['content_hash']) in excluded and not memberships.get(row['slice_id']):
                        continue
                    if not eligible(row['slice_id']):
                        continue
                    messages = json.loads(row['messages_json'])
                    ids = [int(m['message_id']) for m in messages]
                    if row['session_id'] == session_id:
                        anchor = metadata['anchor_message_id']
                        start = metadata['context_start_id']
                        if anchor and max(ids) >= int(anchor):
                            metadata['filtered_by_anchor'] += 1
                            continue
                        if ((context_message_ids is not None and set(ids)<=set(context_message_ids)) or
                                (context_message_ids is None and start and min(ids)>=int(start))):
                            metadata['filtered_by_window'] += 1
                            continue
                    vector = json.loads(row['vector_json'])
                    if (not isinstance(vector, list) or len(vector) != len(query_vector)
                            or row['dimensions'] != len(vector)
                            or any(type(v) not in (float, int) or not math.isfinite(v) for v in vector)):
                        metadata['invalid_vectors'] += 1
                        continue
                    norm = math.hypot(*vector)
                    if not norm or not math.isfinite(norm):
                        metadata['invalid_vectors'] += 1
                        continue
                    score = sum((a / qnorm) * (b / norm) for a, b in zip(query_vector, vector))
                    ranked.append({'slice_id': row['slice_id'], 'instance': self.instance,
                        'session_id': row['session_id'], 'user_message_id': row['user_message_id'],
                        'assistant_message_id': row['assistant_message_id'],
                        'content_hash': row['content_hash'], 'score': max(-1., min(1., score)),
                        'hit_entry': 'raw_slice_vector', 'direct': True,
                        'span_memberships': [m for m in memberships.get(row['slice_id'], []) if m['membership_kind']=='span'],
                        'maintenance_relations': relations.get(row['slice_id'], []),
                        'source_memberships': [m for m in memberships.get(row['slice_id'], []) if m['membership_kind']!='span']})
                if purpose!='search':
                    for item in ranked:
                        parents=passing_parents(self.root,item,query_vector,config,events)
                        item['parent_event_ids']=parents
                        source=store.get(item['slice_id'])
                        item['parent_gate_passed']=bool(parents) or (parents is None and informative(source))
                ranked.sort(key=lambda r: (-r['score'], r['slice_id']))
                for index, item in enumerate(ranked, 1):
                    item['rank'] = index
                    # Compare full precision; displayed rounding never admits a candidate.
                    item['in_candidate_pool'] = item['score'] >= threshold and item.get('parent_gate_passed',True)
                metadata['ranked_count'] = len(ranked)
                metadata['candidate_pool'] = [dict(item) for item in ranked if item['in_candidate_pool']]
                metadata['candidate_count'] = len(metadata['candidate_pool'])
        except SliceEmbeddingError as exc:
            metadata.update(status='unavailable', error_code=str(exc))
        with self.db() as db:
            db.execute('INSERT INTO queries VALUES (?,?,?,?,?,?)',
                (trace_id, self.instance, json.dumps(metadata, ensure_ascii=False),
                 json.dumps(query_vector, allow_nan=False) if query_vector is not None else None,
                 json.dumps(ranked, allow_nan=False), datetime.now(timezone.utc).isoformat()))
        # Hook responses stay compact; full candidate identities are available in the trace.
        return {key: value for key, value in metadata.items() if key != 'candidate_pool'}

    def trace(self, trace_id, include_sources=False):
        if not self.path.exists():
            return None
        with self.db() as db:
            row = db.execute('SELECT * FROM queries WHERE trace_id=? AND instance=?',
                             (trace_id, self.instance)).fetchone()
        if row is None:
            return None
        result = json.loads(row['metadata_json'])
        result['created_at'] = row['created_at']
        result['rankings'] = json.loads(row['rankings_json'])
        if include_sources:
            store = TurnSliceStore(str(self.root))
            for item in result['rankings']:
                source = store.get(item['slice_id'])
                if source and source['instance'] == self.instance and source['content_hash'] == item['content_hash']:
                    item['messages'] = source['messages']
                else:
                    item['source_status'] = 'changed_or_missing'
        return result


def automatic_slice_recall(service, session, turn, text, context_start_id=None,context_message_ids=None):
    """Keep the App hook contract; rankings stay separate from actual injection."""
    try:
        matched = SliceRecall(service.store.root, service.source_instance).match(text,
            session_id=session.session_id, turn=turn,
            anchor_message_id=service.turn_ledger.user_msg.get((session.session_id, turn)),
            context_start_id=context_start_id, purpose='automatic',context_message_ids=context_message_ids)
    except (OSError, sqlite3.Error, ValueError):
        matched = {'status': 'unavailable', 'error_code': 'slice_recall_storage_unavailable',
                   'ranked_count': 0, 'candidate_threshold': CANDIDATE_MIN_SCORE,
                   'candidate_count': 0, 'mode': 'diagnostic', 'injected': False}
    return {'prefix': '', 'presentations': [], 'recall_refs': {'events': []},
        'recalled_count': 0, 'entity_recalled': 0, 'entity_names': [],
        'filtered_by_sleep': 0, 'filtered_by_window': matched.get('filtered_by_window', 0),
        'dropped_by_seats': 0, 'weak_pooled': 0, 'strong_dropped': 0,
        'observe_extra': None, 'resolved_brought': [],
        'semantic_matched': 0, 'semantic_reason': matched['status'],
        'slice_recall': matched}
