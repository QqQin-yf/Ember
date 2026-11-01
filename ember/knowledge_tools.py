"""App AI tools: read classified sources, author memories, manage fixed budget."""
import json

from .knowledge import KnowledgeStore


def tool_specs(sleep=False):
    strings={'type':'array','items':{'type':'string'},'minItems':1,'maxItems':100,'uniqueItems':True}
    def spec(name,desc,properties,required=()):
        return {'type':'function','function':{'name':name,'description':desc,'parameters':{
            'type':'object','additionalProperties':False,'properties':properties,'required':list(required)}}}
    from .writer import build_tools_spec
    writer=build_tools_spec()[0]['function']['parameters']['properties']
    specs = [
        spec('list_memory_groups','读取后台对旧切片的分组。分组不是事实结论；由你阅读原文后自主决定是否提炼认知、规律或 Event。',
             {'include_reviewed':{'type':'boolean'}}),
        spec('read_memory_slices','按切片 ID 精确读取原文；也可按归类组或认知/规律/Event ID 下钻。保留分离来源，不扩展成连续区间。',
             {'slice_ids':strings,'group_id':{'type':'string'},'memory_id':{'type':'string'}}),
        spec('search_memory_slices','主动用自然语言检索原文切片，返回 ID、匹配分数及原文，供进一步下钻。',
             {'query':{'type':'string','minLength':1},'k':{'type':'integer','minimum':1,'maximum':20}},['query']),
        spec('write_memory_knowledge','由你从原文提炼认知或规律。必须提供来源切片和触点。默认尝试 pin 至 briefing；该类条数满时先 unpin，再 pin；后台不替你提炼。',
             {'kind':{'enum':['cognition','pattern']},'content':{'type':'string','minLength':1},
              'recall_cues':strings,'source_slice_ids':strings,
              'confidence':{'type':'integer','minimum':0,'maximum':10},
              'confidence_basis':{'enum':['recently_disproved','outdated']},'confidence_evidence':{'type':'string'}},['kind','content','recall_cues','source_slice_ids','confidence']),
        spec('set_knowledge_pin','自主管理认知/规律的 briefing 常驻清单；该类条数满时先 unpin，再 pin；取消后仍可正常召回。',
             {'memory_id':{'type':'string'},'pinned':{'type':'boolean'}},['memory_id','pinned']),
        spec('finish_memory_group','阅读并处理分组后标记 reviewed，或判断不值得提炼时 dismissed。不会删除原文。',
             {'group_id':{'type':'string'},'state':{'enum':['reviewed','dismissed']}},['group_id','state']),
        spec('list_memory_knowledge','读取现有认知/规律与 pin 预算，供 Sleep 时自主取舍。',{})]
    specs += [
        spec('review_memory_knowledge','审阅相似认知/规律：维持、改写补写或合并。每条必须列为keep/rewrite/merge目标或merge_ids成员，不能遗漏；每条最终保留项必须评分。只有近期经历明确打破或过时才能<=5，须提供逐字来源证据。',
            {'group_id':{'type':'string'},'items':{'type':'array','minItems':1,'items':{'type':'object',
              'additionalProperties':False,'required':['memory_id','action','confidence'],'properties':{
                'memory_id':{'type':'string'},'action':{'enum':['keep','rewrite','merge']},
                'content':{'type':'string'},'recall_cues':strings,'merge_ids':strings,
                'source_slice_ids':strings,
                'confidence':{'type':'integer','minimum':0,'maximum':10},
                'confidence_basis':{'enum':['recently_disproved','outdated']},'confidence_evidence':{'type':'string'}}}}},['group_id','items']),
        spec('extract_memory_entity','从Event主题材料中正式提取确定的Entity。先读原文；无法确定可dismiss，不必提取。',
            {'group_id':{'type':'string'},'name':{'type':'string','minLength':1},'summary':{'type':'string','minLength':1},
             'aliases':{'type':'array','items':{'type':'string'}},'event_ids':strings},['group_id','name','summary','event_ids']),
        spec('set_slice_recall_state','阅读后标注过时或与当前上下文矛盾的切片，不删除原文。提供逐字证据。',
            {'group_id':{'type':'string'},'slice_ids':strings,'reason':{'type':'string','minLength':1},
             'evidence':{'type':'string','minLength':1}},['group_id','slice_ids','reason','evidence'])]
    return ([s for s in specs if s['function']['name']!='search_memory_slices'] if sleep else
            [s for s in specs if s['function']['name'] in ('read_memory_slices','search_memory_slices')])


TOOL_NAMES={'list_memory_groups','read_memory_slices','search_memory_slices','write_memory_knowledge',
            'set_knowledge_pin','finish_memory_group','list_memory_knowledge',
            'review_memory_knowledge','extract_memory_entity','set_slice_recall_state'}
