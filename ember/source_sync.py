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
    A page is acknowledged after local storage. Deferred vectors are handled by the worker.
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
                        if exc.code not in ('source_revoked', 'source_private'):
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
                sync_increment(service)
                self.next_poll[name]=time.time()+30
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
