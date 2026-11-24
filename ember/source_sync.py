"""Shared durable cursor for Event-batch and periodic background source pulls."""
import asyncio
from collections import defaultdict
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time

from . import source_client
from .lifecycle_config import lifecycle_config
from .slice_embedding import SliceEmbeddingConfig, SliceEmbeddingError
from .slice_policy import prepare_slices
from .turn_slices import TurnSliceStore


@contextmanager
def db(root):
    conn=sqlite3.connect(Path(root)/'source_sync.sqlite3',timeout=120)
    conn.row_factory=sqlite3.Row
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS sync(instance TEXT PRIMARY KEY,cursor TEXT,requested INTEGER DEFAULT 0,completed INTEGER DEFAULT 0,retry_at REAL DEFAULT 0,error TEXT)')
        conn.execute('''CREATE TABLE IF NOT EXISTS sync_failures(
            instance TEXT PRIMARY KEY, phase TEXT NOT NULL, code TEXT NOT NULL,
            http_status INTEGER, attempts INTEGER NOT NULL)''')
        with conn:
            yield conn
    finally:
        conn.close()


def request_sync(service):
    with db(service.store.root) as conn:
        conn.execute('INSERT INTO sync(instance,requested) VALUES (?,1) ON CONFLICT(instance) DO UPDATE SET requested=requested+1',
                     (service.source_instance,))


def source_sync_status(service):
    instance=service.source_instance
    if not source_client.configured():
        return {'status':'unavailable','code':'source_unconfigured'}
    with db(service.store.root) as conn:
        row=conn.execute('SELECT cursor,retry_at FROM sync WHERE instance=?',(instance,)).fetchone()
        failure=conn.execute('SELECT phase,code,http_status,attempts FROM sync_failures WHERE instance=?',
                             (instance,)).fetchone()
    return {'status':'ok','cursor':json.loads(row['cursor']) if row and row['cursor'] else None,
            'retry_at':row['retry_at'] if row else 0,'failure':dict(failure) if failure else None}


def _retry_page(conn, instance, phase, exc):
    code = getattr(exc, 'code', None) or (
        str(exc) if isinstance(exc, SliceEmbeddingError) else 'source_sync_unavailable')
    old = conn.execute('SELECT attempts FROM sync_failures WHERE instance=?', (instance,)).fetchone()
    attempts = (old['attempts'] if old else 0) + 1
    delay = min(300, max(1, lifecycle_config()['slice']['retry_seconds']) * 2 ** min(attempts - 1, 8))
    retry_at = time.time() + delay
    conn.execute('INSERT OR REPLACE INTO sync_failures VALUES (?,?,?,?,?)',
                 (instance, phase, code, getattr(exc, 'http_status', None), attempts))
    conn.execute('UPDATE sync SET retry_at=?,error=? WHERE instance=?', (retry_at, code, instance))
    return {'status': 'unavailable', 'code': code, 'retry_at': retry_at}


def sync_increment(service):
    """Commit each source page before proceeding, so a failed page is safe to retry."""
    instance = service.source_instance
    if not source_client.configured():
        return {'status': 'unavailable', 'code': 'source_unconfigured'}
    while True:
        with db(service.store.root) as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT OR IGNORE INTO sync(instance) VALUES (?)', (instance,))
            row = conn.execute('SELECT * FROM sync WHERE instance=?', (instance,)).fetchone()
            if row['retry_at'] > time.time():
                return {'status': 'unavailable', 'code': row['error'], 'retry_at': row['retry_at']}
            cursor = json.loads(row['cursor']) if row['cursor'] else None
            phase = 'fetch'
            try:
                page = source_client.fetch_source_increment(cursor, instance=instance,
                    limit=lifecycle_config()['slice']['page_size'])
                phase = 'store'
                store = TurnSliceStore(service.store.root)
                revoked = defaultdict(list)
                for item in page.get('revoked', []):
                    revoked[item['session_id']].append(item['message_id'])
                for session_id, ids in revoked.items():
                    store.revoke(instance, session_id, ids)
                for item in page['turns']:
                    try:
                        store.save(item['session_id'], item['messages'], instance=instance,
                                   user_is_system=item.get('user_is_system', False))
                    except source_client.SourceUnavailable as exc:
                        if exc.code not in ('source_revoked', 'source_private'):
                            raise
                phase = 'prepare'
                if store.path.exists():
                    prepare_slices(service, SliceEmbeddingConfig.load())
            except (source_client.SourceUnavailable, SliceEmbeddingError, ValueError, OSError, sqlite3.Error) as exc:
                # Share the lock with writer/extraction requests when recording the retry.
                return _retry_page(conn, instance, phase, exc)
            cursor = page['cursor']
            done = not page['has_more']
            conn.execute('UPDATE sync SET cursor=?,completed=?,retry_at=0,error=NULL WHERE instance=?',
                         (json.dumps(cursor), row['requested'] if done else row['completed'], instance))
            conn.execute('DELETE FROM sync_failures WHERE instance=?', (instance,))
        if done:
            return {'status': 'ok', 'cursor': cursor}


class SourceSyncWorker:
    def __init__(self,registry):
        self.registry=registry
        self.stop_event=asyncio.Event()
        self.next_poll={}
        self.last_instance=None

    def process_one(self):
        names=list(self.registry.names)
        if self.last_instance in names:
            split=names.index(self.last_instance)+1
            names=names[split:]+names[:split]
        for name in names:
            service=self.registry.service(name)
            from .promises import Promises
            Promises(service).sweep()
            root=service.store.root
            now=time.time()
            with db(root) as conn:
                row=conn.execute('SELECT * FROM sync WHERE instance=?',(name,)).fetchone()
            due=now>=self.next_poll.get(name,0)
            requested=row and row['requested']>row['completed']
            if (due or requested) and (not row or row['retry_at']<=now) and source_client.configured():
                result=sync_increment(service)
                self.next_poll[name]=result.get('retry_at') or time.time()+30
                self.last_instance=name
                return True
        return False

    async def run(self):
        while not self.stop_event.is_set():
            try:
                await asyncio.to_thread(self.process_one)
            except Exception:
                pass  # Durable requested/completed counters retain work.
            try:
                await asyncio.wait_for(self.stop_event.wait(),timeout=1)
            except asyncio.TimeoutError:
                pass
