"""Day-level drafts and bounded App review. Drafts never settle source material."""
from datetime import datetime, timedelta
import json
import random
import time
import logging
from zoneinfo import ZoneInfo

from .extraction import Extraction
from .generations import fingerprint
from .maintenance_models import ask
from .slice_embedding import SliceEmbeddingConfig
from .turn_slices import TurnSliceStore

ZONE = ZoneInfo('Asia/Taipei')
TOOLS = {'get_memory_proposals', 'review_memory_proposals', 'read_memory_proposal_sources'}


def tables(db):
    db.execute("CREATE TABLE IF NOT EXISTS proposal_day_retry (instance TEXT, day TEXT, retry_at REAL, PRIMARY KEY(instance,day))")
    db.execute("""CREATE TABLE IF NOT EXISTS memory_proposals (
        id TEXT PRIMARY KEY, instance TEXT, body TEXT, state TEXT, result TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS proposal_days (
        instance TEXT, day TEXT, digest TEXT, PRIMARY KEY(instance,day))""")
    db.execute("""CREATE TABLE IF NOT EXISTS proposal_reviews (
        id TEXT PRIMARY KEY, instance TEXT, ids TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS proposal_recall (
        instance TEXT, slice_id TEXT, content_hash TEXT, label TEXT,
        PRIMARY KEY(instance,slice_id,content_hash))""")


