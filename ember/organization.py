"""One vector + LLM grouping path for themes and concrete Entity objects."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

from .core_semantic import CoreVectorStore, EntitySummaryVectorStore, cosine
from .entity import Entity
from .generations import fingerprint
from .lifecycle_config import lifecycle_config
from .maintenance_models import ask, prose
from .memory_lifecycle import Lifecycle
from .slice_embedding import SliceEmbeddingConfig, SliceEmbeddingClient


def document(event):
    return {key:getattr(event,key) for key in ('event_id','event_time','summary','core','kind','significance','status','due')}


class Organization:
    def __init__(self,service):
        self.service=service
        self.root=Path(service.store.root)

    @contextmanager
    def db(self):
        conn=sqlite3.connect(self.root/'organization.sqlite3',timeout=20)
        conn.row_factory=sqlite3.Row
        try:
            conn.executescript('''CREATE TABLE IF NOT EXISTS processed(event_id TEXT PRIMARY KEY,hash TEXT,error TEXT,retry_at REAL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS pool(origin TEXT,event_id TEXT,body TEXT,PRIMARY KEY(origin,event_id));
                CREATE TABLE IF NOT EXISTS themes(id TEXT PRIMARY KEY,origin TEXT,name TEXT,text TEXT,members TEXT,vector TEXT,space TEXT);
                CREATE TABLE IF NOT EXISTS replacements(event_id TEXT PRIMARY KEY,theme_id TEXT);
                CREATE TABLE IF NOT EXISTS changed_entities(entity_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS entity_updates(entity_id TEXT PRIMARY KEY, hash TEXT);
                CREATE TABLE IF NOT EXISTS entity_links(entity_id TEXT,event_id TEXT,hash TEXT,
                    PRIMARY KEY(entity_id,event_id));''')
            with conn:
                yield conn
        finally:
            conn.close()

    def themes(self):
        with self.db() as conn:
            return [dict(r,members=json.loads(r['members']),vector=json.loads(r['vector'])) for r in conn.execute('SELECT * FROM themes')]

    def replacement(self,eid):
        with self.db() as conn:
            row=conn.execute('SELECT t.* FROM themes t JOIN replacements r ON r.theme_id=t.id WHERE r.event_id=?',(eid,)).fetchone()
        return dict(row) if row else None

    def vector(self,event,config):
        store=CoreVectorStore(str(self.root));old=store.get(event.event_id)
        if old and old['text']==event.core and old['model']==config.space:
            return old['vector']
        vector=SliceEmbeddingClient(config).embed(event.core)
        store.upsert(event.event_id,event.core,vector,config.space)
        return vector

    def copy_pool(self,event):
        low=event.significance<=lifecycle_config()['promise']['low_score_max']
        origin=('daily' if low and event.kind=='daily' else
                'promise' if low and event.kind=='promise' and event.status!='open' else None)
        if origin:
            with self.db() as conn:
                conn.execute('INSERT OR IGNORE INTO pool VALUES (?,?,?)',(origin,event.event_id,json.dumps(document(event),ensure_ascii=False)))
        return origin

    def summarize_theme(self,group,origin,config):
        known={t['id']:t for t in self.themes()}
        old=known.get(group.get('theme_id')) or next((t for t in known.values() if t['origin']==origin and t['name']==group['name']),None)
        members=set(group['event_ids']) | set(old['members'] if old else [])
        with self.db() as conn:
            pool={r['event_id']:json.loads(r['body']) for r in conn.execute('SELECT * FROM pool WHERE origin=?',(origin,))}
        if not members<=pool.keys() or len(members)<lifecycle_config()['grouping']['minimum']:
            return
        material=[pool[eid] for eid in sorted(members)]
        text=prose('theme_summary','根据全部副本客观概括这个主题。不能写成当事人自己的认知或规律。',material)
        label='来自较早的日常事件的概括' if origin=='daily' else '由第三方根据以前的约定客观总结'
        text=label+'：'+text
        vector=SliceEmbeddingClient(config).embed(text)
        tid=old['id'] if old else 'THM-'+fingerprint([origin,group['name'],sorted(members)])[:16]
        with self.db() as conn:
            conn.execute('INSERT OR REPLACE INTO themes VALUES (?,?,?,?,?,?,?)',
                         (tid,origin,group['name'],text,json.dumps(sorted(members)),json.dumps(vector),config.space))
            if origin=='daily':
                conn.executemany('INSERT OR REPLACE INTO replacements VALUES (?,?)',[(eid,tid) for eid in members])
        if origin=='daily':
            for eid in members:
                Lifecycle(self.root).mark(eid,'no_recall','daily_aggregated',[tid])

    def entity_candidates(self,name,aliases,config,exclude=None):
        client=SliceEmbeddingClient(config)
        vector=client.embed('\n'.join([name,*aliases]))
        cards=[]
        names = {s.strip().casefold() for s in (name, *aliases)}
        life=Lifecycle(self.root)
        eligible=life.eligibility_checker()
        for card in self.service.entity_store.all():
            if card.entity_id==exclude or not eligible(card.entity_id):
                continue
            row=self.entity_vector(card,config)
            exact = any(s.strip().casefold() in names for s in (card.name, *card.aliases))
            if exact or (len(vector)==len(row['vector']) and cosine(vector,row['vector'])>=lifecycle_config()['grouping']['threshold']):
                cards.append(card)
        return cards

    def entity_vector(self, card, config):
        store = EntitySummaryVectorStore(str(self.root))
        text = '\n'.join([card.name, *card.aliases, card.summary])
        row = store.get(card.entity_id)
        if not row or row['text'] != text or row['model'] != config.space:
            vector = SliceEmbeddingClient(config).embed(text)
            store.upsert(card.entity_id, text, vector, config.space)
            row = store.get(card.entity_id)
        return row

    def identify(self,group,cards,*,require_distinct=False):
        if not cards:
            return None, True
        result=ask('entity_identity','只有确定同一具体对象才给 same_id。确认是不同的具体对象才填 distinct=true，拿不准填 false。主题相似不算同一对象。',
                   {'object':group,'existing':[e.to_dict() for e in cards]},
                   {'type':'object','required':['same_id'],'properties':{'same_id':{'type':['string','null']},
                    'distinct':{'type':'boolean'}}})
        if result['same_id'] is not None and result['same_id'] not in {e.entity_id for e in cards}:
            raise ValueError('unknown_entity_identity')
        return result['same_id'], result.get('distinct', False) or not require_distinct

    def attach(self,group,config,*,entity_id=None,require_distinct=False):
        eid = entity_id
        if eid is None:
            cards=self.entity_candidates(group['name'],group.get('aliases',[]),config)
            eid, distinct=self.identify(group,cards,require_distinct=require_distinct)
            if not eid and not distinct:
                return None
        eid=eid or 'ENT-'+fingerprint([group['name'],sorted(group['event_ids'])])[:16]
        card=next((e for e in self.service.entity_store.all() if e.entity_id==eid),None)
        new_card = card is None
        card=Entity.from_dict(card.to_dict()) if card else Entity(eid,group['name'],entity_type=group.get('entity_type','object'),created_at=datetime.now(timezone.utc).isoformat())
        events=[e for e in self.service.store.all_events() if eid in e.entity_refs or e.event_id in group['event_ids']]
        project=card.entity_type=='project' or any(e.kind=='project' for e in events)
        old=card.summary
        evidence={e.event_id:fingerprint(document(e)) for e in events}
        aliases = list(dict.fromkeys(card.aliases+group.get('aliases',[])+([group['name']] if card.name!=group['name'] else [])))
        update_hash = fingerprint([evidence, aliases, project, old])
        with self.db() as conn:
            receipt = conn.execute('SELECT hash FROM entity_updates WHERE entity_id=?',(eid,)).fetchone()
        if receipt and receipt['hash'] == update_hash and all(eid in e.entity_refs for e in events):
            self.entity_vector(card, config)
            self.link_events(eid, group['event_ids'])
            if project:
                self.project_supersession(events)
            return eid
        text=prose('entity_summary',
                   '根据相关事件客观整理。项目只写当前有效现状，新事实取代旧状态。' if project else
                   '根据相关事件客观整理，只返回新知道的补充；不要删除、重写或否定已有内容，没有新增则返回空格。',
                   {'existing':old,'events':[document(e) for e in events]})
        text=('根据相关事件客观整理：'+text if project or not old else
              old+'\n'+text.strip() if text.strip() and not old.endswith(text.strip()) else old)
        card.summary=text;card.summary_at=datetime.now(timezone.utc).isoformat()
        card.aliases=aliases
        with self.service._generation_lock:
            current=next((e for e in self.service.entity_store.all() if e.entity_id==eid),None)
            if current and current.summary!=old:
                raise ValueError('entity_changed_during_summary')
            if any(fingerprint(document(self.service.store.get_event(mid)))!=digest for mid,digest in evidence.items()):
                raise ValueError('event_changed_during_summary')
            self.service.entity_store.upsert(card);self.service.entity_store.save()
            for event in events:
                if eid not in event.entity_refs:
                    event.entity_refs=event.entity_refs+[eid]
                    self.service.store.write_event(event)
            with self.db() as conn:
                conn.execute('INSERT OR REPLACE INTO entity_updates VALUES (?,?)',
                             (eid, fingerprint([evidence, aliases, project, text])))
                if new_card:
                    conn.execute('INSERT OR IGNORE INTO changed_entities VALUES (?)',(eid,))
        self.entity_vector(card, config)
        self.link_events(eid, group['event_ids'])
        if project:
            self.project_supersession(events)
        return eid

    def link_events(self, entity_id, subjects):
        from .promises import instant
        from .relation import EventRelation

        life = Lifecycle(self.root)
        eligible = life.eligibility_checker()
        events = []
        for event in self.service.store.all_events():
            if (event.kind == 'relationship' or not eligible(event.event_id)
                    or entity_id not in event.entity_refs):
                continue
            try:
                events.append((instant(event.event_time), event.event_id, event))
            except (TypeError, ValueError, AttributeError):
                continue
        events.sort()
        limit = lifecycle_config()['grouping']['batch_size']
        for index, (_, event_id, event) in enumerate(events):
            if event_id not in subjects:
                continue
            window = events[max(0, index-limit):index+limit+1]
            material = [document(item) for _, _, item in window]
            digest = fingerprint(material)
            with self.db() as conn:
                receipt = conn.execute('SELECT hash FROM entity_links WHERE entity_id=? AND event_id=?',
                                       (entity_id, event_id)).fetchone()
            if receipt and receipt['hash'] == digest:
                continue
            order = {eid: stamp for stamp, eid, _ in window}
            result = ask('relationship_links',
                         '同一 Entity 的历史按发生时间排列。只连中心事件和具体延续或回应它的事件。'
                         'source 必须早于 target。相邻、同属项目或主题相似不算关系。可不连，不判断取代。',
                         {'subject': event_id, 'events': material},
                         {'type':'object','required':['relations'],'properties':{'relations':{'type':'array','items':{
                             'type':'object','required':['source','target','type'],'properties':{
                                 'source':{'type':'string'},'target':{'type':'string'},
                                 'type':{'enum':['continues','responds_to']}}}}}}) if len(window)>1 else {'relations':[]}
            for edge in result['relations']:
                source, target = edge['source'], edge['target']
                if (event_id not in (source,target) or source not in order or target not in order
                        or order[source] >= order[target]):
                    raise ValueError('entity_link_outside_history')
            with self.service._generation_lock:
                for previous in material:
                    latest = self.service.store.get_event(previous['event_id'])
                    if document(latest) != previous or entity_id not in latest.entity_refs:
                        raise ValueError('entity_history_changed_during_linking')
                existing = {(r.src_event,r.dst_event,r.type)
                            for r in self.service.relation_store.all() if r.status=='active'}
                for edge in result['relations']:
                    key = (edge['source'],edge['target'],edge['type'])
                    if key not in existing:
                        rid = 'rel_'+fingerprint(key)[:16]
                        self.service.relation_store.add(EventRelation(rid,*key,provenance_refs=['entity-history']))
                        existing.add(key)
                with self.db() as conn:
                    conn.execute('INSERT OR REPLACE INTO entity_links VALUES (?,?,?)',(entity_id,event_id,digest))

    def project_supersession(self,events):
        rows=[document(e) for e in events if e.kind=='project']
        if len(rows)<lifecycle_config()['grouping']['minimum']:
            return
        result=ask('memory_group','只列明确被新的项目事实取代的旧 Event；事情完成后的过时过程状态也退出自动召回。保留可主动检索的原件。',rows,
                   {'type':'object','required':['superseded'],'properties':{'superseded':{'type':'array','items':{
                       'type':'object','required':['old','new'],'properties':{'old':{'type':'string'},'new':{'type':'string'}}}}}})
        by_id={e.event_id:e for e in events}
        from .promises import instant
        for pair in result['superseded']:
            old,new=by_id[pair['old']],by_id[pair['new']]
            if old.kind!='project' or new.kind!='project' or instant(old.event_time)>=instant(new.event_time):
                raise ValueError('invalid_project_supersession')
        for pair in result['superseded']:
            Lifecycle(self.root).mark(pair['old'],'superseded','project_new_fact',[pair['new']])
            self.service.sleep_state.mark(pair['old'],'superseded','project_new_fact','organization')

    def merge_changed(self,config):
        with self.db() as conn:
            ids=[r[0] for r in conn.execute('SELECT entity_id FROM changed_entities')]
        for eid in ids:
            primary_id = Lifecycle(self.root).canonical(eid)
            if primary_id != eid:
                self.entity_vector(self.service.entity_store.get(primary_id), config)
                self.link_events(primary_id, [e.event_id for e in self.service.store.all_events()
                                              if primary_id in e.entity_refs])
                with self.db() as conn:
                    conn.execute('DELETE FROM changed_entities WHERE entity_id=?',(eid,))
                continue
            card=self.service.entity_store.get(eid)
            cards=self.entity_candidates(card.name,card.aliases,config,exclude=eid)
            target,_=self.identify(card.to_dict(),cards)
            if target:
                order={e.entity_id:i for i,e in enumerate(self.service.entity_store.all())}
                primary,absorbed=sorted([card,self.service.entity_store.get(target)],key=lambda e:(e.created_at or '',order[e.entity_id]))
                text=prose('entity_summary','合并确定同一对象的卡片。只综合改写原有内容，逐句有据，保留原意、归属、时间、条件、例外与分歧；禁止增加原文未表达的事实、推断或说法。',
                           [primary.to_dict(),absorbed.to_dict()])
                primary.summary='根据相关事件客观整理：'+text
                primary.aliases=list(dict.fromkeys(primary.aliases+absorbed.aliases+[absorbed.name]))
                with self.service._generation_lock:
                    for event in self.service.store.all_events():
                        if absorbed.entity_id in event.entity_refs:
                            event.entity_refs=list(dict.fromkeys(primary.entity_id if i==absorbed.entity_id else i for i in event.entity_refs))
                            self.service.store.write_event(event)
                    self.service.entity_store.save()
                    with Lifecycle(self.root).db() as conn:
                        conn.execute('INSERT OR REPLACE INTO aliases VALUES (?,?)',(absorbed.entity_id,primary.entity_id))
                    with self.db() as conn:
                        conn.execute('INSERT OR IGNORE INTO changed_entities VALUES (?)',(primary.entity_id,))
                self.entity_vector(primary, config)
                self.link_events(primary.entity_id, [e.event_id for e in self.service.store.all_events()
                                                     if primary.entity_id in e.entity_refs])
            with self.db() as conn:
                conn.execute('DELETE FROM changed_entities WHERE entity_id=?',(eid,))

    def process_one(self):
        for event in self.service.store.all_events():
            self.copy_pool(event)
        try:
            config=SliceEmbeddingConfig.load()
        except Exception as exc:
            with self.db() as conn:
                for event in self.service.store.all_events():
                    if event.kind!='relationship':
                        conn.execute('INSERT OR REPLACE INTO processed VALUES (?,?,?,?)',
                            (event.event_id,fingerprint(document(event)),type(exc).__name__,time.time()+lifecycle_config()['slice']['retry_seconds']))
            return False
        self.merge_changed(config)
        for event in self.service.store.all_events():
            if event.kind=='relationship' or Lifecycle(self.root).canonical(event.event_id)!=event.event_id:
                continue
            original=document(event)
            decision = event.entity_decision or {}
            digest=fingerprint([original,decision]) if decision else fingerprint(original)
            with self.db() as conn:
                row=conn.execute('SELECT * FROM processed WHERE event_id=?',(event.event_id,)).fetchone()
            if row and row['hash']==digest and not row['error']:
                continue
            if row and row['retry_at']>time.time():
                continue
            try:
                for route in self.service.store.all_routes():
                    if route.event_id==event.event_id and route.status=='active':
                        with Lifecycle(self.root).db() as conn:
                            old=conn.execute('SELECT * FROM vectors WHERE id=?',('cue:'+route.route_id,)).fetchone()
                        if not old or old['hash']!=route.cue or old['space']!=config.space:
                            cue_vector=SliceEmbeddingClient(config).embed(route.cue)
                            with Lifecycle(self.root).db() as conn:
                                conn.execute('INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)',
                                    ('cue:'+route.route_id,route.cue,config.space,json.dumps(cue_vector)))
                origin=self.copy_pool(event)
                vector=self.vector(event,config)
                state = decision.get('state')
                refs = list(dict.fromkeys(Lifecycle(self.root).canonical(eid) for eid in event.entity_refs))
                for eid in refs:
                    card = self.service.entity_store.get(eid)
                    self.attach({'name':card.name,'event_ids':[event.event_id]},config,entity_id=eid)
                if state == 'proposed' and not refs:
                    life = Lifecycle(self.root)
                    name = decision['name'].strip()
                    matches = [card for card in self.service.entity_store.all()
                               if life.eligible(card.entity_id)
                               and name.casefold() in {value.strip().casefold()
                                                      for value in [card.name,*card.aliases]}]
                    if not matches or (len(matches) == 1 and
                            decision.get('type',matches[0].entity_type) == matches[0].entity_type):
                        eid = matches[0].entity_id if matches else 'ENT-'+fingerprint([name,[event.event_id]])[:16]
                        self.attach({'name':name,'entity_type':decision.get('type','object'),
                                     'event_ids':[event.event_id],'evidence':original},config,entity_id=eid)
                if not origin:
                    with self.db() as conn:
                        conn.execute('INSERT OR REPLACE INTO processed VALUES (?,?,NULL,0)',(event.event_id,digest))
                    continue
                nearby=[]
                for other in self.service.store.all_events():
                    if other.kind!='relationship' and other.event_id!=event.event_id:
                        ov=self.vector(other,config)
                        if len(ov)==len(vector) and cosine(vector,ov)>=lifecycle_config()['grouping']['threshold']:
                            nearby.append(other)
                nearby=nearby[:lifecycle_config()['grouping']['batch_size']]
                evidence={e.event_id:fingerprint(document(e)) for e in [event,*nearby]}
                result=ask('memory_group','只判断是否属于同一主题。保留事实、说话人、条件、例外和分歧；不得增加材料未表达的内容。',
                           {'subject':original,'entity_hint':decision.get('name'),'nearby':[document(e) for e in nearby],
                            'themes':[{k:v for k,v in t.items() if k not in ('vector','space')} for t in self.themes()]},
                           {'type':'object','required':['groups'],'properties':{'groups':{'type':'array','items':{
                               'type':'object','required':['category','name','event_ids'],'properties':{
                                   'category':{'enum':['theme']},'name':{'type':'string','minLength':1},
                                   'event_ids':{'type':'array','minItems':1,'uniqueItems':True,'items':{'type':'string'}},
                                   'aliases':{'type':'array','items':{'type':'string'}},'theme_id':{'type':['string','null']},
                                   'entity_type':{'enum':['project','person','animal','place','object']}}}}}})
                allowed={event.event_id}|{e.event_id for e in nearby}
                for group in result['groups']:
                    if not set(group['event_ids'])<=allowed or event.event_id not in group['event_ids']:
                        raise ValueError('group_outside_candidates')
                if any(fingerprint(document(self.service.store.get_event(eid)))!=old for eid,old in evidence.items()):
                    raise ValueError('event_changed_during_grouping')
                for group in result['groups']:
                    if group['category']=='theme' and origin:
                        for eid in group['event_ids']:
                            self.copy_pool(self.service.store.get_event(eid))
                        self.summarize_theme(group,origin,config)
                with self.db() as conn:
                    conn.execute('INSERT OR REPLACE INTO processed VALUES (?,?,NULL,0)',(event.event_id,digest))
            except Exception as exc:
                with self.db() as conn:
                    conn.execute('INSERT OR REPLACE INTO processed VALUES (?,?,?,?)',
                                 (event.event_id,digest,type(exc).__name__,time.time()+lifecycle_config()['slice']['retry_seconds']))
            return True
        return False


class OrganizationWorker:
    def __init__(self,registry):
        self.registry=registry;self.stop_event=asyncio.Event()

    async def run(self):
        while not self.stop_event.is_set():
            for name in self.registry.names:
                try:
                    await asyncio.to_thread(Organization(self.registry.service(name)).process_one)
                except Exception:
                    pass
            try:
                await asyncio.wait_for(self.stop_event.wait(),timeout=1)
            except asyncio.TimeoutError:
                pass
