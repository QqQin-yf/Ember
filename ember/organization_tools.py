"""Entity summaries remain independent of Event recall seats."""
from .memory_lifecycle import Lifecycle
from .organization import Organization


def tool_specs():
    def spec(name,description,properties,required):
        return {'type':'function','function':{'name':name,'description':description,'parameters':{
            'type':'object','additionalProperties':False,'properties':properties,'required':required}}}
    return [spec('revise_entity_summary','看到 Entity summary 写错或需要补充时直接修订。',
                 {'entity_id':{'type':'string'},'summary':{'type':'string','minLength':1}},['entity_id','summary']),
            spec('read_entity_events','沿 Entity 卡片下钻其具体经历；需要原话再按 Event ID 读原文。',
                 {'entity_id':{'type':'string'}},['entity_id'])]


TOOL_NAMES={'revise_entity_summary','read_entity_events'}


def dispatch(service,name,args):
    try:
        eid=Lifecycle(service.store.root).canonical(args['entity_id'])
        card=service.entity_store.get(eid)
        if name=='read_entity_events':
            return {'status':'ok','entity_id':eid,'events':[
                {k:v for k,v in e.to_dict().items() if k!='focus'} for e in service.store.all_events() if eid in e.entity_refs]}
        if not isinstance(args.get('summary'),str) or not args['summary'].strip():
            raise ValueError('nonempty_summary_required')
        card.summary=args['summary'].strip()
        service.entity_store.save()
        with Organization(service).db() as conn:
            conn.execute('INSERT OR IGNORE INTO changed_entities VALUES (?)',(eid,))
        return {'status':'ok','entity_id':eid,'summary':card.summary}
    except (ValueError,KeyError,TypeError) as exc:
        return {'status':'failed','code':'invalid_entity_update','error':str(exc)}


def append_entity_cards(service,result):
    life=Lifecycle(service.store.root)
    present={p['entity_id'] for p in result.get('presentations',[]) if p.get('entity_id')}
    attached=set()
    for item in result.get('presentations',[]):
        if item.get('event_id'):
            event=service.store.get_event(item['event_id'])
            if event.kind!='relationship':
                attached.update(life.canonical(eid) for eid in event.entity_refs)
    for eid in sorted(attached-present):
        try:
            card=service.entity_store.get(eid)
        except KeyError:
            continue
        block=f'关于〈{card.name}〉 [{eid}]：{card.summary}'
        result['prefix']+='\n'+block
        result['presentations'].append({'presentation':'entity_overview','entity_id':eid,'text':block})
        result['entity_names'].append(card.name)
    result['entity_recalled']=len({p['entity_id'] for p in result.get('presentations',[]) if p.get('entity_id')})
    result['recalled_count']=len({p['event_id'] for p in result.get('presentations',[]) if p.get('event_id')})
    return result