SLEEP_ONLY_TOOLS=TOOL_NAMES-{'read_memory_slices','search_memory_slices'}
VOLATILE_TOOLS={'read_memory_slices','search_memory_slices','list_pending_promises'}


def read_memory(service, memory_id, canonicalize=True):
    from .memory_lifecycle import Lifecycle
    if canonicalize:
        memory_id=Lifecycle(service.store.root,service.source_instance).canonical(memory_id)
    store=KnowledgeStore(service.store.root,service.source_instance)
    sources=store.event_sources(memory_id)
    if sources is None:
        row=next((r for r in store.all() if r['memory_id']==memory_id),None)
        if row is None:
            return None
        sources=row['sources']
    else:
        try:
            service.store.get_event(memory_id)
        except KeyError:
            return None
    try:
        rows=store.read_sources(sources)
        return {'status':'ok','memory_id':memory_id,'sources':sources,'slices':rows,
                'transcript':'\n\n'.join(f"[{r['slice_id']}]\n"+'\n'.join(f"{m['role']}: {m['content']}" for m in r['messages']) for r in rows)}
    except ValueError as exc:
        return {'status':'failed','code':str(exc)}


def write_event(service, args, context):
    from .writer import validate_writer_arguments
    from .models import RecallRoute
    from .slice_maintenance import enqueue_event_maintenance
    store=KnowledgeStore(service.store.root,service.source_instance)
    if context is None:
        context=service._live_turn
    if context is None:
        raise ValueError('A live App generation is required to author an Event')
    # Reuse the complete Event content/route validation, while provenance is
    # explicitly resolved by slice IDs, never by these legacy quote anchors.
    validated,error=validate_writer_arguments(args)
    if error:
        raise ValueError(str(error))
    sources=store.sources(args.get('source_slice_ids'))
    rows=store.read_sources(sources)
    event=service._build_event(context['session'],context['turn'],validated['event'])
    event.source_ref='slice-sources/'+event.event_id
    event.raw_ref=event.source_ref
    event.turn_refs=[]
    event.provenance_refs=['app-slice-writer']+['slice:'+s['slice_id'] for s in sources]
    corpus=[m for r in rows for m in r['messages']]
    service._validate_focus(event,corpus)
    store.save_event_sources(event.event_id,sources)
    service.store.write_event(event)
    for index,route in enumerate(validated['recall_routes'],1):
        service.store.write_route(RecallRoute(route_id=f'rt_{event.event_id}_{index:02d}',event_id=event.event_id,
            matcher='lexical',cue_type=route['cue_type'],cue=route['cue'],status='active',
            evidence_refs=['slice:'+s['slice_id'] for s in sources],provenance_refs=['app-slice-writer']))
    from .promises import Promises
    Promises(service).written(event)
    from .source_sync import request_sync
    request_sync(service)
    return {'status':'created','event_id':event.event_id,'summary':event.summary,
            'source_slice_ids':args['source_slice_ids']}


