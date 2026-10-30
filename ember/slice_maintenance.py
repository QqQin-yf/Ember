"""Event-triggered third-party maintenance, independent of Sleep and App AI."""
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .event_slices import EventSliceLinks
from .generations import fingerprint
from .maintenance_client import MaintenanceConfig, MaintenanceClient, MaintenanceModelError
from .maintenance_materials import (MaintenancePending, events, event_document, prepare,
                                    stage_one, stage_two)
from .turn_slices import TurnSliceStore


VERDICTS = {'related', 'unrelated', 'uncertain'}


class MaintenanceQueue:
    def __init__(self, root):
        self.root = Path(root)

    @contextmanager
    def db(self):
        with EventSliceLinks(self.root).db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS slice_maintenance_jobs (
                job_id TEXT PRIMARY KEY, instance TEXT NOT NULL, event_id TEXT NOT NULL,
                event_hash TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                data_json TEXT NOT NULL DEFAULT '{}', attempts INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                claim_token TEXT, error_code TEXT);
                CREATE TABLE IF NOT EXISTS slice_maintenance_relations (
                relation_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, instance TEXT NOT NULL,
                scope TEXT NOT NULL, event_id TEXT NOT NULL, event_hash TEXT NOT NULL,
                new_slice_id TEXT, new_slice_hash TEXT, old_slice_id TEXT NOT NULL,
                old_slice_hash TEXT NOT NULL, relationship TEXT NOT NULL, evidence_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS slice_maintenance_observations (
                job_id TEXT NOT NULL, candidate_id TEXT NOT NULL, kind TEXT NOT NULL,
                PRIMARY KEY(job_id,candidate_id));''')
            yield db

    def enqueue(self, event, instance='default'):
        digest = fingerprint(event_document(event))
        job_id = fingerprint([instance, event['event_id'], digest])
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO slice_maintenance_jobs(job_id,instance,event_id,event_hash) VALUES (?,?,?,?)',
                       (job_id, instance, event['event_id'], digest))
        return job_id

    def claim(self):
        if not (self.root / 'turn_slices.sqlite3').exists():
            return None
        now = time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT * FROM slice_maintenance_jobs WHERE
                (state IN ('pending','retry','waiting_source','waiting_config') AND retry_at<=?)
                OR (state='working' AND lease_until<=?) ORDER BY rowid LIMIT 1''', (now, now)).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute("UPDATE slice_maintenance_jobs SET state='working',attempts=attempts+1,lease_until=?,claim_token=? WHERE job_id=?",
                       (now + 120, token, row['job_id']))
            return dict(row, claim_token=token, attempts=row['attempts']+1)

    def save(self, job, state, data, error=None, relations=(), additions=(), observations=()):
        delay = 5 if state in ('waiting_source', 'waiting_config') else min(300, 2 ** min(job['attempts'], 8)) if state == 'retry' else 0
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            owned = db.execute("SELECT 1 FROM slice_maintenance_jobs WHERE job_id=? AND claim_token=? AND state='working'",
                               (job['job_id'], job['claim_token'])).fetchone()
            if not owned:
                return
            for row in relations:
                db.execute('INSERT OR IGNORE INTO slice_maintenance_relations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', row)
            for row in additions:
                db.execute('INSERT OR IGNORE INTO event_slice_additions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', row)
            for candidate_id in observations:
                db.execute('INSERT OR IGNORE INTO slice_maintenance_observations VALUES (?,?,?)',
                           (job['job_id'], candidate_id, 'suspected_false_recall'))
            db.execute('''UPDATE slice_maintenance_jobs SET state=?,data_json=?,error_code=?,retry_at=?,
                lease_until=0,claim_token=NULL WHERE job_id=? AND claim_token=?''',
                (state, json.dumps(data, ensure_ascii=False), error, time.time()+delay, job['job_id'], job['claim_token']))

    def statuses(self):
        if not (self.root / 'turn_slices.sqlite3').exists():
            return []
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT job_id,event_id,state,attempts,error_code FROM slice_maintenance_jobs ORDER BY rowid')]

    def relations(self, instance='default'):
        library, store = events(self.root), TurnSliceStore(str(self.root))
        with self.db() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM slice_maintenance_relations WHERE instance=?', (instance,))]
        valid = []
        for row in rows:
            event, old = library.get(row['event_id']), store.get(row['old_slice_id'])
            new = store.get(row['new_slice_id']) if row['new_slice_id'] else None
            if (event and fingerprint(event_document(event)) == row['event_hash'] and old and
                    old['content_hash'] == row['old_slice_hash'] and
                    (not row['new_slice_id'] or (new and new['content_hash'] == row['new_slice_hash']))):
                row['evidence'] = json.loads(row.pop('evidence_json'))
                valid.append(row)
        return valid