def backfill_source_times(store, instance):
    from . import source_client
    from collections import defaultdict
    if not source_client.configured():
        return
    with store._db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS slice_time_retries (
            slice_id TEXT PRIMARY KEY, retry_at REAL)""")
        rows = db.execute("""SELECT s.slice_id,s.session_id,s.assistant_message_id
            FROM turn_slices s LEFT JOIN slice_source_times t USING(slice_id)
            LEFT JOIN slice_time_retries r USING(slice_id)
            WHERE s.instance=? AND t.slice_id IS NULL AND COALESCE(r.retry_at,0)<=?
            ORDER BY s.saved_at LIMIT 100""",(instance,time.time())).fetchall()
        db.executemany('INSERT OR REPLACE INTO slice_time_retries VALUES (?,?)',
                       [(row['slice_id'],time.time()+3600) for row in rows])
    sessions = defaultdict(list)
    for row in rows:
        sessions[row['session_id']].append(row)
    for session, items in sessions.items():
        timestamps = source_client.fetch_source_timestamps(
            session,list(dict.fromkeys(row['assistant_message_id'] for row in items)),instance=instance)
        with store._db() as db:
            for row in items:
                stamp = timestamps.get(row['assistant_message_id'])
                if stamp is None:
                    continue
                try:
                    occurred = datetime.fromtimestamp(stamp / 1000, ZONE).isoformat()
                except (OverflowError,OSError,ValueError):
                    continue
                db.execute("""INSERT OR IGNORE INTO slice_source_times
                    SELECT slice_id,? FROM turn_slices WHERE slice_id=?""",(occurred,row['slice_id']))
                db.execute('DELETE FROM slice_time_retries WHERE slice_id=?',(row['slice_id'],))


def propose_day(service):
    q = Extraction(service)
    store = TurnSliceStore(q.root)
    if not store.path.exists():
        return False
    backfill_source_times(store,q.instance)
    with store._db() as db:
        times = dict(db.execute('SELECT slice_id,occurred_at FROM slice_source_times'))
    if not times:
        return False
    config = SliceEmbeddingConfig.load()
    # Leave the previous local day open for conversations continuing past midnight.
    cutoff = datetime.now(ZONE).date() - timedelta(days=1)
    days = {}
    # Treat the first turns of the following day as context only. Their source
    # versions participate in the receipt so late arrivals can reopen analysis.
    q.recover_outputs()
    for row in q.candidates(config):
        stamp = times.get(row['slice_id'])
        if not stamp:
            continue
        day = datetime.fromisoformat(stamp).astimezone(ZONE).date()
        if day <= cutoff:
            days.setdefault((day.isoformat(), row['session_id']), []).append(row)
    for (day, session), rows in sorted(days.items()):
        if day >= cutoff.isoformat():
            continue
        rows.sort(key=lambda r: int(r['assistant_message_id']))
        following = (datetime.fromisoformat(day).date() + timedelta(days=1)).isoformat()
        boundary = sorted(days.get((following,session), []),
                          key=lambda r: int(r['assistant_message_id']))[:6]
        key = day + ':' + session
        refs = [{k: row[k] for k in ('slice_id','content_hash','session_id','user_message_id','assistant_message_id')}
                for row in rows]
        digest = fingerprint([refs,[(row['slice_id'],row['content_hash']) for row in boundary]])
        with q.db() as db:
            tables(db)
            prior = db.execute('SELECT digest FROM proposal_days WHERE instance=? AND day=?',
                               (q.instance, key)).fetchone()
            retry = db.execute('SELECT retry_at FROM proposal_day_retry WHERE instance=? AND day=?',
                               (q.instance,key)).fetchone()
        if (prior and prior[0] == digest) or (retry and retry[0] > time.time()):
            continue
        try:
            _propose(q, key, digest, refs, rows, boundary)
        except Exception as exc:
            logging.getLogger(__name__).warning('proposal day deferred: %s', type(exc).__name__)
            with q.db() as db:
                db.execute('INSERT OR REPLACE INTO proposal_day_retry VALUES (?,?,?)',
                           (q.instance,key,time.time()+300))
            continue
        with q.db() as db:
            db.execute('DELETE FROM proposal_day_retry WHERE instance=? AND day=?',(q.instance,key))
        return True
    return False


def _propose(q, day, digest, refs, rows, boundary=()):
    materials = []
    for index, row in enumerate(rows):
        materials.append({'index': index, 'messages': row['messages']})
    instruction = (
        '按日期内原话顺序分析。先为每轮判断原文召回 keep/exclude/unsure。'
        '留下可在类似场景补充的经历、选择、感受或互动。信任、安心、被陪伴等当下感受也可保留。'
        '模型或项目话题中的看法、好奇和感受也可保留。排除纯执行进度和缺关键上下文的原话。'
        '再合并同一件事，只提议有至少三轮独立原话支持的日常 Event。'
        '只有助手明确表达新感悟、理解或认知且用户后续有回应、未反驳，才提议 cognition 或 pattern；'
        '必须标出助手表达轮和用户回应轮，分别给出逐字原话，并确认用户没有反驳。跨日未结束的事不要提议。'
        '以第三方视角客观陈述，准确保留计划、感受和说话人，禁止补事实。不写 cue。')
    if boundary:
        instruction += ' next_day_context 仅供识别跨日延续；若仍在继续，暂缓提议。'
    def material(turns):
        return {'turns':turns,'next_day_context':[row['messages'] for row in boundary]}
    index_schema = {'type':'integer','minimum':0,'maximum':len(rows)-1}
    draft = {'type':'object','required':['kind','summary','content','indices','significance'],
             'properties':{'kind':{'enum':['daily','cognition','pattern']},
             'summary':{'type':'string','minLength':1,'maxLength':300},
             'content':{'type':'string','minLength':1,'maxLength':2000},
             'indices':{'type':'array','minItems':1,'maxItems':100,'uniqueItems':True,'items':index_schema},
             'significance':{'type':'integer','minimum':1,'maximum':10},
             'insight_index':index_schema,'response_index':index_schema,
             'insight_quote':{'type':'string','minLength':1},
             'response_quote':{'type':'string','minLength':1},
             'user_did_not_object':{'type':'boolean'}}}
    labels = {'type':'object','required':['index','label'],'properties':{
        'index':index_schema,'label':{'enum':['keep','exclude','unsure']}}}
    schema = {'type':'object','required':['recall','proposals'],'properties':{
        'recall':{'type':'array','items':labels},'proposals':{'type':'array','items':draft}}}
    # Long days are read in order with a bounded first pass, then consolidated.
    if len(json.dumps(materials,ensure_ascii=False).encode()) > 96000:
        notes = []
        batch = []
        size = 0
        for item in materials:
            cost = len(json.dumps(item,ensure_ascii=False).encode())
            if batch and size + cost > 48000:
                notes.append(ask('slice_groups', instruction + ' 保留跨段待合并的候选和来源编号。',material(batch),schema))
                batch, size = [], 0
            batch.append(item)
            size += cost
        if batch:
            notes.append(ask('slice_groups',instruction,material(batch),schema))
        result = ask('slice_groups',instruction+' 综合这些分段结果，同一件事只保留一个提议。',notes,schema)
        result['recall'] = [label for note in notes for label in note['recall']]
    else:
        result = ask('slice_groups',instruction,material(materials),schema)
    labels_by_index = {item['index']:item['label'] for item in result['recall']}
    if len(result['recall']) != len(rows) or set(labels_by_index) != set(range(len(rows))):
        raise ValueError('proposal_recall_coverage_invalid')
    q.knowledge.read_sources(refs)
    pending = []
    for item in sorted(result['proposals'],key=lambda item:-item['significance']):
        ids = item['indices']
        if item['kind'] == 'daily' and len(ids) < 3:
            continue
        if item['kind'] != 'daily':
            insight, reply = item.get('insight_index', -1), item.get('response_index', -1)
            if (insight not in ids or reply not in ids or insight >= reply
                    or item.get('user_did_not_object') is not True
                    or not item.get('insight_quote') or not item.get('response_quote')
                    or not any(m['role']=='assistant' and item['insight_quote'] in m['content'] for m in rows[insight]['messages'])
                    or not any(m['role']=='user' and item['response_quote'] in m['content'] for m in rows[reply]['messages'])):
                continue
        sources = [refs[i] for i in sorted(ids)]
        body = {k:item[k] for k in ('kind','summary','content','significance')}
        body.update(sources=sources, confidence=5, day=day.split(':',1)[0], origin='gemini',
                    excerpt=rows[min(ids)]['messages'][-1]['content'][:180])
        pid = 'PR-' + fingerprint([q.instance,item['kind'],sources])[:24]
        pending.append((pid,q.instance,json.dumps(body,ensure_ascii=False),'pending',None))
    with q.service._generation_lock, q.db() as db:
        tables(db)
        db.executemany('INSERT OR IGNORE INTO memory_proposals VALUES (?,?,?,?,?)',pending)
        db.executemany('INSERT OR REPLACE INTO proposal_recall VALUES (?,?,?,?)',[
            (q.instance,ref['slice_id'],ref['content_hash'],labels_by_index[i]) for i,ref in enumerate(refs)])
        db.execute('INSERT OR REPLACE INTO proposal_days VALUES (?,?,?)',(q.instance,day,digest))


def _invalidate(q, db):
    from .event_slices import EventSliceLinks, sync_from_files
    sync_from_files(q.root,q.instance)
    store = TurnSliceStore(q.root)
    ranges = {}
    for link in EventSliceLinks(q.root).all(q.instance):
        event = q.service.store._events.get(link['event_id'])
        if not event or 'gemini-proposal' in event.provenance_refs:
            continue
        item = store.get(link['slice_id'])
        if item:
            first = int(item['user_message_id'] or item['assistant_message_id'])
            last = int(item['assistant_message_id'])
            ranges.setdefault(item['session_id'], []).append((first,last))
    knowledge = {row['proposal_id']: row['memory_id'] for row in q.knowledge.all() if row.get('proposal_id')}
    for row in db.execute("SELECT * FROM memory_proposals WHERE instance=? AND state='pending'",(q.instance,)):
        eid = 'EVT-PR-' + row['id'].removeprefix('PR-')
        if eid in q.service.store._events or row['id'] in knowledge:
            eid = knowledge.get(row['id'], eid)
            receipt = {'proposal_id': row['id'], 'memory_id': eid}
            db.execute("UPDATE memory_proposals SET state='accepted',result=? WHERE id=?",
                       (json.dumps(receipt),row['id']))
            continue
        refs = json.loads(row['body'])['sources']
        first = min(int(ref['user_message_id'] or ref['assistant_message_id']) for ref in refs)
        last = max(int(ref['assistant_message_id']) for ref in refs)
        conflict = any(first <= end and start <= last for start,end in ranges.get(refs[0]['session_id'], ()))
        if not conflict:
            try:
                q.knowledge.read_sources(refs)
            except ValueError:
                conflict = True
        if conflict:
            db.execute("UPDATE memory_proposals SET state='withdrawn' WHERE id=?",(row['id'],))


def pending_count(service, session, review_id=None):
    q = Extraction(service)
    with q.db() as db:
        tables(db)
        _invalidate(q,db)
        if review_id:
            return len(_batch(q,db,review_id,session))
        rows = db.execute("SELECT body FROM memory_proposals WHERE instance=? AND state='pending'",(q.instance,))
        return sum(json.loads(row[0])['sources'][0]['session_id'] == session for row in rows)


def _batch(q, db, review_id, session):
    row = db.execute('SELECT ids FROM proposal_reviews WHERE id=? AND instance=?',(review_id,q.instance)).fetchone()
    if not row:
        raise ValueError('unknown_review')
    receipt = json.loads(row[0])
    if receipt['session'] != session:
        raise ValueError('unknown_review')
    ids = receipt['ids']
    pending = []
    for pid in ids:
        item = db.execute("SELECT body FROM memory_proposals WHERE id=? AND state='pending'",(pid,)).fetchone()
        if item:
            pending.append((pid,json.loads(item[0])))
    return pending


def tool_specs():
    def spec(name,desc,props,required):
        return {'type':'function','function':{'name':name,'description':desc,'parameters':{
            'type':'object','properties':props,'required':required,'additionalProperties':False}}}
    return [
        spec('read_memory_proposal_sources','按提议和 offset 查看原话。返回 next_offset 时继续取。',
             {'proposal_id':{'type':'string'},'offset':{'type':'integer','minimum':0}},['proposal_id']),
        spec('get_memory_proposals','读取最多三条待审提议。默认置信度5表示尚未经 App AI 审阅；重要度用于排序。用 review_id 继续本次审阅。',
             {'review_id':{'type':'string'}},[]),
        spec('review_memory_proposals','逐条接受或拒绝。接受保留第三方正文并由你填写 cue、confidence、significance；日常可填 entity 和逐字 focus。拒绝后可自行写。',
             {'review_id':{'type':'string'},'decisions':{'type':'array','minItems':1,'maxItems':3,'items':{
                 'type':'object','required':['proposal_id','action'],'properties':{
                    'proposal_id':{'type':'string'},'action':{'enum':['accept','reject']},
                    'confidence':{'type':'integer','minimum':0,'maximum':10},
                    'significance':{'type':'integer','minimum':1,'maximum':10},
                    'focus':{'type':'string'},'recall_cues':{'type':'array','items':{'type':'string'}},
                    'recall_routes':{'type':'array','items':{'type':'object'}},'entity':{'type':'object'}}}}},
             ['review_id','decisions'])]


def dispatch(service,name,args,context):
    from .tool_validation import validate_tool_input
    if not isinstance(args,dict):
        return {'status':'failed','code':'invalid_arguments'}
    spec = next(s['function'] for s in tool_specs() if s['function']['name']==name)
    error = validate_tool_input(args,spec['parameters'])
    if error:
        return error
    q = Extraction(service)
    try:
        with service._generation_lock:
            return _dispatch(q,name,args,context)
    except (ValueError,TypeError,KeyError) as exc:
        return {'status':'failed','code':str(exc)}


def _dispatch(q,name,args,context):
    from uuid import uuid4
    context = context or q.service._live_turn
    if not context:
        raise ValueError('live_app_generation_required')
    session = context['session'].session_id
    rid = args.get('review_id')
    with q.db() as db:
        tables(db)
        _invalidate(q,db)
        if name == 'read_memory_proposal_sources':
            row = db.execute('SELECT body FROM memory_proposals WHERE instance=? AND id=?',
                             (q.instance,args['proposal_id'])).fetchone()
            if not row:
                raise ValueError('unknown_proposal')
            refs = json.loads(row[0])['sources']
            if refs[0]['session_id'] != session:
                raise ValueError('unknown_proposal')
            rows = q.knowledge.read_sources(refs)
            text = json.dumps([{'slice_id':r['slice_id'],'messages':r['messages']} for r in rows],ensure_ascii=False)
            offset = args.get('offset',0)
            end = min(len(text),offset+4000)
            return {'status':'ok','material':text[offset:end],
                    'next_offset':end if end<len(text) else None}
        if name == 'get_memory_proposals' and not rid:
            active = db.execute('SELECT id FROM proposal_reviews WHERE instance=? AND json_extract(ids,"$.session")=? ORDER BY rowid DESC LIMIT 1',(q.instance,session)).fetchone()
            if active and _batch(q,db,active[0],session):
                rid = active[0]
            else:
                rows = list(db.execute("SELECT id,body FROM memory_proposals WHERE instance=? AND state='pending'",(q.instance,)))
                rows = [row for row in rows if json.loads(row['body'])['sources'][0]['session_id'] == session]
                random.shuffle(rows)
                rows.sort(key=lambda row:-json.loads(row['body'])['significance'])
                rid = 'RV-' + uuid4().hex
                db.execute('INSERT INTO proposal_reviews VALUES (?,?,?)',(rid,q.instance,json.dumps({'session':session,'ids':[r['id'] for r in rows[:10]]})))
                db.executemany("UPDATE memory_proposals SET state='dropped' WHERE id=?",[(r['id'],) for r in rows[10:]])
        pending = _batch(q,db,rid,session)
    if context.get('extraction_scope', {}).get('mode') == 'proposals':
        context['extraction_scope']['review_id'] = rid
    if name == 'get_memory_proposals':
        items = []
        for pid,body in pending[:3]:
            refs = body.pop('sources')
            ids = [s['slice_id'] for s in refs]
            items.append(dict(body,proposal_id=pid,source_count=len(ids),
                              source_range=[refs[0]['user_message_id'] or refs[0]['assistant_message_id'],
                                            refs[-1]['assistant_message_id']]))
        return {'status':'ok','review_id':rid,'items':items,'remaining':len(pending),'done':not pending}
    current = dict(pending[:3])
    saved = []
    rejected = []
    conflicts = []
    failed = []
    seen = set()
    with q.db() as db:
        review = db.execute('SELECT ids FROM proposal_reviews WHERE id=? AND instance=?',(rid,q.instance)).fetchone()
        allowed = set(json.loads(review[0])['ids'])
    for decision in args['decisions']:
        pid = decision['proposal_id']
        if pid not in allowed or pid in seen:
            raise ValueError('invalid_proposal_selection')
        seen.add(pid)
        if pid not in current:
            with q.db() as db:
                prior = db.execute('SELECT state,result FROM memory_proposals WHERE id=?',(pid,)).fetchone()
            if prior['state']=='accepted':
                saved.append(json.loads(prior['result']))
            elif prior['state']=='rejected':
                rejected.append(pid)
            elif prior['state']=='withdrawn':
                conflicts.append(pid)
            else:
                raise ValueError('proposal_not_in_current_batch')
            continue
        if decision['action']=='reject':
            with q.db() as db:
                db.execute("UPDATE memory_proposals SET state='rejected' WHERE id=?",(pid,))
            rejected.append(pid)
            continue
        with q.db() as db:
            _invalidate(q,db)
            state = db.execute('SELECT state FROM memory_proposals WHERE id=?',(pid,)).fetchone()[0]
        if state == 'withdrawn':
            conflicts.append(pid)
            continue
        try:
            result = _accept(q,pid,current[pid],decision,context)
        except (ValueError,TypeError,KeyError) as exc:
            failed.append({'proposal_id':pid,'reason':str(exc)})
            continue
        with q.db() as db:
            db.execute("UPDATE memory_proposals SET state='accepted',result=? WHERE id=?",
                       (json.dumps(result),pid))
        saved.append(result)
    with q.db() as db:
        _invalidate(q,db)
        left=len(_batch(q,db,rid,session))
    return {'status':'ok','review_id':rid,'saved':saved,'rejected':rejected,
            'conflicts':conflicts,'failed':failed,'remaining':left,'done':not left}


def _accept(q,pid,body,decision,context):
    from .knowledge_tools import write_event
    confidence, significance = decision['confidence'],decision['significance']
    sources = body['sources']
    q.knowledge.read_sources(sources)
    ids = [s['slice_id'] for s in sources]
    service = q.service
    context = context or service._live_turn
    if not context:
        raise ValueError('live_app_generation_required')
    if body['kind']=='daily':
        event_id = 'EVT-PR-' + pid.removeprefix('PR-')
        args = {'event':{'kind':'daily','summary':body['summary'],'core':body['content'],
                         'significance':significance,'focus':decision['focus']},
                'source_slice_ids':ids,'recall_routes':decision['recall_routes']}
        if 'entity' in decision:
            args['entity']=decision['entity']
        result=write_event(service,args,dict(context,source_versions=sources,proposal_id=pid,
                            reviewed_event_id=event_id,review_confidence=confidence))
        mid=result['event_id']
    else:
        result=q.knowledge.create(body['kind'],body['content'],decision['recall_cues'],ids,
                                  confidence=confidence,significance=significance,proposal_id=pid,source_versions=sources)
        mid=result['memory_id']
        q.record_sources(sources,mid,'written')
    return {'proposal_id':pid,'memory_id':mid}


def recall_exclusions(root, instance):
    import sqlite3
    from contextlib import closing
    from pathlib import Path
    path = Path(root) / 'extraction.sqlite3'
    if not path.exists():
        return set()
    with closing(sqlite3.connect(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='proposal_recall'").fetchone():
            return set()
        return set(db.execute(
            "SELECT slice_id,content_hash FROM proposal_recall WHERE instance=? AND label='exclude'",(instance,)))
