"""One durable review per direction, shared by online pools and library scans."""
import json
import unicodedata
from .generations import fingerprint


def direction(value):
    return ' '.join(unicodedata.normalize('NFKC',str(value or '')).casefold().split())


def family(kind):
    return kind if kind in ('knowledge_review','entity_extract') else 'source_review'


def combine(left,right):
    result={**left,**right}
    for field,key in [('items','id'),('evidence',None)]:
        merged={}
        for item in left.get(field,[])+right.get(field,[]):
            merged[item[key] if key else fingerprint(item)]=item
        if merged:
            result[field]=list(merged.values())
    result['memory_ids']=sorted(set(left.get('memory_ids',[])+right.get('memory_ids',[])))
    result['change_keys']=sorted(set(left.get('change_keys',[])+right.get('change_keys',[])))
    result['direction']=left.get('direction') or right.get('direction','')
    result['reviewed_versions']={**left.get('reviewed_versions',{}),**right.get('reviewed_versions',{})}
    return result


def submit(life,kind,sources,reason,material):
    from .knowledge import KnowledgeStore
    sources={s['slice_id']:s for s in sources}
    topic=direction(material.get('direction'))
    versions={r['id']:r['document'].get('content_hash') for r in material.get('items',[]) if r.get('kind')=='knowledge'}
    proposed_gid='GR-'+fingerprint([life.instance,kind,topic or sorted(sources),sorted(versions.items())])[:24]
    # Attach both local databases so group membership and typed material commit together.
    with life.db():
        pass
    with KnowledgeStore(life.root,life.instance).db() as db:
        db.execute('ATTACH DATABASE ? AS lifecycle',(str(life.root/'memory_lifecycle.sqlite3'),))
        db.execute('CREATE TABLE IF NOT EXISTS group_updates(id TEXT PRIMARY KEY,instance TEXT,body TEXT)')
        db.execute('BEGIN IMMEDIATE')
        rows=[dict(r) for r in db.execute('''SELECT g.*,m.body FROM memory_groups g
            LEFT JOIN lifecycle.review_materials m ON g.group_id=m.id
            WHERE g.instance=? AND g.state!='merged' ORDER BY g.rowid''',(life.instance,))]
        active=set()
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='sleep_knowledge_runs'").fetchone():
            for row in db.execute("SELECT groups_json FROM sleep_knowledge_runs WHERE instance=? AND state IN ('active','unknown')",(life.instance,)):
                active.update(g['group_id'] for g in json.loads(row[0]))
        matches=[]
        ids=set(material.get('memory_ids',[]))
        for row in rows:
            if family(row['kind'])!=family(kind):
                continue
            body=json.loads(row['body']) if row['body'] else {}
            old_sources=json.loads(row['sources_json'])
            same=(topic and topic==direction(body.get('direction')))
            if kind=='knowledge_review':
                same=bool(ids.intersection(body.get('memory_ids',[])))
            elif not topic:
                same=set(sources)=={s['slice_id'] for s in old_sources}
            if material.get('group_id')==row['group_id'] or row['group_id']==proposed_gid:
                same=True
            if same:
                row['material']=body;row['sources']=old_sources;matches.append(row)
                if kind=='knowledge_review':
                    ids.update(body.get('memory_ids',[]))
        if kind=='knowledge_review':
            for mid,version in versions.items():
                current=db.execute('SELECT content_hash FROM knowledge WHERE memory_id=? AND instance=?',(mid,life.instance)).fetchone()
                alias=db.execute('SELECT 1 FROM lifecycle.aliases WHERE id=?',(mid,)).fetchone()
                if alias or (current and current[0]!=version):
                    return matches[0]['group_id'] if matches else None
        # Follow transitive knowledge overlaps even when older rows are out of order.
        if kind=='knowledge_review':
            changed=True
            while changed:
                changed=False
                for row in rows:
                    if row['kind']!=kind or row in matches:
                        continue
                    body=json.loads(row['body']) if row['body'] else {}
                    if ids.intersection(body.get('memory_ids',[])):
                        row['material']=body;row['sources']=json.loads(row['sources_json'])
                        matches.append(row);ids.update(body['memory_ids']);changed=True
        if any(row['group_id'] in active for row in matches):
            # Never mutate a frozen Sleep group, even if a background scan finishes now.
            pending={'kind':kind,'sources':list(sources.values()),'reason':reason,'material':material}
            db.execute('INSERT OR IGNORE INTO group_updates VALUES (?,?,?)',
                (fingerprint(pending),life.instance,json.dumps(pending,ensure_ascii=False)))
            return matches[0]['group_id']
        if matches:
            primary=next((r for r in matches if r['state']=='pending'),matches[0])
            gid=primary['group_id']
            kind=primary['kind']
            body={}
            for row in matches:
                body=combine(body,row['material'])
                sources={**{s['slice_id']:s for s in row['sources']},**sources}
            reviewed=body.get('reviewed_versions') or {
                r['id']:r['document'].get('content_hash') for r in body.get('items',[]) if r.get('kind')=='knowledge'}
            substantive=(kind=='knowledge_review' and any(reviewed.get(mid)!=ver for mid,ver in versions.items()))
            change_key=None
            if material.get('change_evidence'):
                participants={e['id'] for e in material['change_evidence']}
                change_key=fingerprint(sorted((r['id'],fingerprint(r['document'])) for r in material.get('items',[]) if r['id'] in participants))
            substantive=substantive or bool(change_key and change_key not in body.get('change_keys',[]))
            state='pending' if any(r['state']=='pending' for r in matches) or substantive else primary['state']
            recorded_versions=dict(body.get('reviewed_versions',{}))
            body=combine(body,material)
            body['reviewed_versions']=recorded_versions
            if change_key:
                body['change_keys']=sorted(set(body.get('change_keys',[])+[change_key]))
            for row in matches:
                if row['group_id']!=gid:
                    db.execute("UPDATE memory_groups SET state='merged' WHERE group_id=?",(row['group_id'],))
                    old=dict(row['material'],merged_into=gid)
                    db.execute('INSERT OR REPLACE INTO lifecycle.review_materials VALUES (?,?)',(row['group_id'],json.dumps(old)))
        else:
            gid=proposed_gid
            body=material;state='pending'
            if material.get('change_evidence'):
                participants={e['id'] for e in material['change_evidence']}
                body=dict(body,change_keys=[fingerprint(sorted((r['id'],fingerprint(r['document'])) for r in material.get('items',[]) if r['id'] in participants))])
        body=dict(body)
        body.pop('group_id',None)
        db.execute('INSERT OR REPLACE INTO lifecycle.review_materials VALUES (?,?)',(gid,json.dumps(body,ensure_ascii=False)))
        db.execute('''INSERT INTO memory_groups(group_id,instance,kind,sources_json,reason,state) VALUES (?,?,?,?,?,?)
            ON CONFLICT(group_id) DO UPDATE SET sources_json=excluded.sources_json,state=excluded.state''',
            (gid,life.instance,kind,json.dumps(sorted(sources.values(),key=lambda s:s['slice_id'])),reason,state))
    return gid


def consolidate(life):
    from .knowledge import KnowledgeStore
    store=KnowledgeStore(life.root,life.instance)
    with store.db() as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='sleep_knowledge_runs'").fetchone() and db.execute(
                "SELECT 1 FROM sleep_knowledge_runs WHERE instance=? AND state IN ('active','unknown')",(life.instance,)).fetchone():
            return
        updates=[dict(r) for r in db.execute('SELECT * FROM group_updates WHERE instance=?',(life.instance,))] if db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='group_updates'").fetchone() else []
    for update in updates:
        p=json.loads(update['body'])
        submit(life,p['kind'],p['sources'],p['reason'],p['material'])
        with store.db() as db:
            db.execute('DELETE FROM group_updates WHERE id=?',(update['id'],))
    for group in store.groups():
        submit(life,group['kind'],group['sources'],group['reason'],group.get('material',{}))