def _contains_evidence(quote, texts):
    return isinstance(quote, str) and bool(quote) and any(quote in text for text in texts)


def _document_texts(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _document_texts(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in ('event_id', 'version_id'):
                yield from _document_texts(item)


def validate_decisions(phase, payload, decisions):
    expected = {r['candidate_id']: r for r in payload['candidates']}
    if (not isinstance(decisions, list) or any(not isinstance(r, dict) for r in decisions) or
            len(decisions) != len(expected) or
            {r.get('candidate_id') for r in decisions} != set(expected)):
        raise MaintenanceModelError('maintenance_decision_ids_invalid')
    for decision in decisions:
        source = expected[decision['candidate_id']]
        entries = [decision] if phase == 'event' else decision.get('turns', [])
        if not isinstance(entries, list) or any(not isinstance(r, dict) for r in entries):
            raise MaintenanceModelError('maintenance_turn_ids_invalid')
        if phase == 'event' and decision.get('action'):
            raise MaintenanceModelError('maintenance_action_invalid')
        if phase == 'turn':
            turns = {r['turn']: r for r in source['new_turns']}
            if len(entries) != len(turns) or {r.get('turn') for r in entries} != set(turns):
                raise MaintenanceModelError('maintenance_turn_ids_invalid')
        for entry in entries:
            if entry.get('verdict') not in VERDICTS:
                raise MaintenanceModelError('maintenance_verdict_invalid')
            if entry['verdict'] != 'related':
                if entry.get('action'):
                    raise MaintenanceModelError('maintenance_action_without_relation')
                continue
            if not isinstance(entry.get('relationship'), str) or not entry['relationship'].strip():
                raise MaintenanceModelError('maintenance_relationship_missing')
            old = entry.get('old_evidence')
            key = 'event_evidence' if phase == 'event' else 'new_evidence'
            new = entry.get(key)
            target_texts = (_document_texts(payload['new_event']) if phase == 'event' else
                            (m['content'] for m in turns[entry['turn']]['messages']))
            if (not _contains_evidence(old, (m['content'] for m in source['messages'])) or
                    not _contains_evidence(new, target_texts)):
                raise MaintenanceModelError('maintenance_evidence_invalid')
            if phase == 'turn':
                if entry.get('action') not in ('link', 'add_source'):
                    raise MaintenanceModelError('maintenance_action_invalid')
                if entry['action'] == 'add_source' and entry.get('target_event_id') not in {
                        r['event']['event_id'] for r in source['possible_event_sources']}:
                    raise MaintenanceModelError('maintenance_source_target_invalid')


class SliceMaintenanceWorker:
    def __init__(self, registry, config_path=None):
        self.registry = registry
        self.config_path = Path(config_path or os.environ.get('EMBER_SLICE_MAINTENANCE_CONFIG') or
                                Path(__file__).resolve().parents[1] / 'config/slice-maintenance.local.json')
        self.stop_event = asyncio.Event()
        self._last_discover = 0

    def discover(self):
        for name in self.registry.names:
            root = self.registry.dir_of(name)
            for event in events(root).values():
                MaintenanceQueue(root).enqueue(event, name)

    def process_one(self):
        if time.time() - self._last_discover > 5:
            self.discover();self._last_discover = time.time()
        for name in self.registry.names:
            root = self.registry.dir_of(name);queue = MaintenanceQueue(root)
            job = queue.claim()
            if job is None:
                continue
            data = json.loads(job['data_json'])
            try:
                event = events(root).get(job['event_id'])
                if event is None or fingerprint(event_document(event)) != job['event_hash']:
                    queue.save(job, 'superseded', data);return True
                if 'material' not in data:
                    data['material'] = prepare(root, name, job['event_id'])
                material = data['material']
                one = data.setdefault('stage_one', {})
                two = data.setdefault('stage_two', {})
                pending_one = [c for c in material['candidates'] if c['candidate_id'] not in one]
                pending_two = [c for c in material['candidates'] if c['candidate_id'] in one
                    and one[c['candidate_id']]['verdict'] != 'related' and c['candidate_id'] not in two]
                if not pending_one and not pending_two:
                    uncertain = any(d['verdict'] == 'uncertain' for d in one.values()) or any(
                        t['verdict'] == 'uncertain' for d in two.values() for t in d['turns'])
                    observations = [cid for cid,d in two.items() if one[cid]['verdict']=='unrelated'
                                    and all(t['verdict']=='unrelated' for t in d['turns'])]
                    queue.save(job, 'done_with_review' if uncertain else 'done', data, observations=observations)
                    return True
                config = MaintenanceConfig.load(self.config_path)
                phase = 'event' if pending_one else 'turn'
                batch = (pending_one or pending_two)[:config.batch_size]
                payload = stage_one(root, material, batch) if phase == 'event' else stage_two(root, material, batch)
                decisions = MaintenanceClient(config).judge(phase, payload)
                validate_decisions(phase, payload, decisions)
                current = events(root)
                if fingerprint(event_document(current[job['event_id']])) != job['event_hash']:
                    raise MaintenancePending('event_changed_during_maintenance')
                relations, additions, completed = [], [], {}
                sources = {c['candidate_id']: c for c in batch}
                inputs = {c['candidate_id']: c for c in payload['candidates']}
                for decision in decisions:
                    cid = decision['candidate_id'];source = sources[cid]
                    old = TurnSliceStore(str(root)).get(cid)
                    if old is None or old['content_hash'] != source['content_hash']:
                        raise MaintenancePending('candidate_source_changed_or_missing')
                    entries = [decision] if phase == 'event' else decision['turns']
                    for entry in entries:
                        if entry['verdict'] != 'related':
                            continue
                        new = None if phase == 'event' else next(r for r in inputs[cid]['new_turns'] if r['turn']==entry['turn'])
                        if new:
                            actual = TurnSliceStore(str(root)).get(new['slice_id'])
                            if actual is None or actual['content_hash'] != new['content_hash']:
                                raise MaintenancePending('trigger_slice_changed')
                        rid = fingerprint([job['job_id'],phase,cid,new['slice_id'] if new else None])
                        evidence = json.dumps(entry, ensure_ascii=False)
                        relations.append((rid,job['job_id'],name,phase,job['event_id'],job['event_hash'],
                            new['slice_id'] if new else None,new['content_hash'] if new else None,
                            cid,source['content_hash'],entry['relationship'],evidence))
                        if entry.get('action') == 'add_source':
                            target = next(r for r in inputs[cid]['possible_event_sources'] if r['event']['event_id']==entry['target_event_id'])
                            target_id = target['event']['event_id']
                            if target_id not in current or fingerprint(event_document(current[target_id])) != target['event_hash']:
                                raise MaintenancePending('target_event_changed')
                            if not any(m['event_id']==target_id and str(m['version_id'])==str(target['event']['version_id'])
                                       for m in EventSliceLinks(root).for_slice(cid, name)):
                                raise MaintenancePending('target_membership_changed')
                            additions.append((rid,name,target_id,str(target['event']['version_id']),target['event_hash'],
                                new['slice_id'],new['content_hash'],entry['turn'],job['event_id'],job['event_hash'],evidence,job['job_id']))
                    completed[cid] = decision
                (one if phase=='event' else two).update(completed)
                queue.save(job, 'pending', data, relations=relations, additions=additions)
            except MaintenancePending as exc:
                queue.save(job, 'waiting_source', data, str(exc))
            except MaintenanceModelError as exc:
                queue.save(job, 'waiting_config' if str(exc)=='maintenance_config_missing' else 'retry', data, str(exc))
            except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
                queue.save(job, 'retry', data, 'maintenance_data_invalid')
            return True
        return False

    async def run(self):
        while not self.stop_event.is_set():
            try:
                processed = await asyncio.to_thread(self.process_one)
            except Exception:
                processed = False
            if not processed:
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass


def enqueue_event_maintenance(service, event):
    return MaintenanceQueue(service.store.root).enqueue(event.to_dict(), service.source_instance)
