"""Shared durable cursor for Event-batch and briefing-triggered source pulls."""
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
        with conn:
            yield conn
    finally:
        conn.close()


def request_sync(service):
    with db(service.store.root) as conn:
        conn.execute('INSERT INTO sync(instance,requested) VALUES (?,1) ON CONFLICT(instance) DO UPDATE SET requested=requested+1',
                     (service.source_instance,))


def sync_increment(service):
    """Hold one cursor lease across pull, derived writes and cursor advancement.

    Content-addressed slice upserts make a crash before advancement replay-safe.
    A page is acknowledged only after its vectors and bindings are durable.
    """
    instance=service.source_instance
    if not source_client.configured():
        return {'status':'unavailable','code':'source_unconfigured'}
    try:
        with db(service.store.root) as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT OR IGNORE INTO sync(instance) VALUES (?)',(instance,))
            row=conn.execute('SELECT * FROM sync WHERE instance=?',(instance,)).fetchone()
            cursor=json.loads(row['cursor']) if row['cursor'] else None
            cfg=SliceEmbeddingConfig.load()
            while True:
                page=source_client.fetch_source_increment(cursor,instance=instance,limit=lifecycle_config()['slice']['page_size'])
                store = TurnSliceStore(service.store.root)
                revoked = defaultdict(list)
                for item in page.get('revoked', []):
                    revoked[item['session_id']].append(item['message_id'])
                for session_id, ids in revoked.items():
                    store.revoke(instance, session_id, ids)
                for item in page['turns']:
                    try:
                        store.save(item['session_id'],item['messages'],instance=instance,
                                   user_is_system=item.get('user_is_system',False))
                    except source_client.SourceUnavailable as exc:
                        if exc.code != 'source_revoked':
                            raise
                prepare_slices(service,cfg)
                cursor=page['cursor']
                conn.execute('UPDATE sync SET cursor=?,completed=?,retry_at=0,error=NULL WHERE instance=?',
                             (json.dumps(cursor),row['requested'],instance))
                if not page['has_more']:
                    break
        return {'status':'ok','cursor':cursor}
    except (source_client.SourceUnavailable,SliceEmbeddingError,ValueError,OSError,sqlite3.Error) as exc:
        code=getattr(exc,'code','source_sync_unavailable')
        with db(service.store.root) as conn:
            conn.execute('UPDATE sync SET retry_at=?,error=? WHERE instance=?',
                         (time.time()+lifecycle_config()['slice']['retry_seconds'],code,instance))
        return {'status':'unavailable','code':code}


class SourceSyncWorker:
    def __init__(self,registry):
        self.registry=registry
        self.stop_event=asyncio.Event()

    def process_one(self):
        for name in self.registry.names:
            from .promises import Promises
            Promises(self.registry.service(name)).sweep()
            root=self.registry.dir_of(name)
            if not (Path(root)/'source_sync.sqlite3').exists():
                continue
            with db(root) as conn:
                row=conn.execute('SELECT * FROM sync WHERE instance=?',(name,)).fetchone()
            if row and row['requested']>row['completed'] and row['retry_at']<=time.time():
                sync_increment(self.registry.service(name))
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
