"""Durable turn-end queue and background source -> slice -> embedding pipeline."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from . import source_client
from .source_refs import message_seq
from .turn_slices import TurnSliceStore, fetch_and_save_turn
from .slice_embedding import SliceEmbeddingConfig, SliceEmbeddingClient, SliceEmbeddingError, slice_text


class IngestionJobs:
    def __init__(self, root):
        self.path = Path(root) / 'slice_ingestion.sqlite3'

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, instance TEXT NOT NULL, session_id TEXT NOT NULL,
                user_message_id TEXT, assistant_message_id TEXT NOT NULL,
                user_is_system INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                slice_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                claim_token TEXT, error_code TEXT
            )''')
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, instance, session_id, user_id, assistant_id, user_is_system=False):
        user_id = message_seq(user_id) if user_id is not None else None
        assistant_id = message_seq(assistant_id)
        if not instance or not session_id or not assistant_id:
            raise ValueError('Exact source identity required')
        job_id = hashlib.sha256(json.dumps([instance, session_id, user_id, assistant_id]).encode()).hexdigest()
        with self.db() as db:
            db.execute('''INSERT INTO jobs
                (job_id,instance,session_id,user_message_id,assistant_message_id,user_is_system)
                VALUES (?,?,?,?,?,?) ON CONFLICT(job_id) DO NOTHING''',
                (job_id, instance, session_id, user_id, assistant_id, int(user_is_system)))
            row = db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if bool(row['user_is_system']) != bool(user_is_system):
                raise ValueError('Source system flag conflict')
        return {'status': row['state'], 'job_id': job_id}

    def claim(self):
        if not self.path.exists():
            return None
        now = time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT * FROM jobs WHERE
                (state IN ('pending','failed') AND retry_at<=?) OR
                (state='working' AND lease_until<=?) ORDER BY rowid LIMIT 1''', (now, now)).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute("UPDATE jobs SET state='working',attempts=attempts+1,lease_until=?,claim_token=? WHERE job_id=?",
                       (now + 120, token, row['job_id']))
            return dict(row, claim_token=token, attempts=row['attempts'] + 1)

    def status(self, job_id):
        if not self.path.exists():
            return None
        with self.db() as db:
            row = db.execute('''SELECT job_id,state,slice_id,attempts,error_code,retry_at
                FROM jobs WHERE job_id=?''', (job_id,)).fetchone()
            return dict(row) if row else None

    def attach_slice(self, job, slice_id):
        with self.db() as db:
            db.execute('UPDATE jobs SET slice_id=? WHERE job_id=? AND claim_token=?',
                       (slice_id, job['job_id'], job['claim_token']))

    def finish(self, job, error_code=None):
        delay = min(300, 2 ** min(job['attempts'], 8)) if error_code else 0
        with self.db() as db:
            db.execute('''UPDATE jobs SET state=?,error_code=?,retry_at=?,lease_until=0,claim_token=NULL
                WHERE job_id=? AND claim_token=?''',
                ('failed' if error_code else 'ready', error_code, time.time() + delay,
                 job['job_id'], job['claim_token']))


class SlicePipeline:
    def __init__(self, registry, config_path=None):
        self.registry = registry
        self.config_path = Path(config_path or os.environ.get('EMBER_SLICE_EMBED_CONFIG') or
                                Path(__file__).resolve().parents[1] / 'config/slice-embedding.local.json')
        self.stop_event = asyncio.Event()
        self._cursor = 0

    def enqueue(self, service, session_id, user_id, assistant_id, user_is_system=False):
        try:
            return IngestionJobs(service.store.root).enqueue(service.source_instance,
                session_id, user_id, assistant_id, user_is_system)
        except (OSError, sqlite3.Error, ValueError):
            return {'status': 'failed', 'code': 'slice_queue_unavailable'}

    def process_one(self):
        names = self.registry.names
        for _ in names:
            name = names[self._cursor % len(names)]
            self._cursor += 1
            root = self.registry.dir_of(name)
            jobs = IngestionJobs(root)
            job = jobs.claim()
            if job is None:
                continue
            store = TurnSliceStore(root)
            try:
                source = store.get(job['slice_id']) if job['slice_id'] else None
                if source is None:
                    source = fetch_and_save_turn(store, job['session_id'], job['assistant_message_id'],
                        user_message_id=job['user_message_id'], instance=job['instance'],
                        user_is_system=bool(job['user_is_system']))
                    jobs.attach_slice(job, source['slice_id'])
                config = SliceEmbeddingConfig.load(self.config_path)
                if store.embedding(source['slice_id'], config.space) is None:
                    client = SliceEmbeddingClient(config)
                    try:
                        vector = client.embed(slice_text(source))
                    except SliceEmbeddingError as exc:
                        store.save_embedding(source, config.space, None, error_code=str(exc))
                        raise
                    if not store.save_embedding(source, config.space, vector):
                        raise SliceEmbeddingError('slice_changed')
            except source_client.SourceUnavailable as exc:
                jobs.finish(job, exc.code)
            except SliceEmbeddingError as exc:
                jobs.finish(job, str(exc))
            except (OSError, sqlite3.Error, ValueError):
                jobs.finish(job, 'slice_storage_unavailable')
            else:
                jobs.finish(job)
            return True
        return False

    async def run(self):
        while not self.stop_event.is_set():
            try:
                processed = await asyncio.to_thread(self.process_one)
            except Exception:
                processed = False  # Durable lease permits a later recovery; never log private bodies.
            if not processed:
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass


def complete_turn(service, pipeline, session_id, assistant_reply, turn=None,
                  assistant_message_id=None, recall_id=None, status='done', user_is_system=False):
    """Capture exact start identity under the same lock as accepted completion."""
    with service._generation_lock:
        user_id = None
        if recall_id:
            snapshot = service.generations.snapshot(recall_id)
            if snapshot is not None:
                metadata = json.loads(snapshot['metadata_json'])
                user_id = metadata.get('request_context', {}).get('user_message_id')
        else:
            live = service._live_turn
            if live:
                matching = [m for m in live['session'].messages
                            if m.get('turn') == live['turn'] and m.get('role') == 'user']
                if len(matching) == 1:
                    user_id = matching[0].get('_source_message_id')
        result = service.hook_turn_end(session_id, assistant_reply, turn,
            assistant_message_id=assistant_message_id, recall_id=recall_id, status=status)
        if pipeline is not None and result.get('ok') and status == 'done':
            if message_seq(user_id) is None or message_seq(assistant_message_id) is None:
                receipt = {'status': 'failed', 'code': 'source_identity_missing'}
            else:
                receipt = pipeline.enqueue(service, result['session_id'], user_id,
                                           assistant_message_id, user_is_system)
            result = dict(result, slice_ingestion=receipt)
        return result


def complete_run(service, pipeline, body):
    result = service.sidecar_run_finish(body)
    if (pipeline is not None and not result.get('code') and result.get('status') == 'done'
            and result.get('spoke') is True and message_seq(result.get('assistant_message_id'))):
        result = dict(result, slice_ingestion=pipeline.enqueue(service, result['session_id'],
                      None, result['assistant_message_id']))
    return result
