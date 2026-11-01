"""Durable wakeups kept separate from timeline model-work transactions."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time


@contextmanager
def db(root):
    conn=sqlite3.connect(Path(root)/'relationship_work.sqlite3',timeout=20)
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS work(id INTEGER PRIMARY KEY,revision INTEGER,done INTEGER,retry_at REAL)')
        conn.execute('INSERT OR IGNORE INTO work VALUES (1,0,0,NULL)')
        with conn:
            yield conn
    finally:
        conn.close()


def request(root):
    with db(root) as conn:
        conn.execute('UPDATE work SET revision=revision+1 WHERE id=1')


def ready(root,now=None):
    with db(root) as conn:
        revision,done,retry=conn.execute('SELECT revision,done,retry_at FROM work WHERE id=1').fetchone()
    return revision if revision>done or (retry is not None and retry<=(time.time() if now is None else now)) else None


def defer(root,retry_at):
    if retry_at is None:
        return
    with db(root) as conn:
        conn.execute('UPDATE work SET retry_at=CASE WHEN retry_at IS NULL THEN ? ELSE min(retry_at,?) END WHERE id=1',
                     (retry_at,retry_at))


def finish(root,revision,retry_at=None,started_at=None):
    with db(root) as conn:
        existing=conn.execute('SELECT retry_at FROM work WHERE id=1').fetchone()[0]
        # A refresh may have scheduled a retry while this worker was running.
        # Keep future wakeups conservatively; an obsolete one costs one idle pass.
        if existing is not None and existing>(time.time() if started_at is None else started_at):
            retry_at=existing if retry_at is None else min(existing,retry_at)
        # A write arriving during model work remains dirty.
        conn.execute('UPDATE work SET done=max(done,?),retry_at=? WHERE id=1',(revision,retry_at))