def dispatch(service,name,args,context=None):
    store=KnowledgeStore(service.store.root,service.source_instance)
    try:
        if name in SLEEP_ONLY_TOOLS and (context is None or context.get('kind')!='sleep'):
            return {'status':'failed','code':'sleep_context_required'}
        if context and context.get('kind')=='sleep':
            if name == 'write_memory_knowledge' and 'source_ids' in context:
                ids=args.get('source_slice_ids')
                if not isinstance(ids,list) or not all(isinstance(s,str) and s in context['source_ids'] for s in ids):
                    return {'status':'failed','code':'source_outside_sleep_materials'}
                if 'read_source_ids' in context and not set(ids)<=context['read_source_ids']:
                    return {'status':'failed','code':'source_not_read_in_sleep'}
                if 'frozen_sources' in context:
                    store.read_sources([context['frozen_sources'][sid] for sid in ids])
            if name=='finish_memory_group' and 'group_ids' in context and args.get('group_id') not in context['group_ids']:
                return {'status':'failed','code':'group_outside_sleep_materials'}
        if name=='list_memory_groups':
            return {'status':'ok','groups':store.groups(pending=not args.get('include_reviewed',False)),
                    'instruction':'这些是旧原文的分组，不是已成立的记忆；阅读后自行决定提炼或 dismiss。'}
        if name=='finish_memory_group':
            return store.finish_group(args.get('group_id'),args.get('state'))
        if name=='write_memory_knowledge':
            if context is None and service._live_turn is None:
                raise ValueError('A live App generation is required to author knowledge')
            from .sleep_review import score, set_confidence
            score(args)
            if args.get('confidence',8)<=5:
                originals=store.read_sources(store.sources(args['source_slice_ids']))
                if not any(args['confidence_evidence'] in m['content'] for s in originals for m in s['messages']):
                    raise ValueError('confidence_evidence_not_in_sources')
            result=store.create(args.get('kind'),args.get('content'),args.get('recall_cues'),args.get('source_slice_ids'))
            result['confidence']=set_confidence(store,result['memory_id'],args)
            if result['confidence']<=5:
                result['pin']={'status':'ok','pinned':False,'reason':'low_confidence_no_recall'}
            return result
        if name=='review_memory_knowledge':
            from .sleep_review import review_knowledge
            return review_knowledge(service,args,context)
        if name=='extract_memory_entity':
            from .sleep_review import extract_entity
            return extract_entity(service,args,context)
        if name=='set_slice_recall_state':
            from .sleep_review import group
            item=group(service,args['group_id'],context)
            selected=[s for s in item['sources'] if s['slice_id'] in args['slice_ids']]
            if len(selected)!=len(args['slice_ids']):
                raise ValueError('slice_outside_group')
            from .memory_lifecycle import Lifecycle
            for row in store.read_sources(selected):
                if not any(args['evidence'] in m['content'] for m in row['messages']):
                    raise ValueError('slice_evidence_invalid')
            for s in selected:
                Lifecycle(store.root,store.instance).mark(s['slice_id'],'no_recall',args['reason'],[args['evidence']])
            return {'status':'ok','slice_ids':args['slice_ids'],'recall_state':'no_recall'}
        if name=='set_knowledge_pin':
            return store.pin(args.get('memory_id'),args.get('pinned'))
        if name=='read_memory_slices':
            if sum(bool(args.get(k)) for k in ('slice_ids','memory_id','group_id'))!=1:
                raise ValueError('Choose exactly one of slice_ids, memory_id, group_id')
            if args.get('memory_id'):
                return read_memory(service,args['memory_id']) or {'status':'failed','code':'memory_not_found'}
            if args.get('group_id'):
                group=(context or {}).get('snapshot_groups',{}).get(args['group_id']) or next((r for r in store.groups(False) if r['group_id']==args['group_id']),None)
                if group is None:
                    raise ValueError('group_not_found')
                sources=group['sources']
            else:
                sources=store.sources(args['slice_ids'])
            return {'status':'ok','slices':store.read_sources(sources)}
        if name=='search_memory_slices':
            from .slice_recall import SliceRecall
            recall=SliceRecall(service.store.root,service.source_instance)
            result=recall.match(args.get('query'),purpose='search')
            trace=recall.trace(result['trace_id'],True)
            k=args.get('k',5)
            if type(k) is not int or not 1<=k<=20:
                raise ValueError('k must be 1..20')
            return {'status':'ok','trace_id':trace['trace_id'],'slice_status':trace['status'],
                    'slices':[r for r in trace['rankings'] if r['in_candidate_pool']][:k]}
    except (ValueError,TypeError,KeyError) as exc:
        return {'status':'failed','code':'invalid_memory_material','error':str(exc)}


def append_briefing(service,report):
    store=KnowledgeStore(service.store.root,service.source_instance)
    section=store.briefing()
    if section['text']:
        report=dict(report,status='ok',text='\n'.join(t for t in [report.get('text',''),section['text']] if t))
        report['chars']=len(report['text'])
    return dict(report,knowledge=section)


def append_recall(service,result,text,session_id=None,context_message_ids=None):
    from .slice_recall import SliceRecall
    store=KnowledgeStore(service.store.root,service.source_instance)
    vector=space=None
    metadata=result.get('slice_recall') or {}
    recall=SliceRecall(service.store.root,service.source_instance)
    if metadata.get('trace_id') and recall.path.exists():
        with recall.db() as db:
            row=db.execute('SELECT vector_json FROM queries WHERE trace_id=? AND instance=?',
                           (metadata['trace_id'],service.source_instance)).fetchone()
        if row and row[0]:
            vector=json.loads(row[0])
            # The exact query embedding namespace is persisted in the trace.
            from .generations import fingerprint
            space=fingerprint(['knowledge-content-v1',metadata.get('space')])
    hits=store.match(text,vector,space)
    if context_message_ids is not None:
        from .memory_lifecycle import sources_in_context
        hits=[r for r in hits if not sources_in_context(r['sources'],session_id,None,context_message_ids)]
    parts=[result.get('prefix','')]
    if hits:
        parts.append('【认知／规律浮现】\n'+'\n'.join(store.line(r) for r in hits))
        result['presentations']=result.get('presentations',[])+[
            {'presentation':'knowledge','memory_id':r['memory_id'],'source_slice_ids':[s['slice_id'] for s in r['sources']],
             'cue_hit':r['cue_hit'],'score':r['score']} for r in hits]
    result['prefix']='\n\n'.join(p for p in parts if p)
    result['knowledge_extra']=[r['memory_id'] for r in hits]
    if result.get('slice_recall') is not None:
        result['slice_recall']=dict(result['slice_recall'],knowledge_extra=result['knowledge_extra'])
    return result
