"""Export exact Ember tool schemas and isolated receipts to a local review file."""
from pathlib import Path
import argparse
import ast
import inspect
import json
import os
import sys
import tempfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from ember.writer import build_tools_spec,build_revise_tool_spec,build_search_tool_spec,build_pin_tool_specs
from ember.knowledge_tools import tool_specs,dispatch
from ember.tool_validation import validate_tool_input
from ember.promises import tool_specs as promise_specs, Promises, dispatch as promise_dispatch
from ember.organization_tools import tool_specs as organization_specs,dispatch as organization_dispatch
from ember.timeline import tool_specs as timeline_specs,Timeline
from ember.pin_policy import tool_specs as pin_specs


def specifications():
    ordinary=build_tools_spec()+build_revise_tool_spec()+build_search_tool_spec()+build_pin_tool_specs()+tool_specs()+promise_specs()+organization_specs()+timeline_specs()+pin_specs()
    sleep=tool_specs(sleep=True)
    by_name={s['function']['name']:s['function'] for s in ordinary+sleep}
    return by_name,{s['function']['name'] for s in ordinary},{s['function']['name'] for s in sleep}


def fields(schema,path='$',required=True):
    yield path,required,schema
    for name,child in schema.get('properties',{}).items():
        yield from fields(child,name if path=='$' else path+'.'+name,name in schema.get('required',[]))
    if 'items' in schema:
        yield from fields(schema['items'],path+'[]',True)


def source_returns(filename,function,tool=None):
    text=(ROOT/filename).read_text(encoding='utf-8')
    tree=ast.parse(text);parents={child:node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    output=[]
    for fn in ast.walk(tree):
        if not isinstance(fn,(ast.FunctionDef,ast.AsyncFunctionDef)) or fn.name!=function:continue
        for node in ast.walk(fn):
            if not isinstance(node,ast.Return):continue
            scope=parents.get(node)
            while scope is not None and not isinstance(scope,(ast.FunctionDef,ast.AsyncFunctionDef)):
                scope=parents.get(scope)
            if scope is not fn:continue
            owner=node;branches=[];named=[]
            while owner in parents and owner is not fn:
                owner=parents[owner]
                if isinstance(owner,ast.If):
                    expression=ast.unparse(owner.test);branches.append(expression)
                    if isinstance(owner.test,ast.Compare) and isinstance(owner.test.left,ast.Name) and owner.test.left.id=='name':
                        named.extend(c.value for c in owner.test.comparators if isinstance(c,ast.Constant) and isinstance(c.value,str))
                elif isinstance(owner,ast.ExceptHandler):
                    branches.append('except '+(ast.unparse(owner.type) if owner.type else 'all'))
            if tool and named and tool not in named:continue
            expression=ast.get_source_segment(text,node.value) if node.value is not None else 'None'
            output.append((node.lineno,' / '.join(reversed(branches)) or '函数末尾或通用路径',expression))
    return output


def source_rejections(filename,function):
    text=(ROOT/filename).read_text(encoding='utf-8')
    output=[]
    for fn in ast.walk(ast.parse(text)):
        if isinstance(fn,ast.FunctionDef) and fn.name==function:
            for node in ast.walk(fn):
                if isinstance(node,ast.Raise) and node.exc is not None:
                    output.append((node.lineno,ast.get_source_segment(text,node)))
    return output


def isolated_receipts():
    """No real memory, credentials, source provider or model calls."""
    from ember.service import EmberService
    from ember.store import EmberStore
    from ember.llm import MockLLMClient
    from ember.models import Event
    from ember.knowledge import KnowledgeStore
    from ember.turn_slices import TurnSliceStore
    from ember.memory_lifecycle import Lifecycle
    from ember.memory_maintenance import catalog,knowledge_reviews
    from ember.slice_embedding import SliceEmbeddingConfig
    from ember.sleep_knowledge import SleepKnowledge
    from ember import source_client
    results={}
    with tempfile.TemporaryDirectory(prefix='ember-tool-contract-') as directory:
        service=EmberService(EmberStore(directory),MockLLMClient())
        knowledge=KnowledgeStore(directory);slices=TurnSliceStore(directory)
        sources=[]
        for i in range(2):
            source=slices.save('fixture-old-'+str(i),[
                {'message_id':i*2+1,'role':'user','content':'Fixture source '+str(i)+': a concrete decision.'},
                {'message_id':i*2+2,'role':'assistant','content':'Fixture reply '+str(i)}])
            sources.append(source)
        ids=[s['slice_id'] for s in sources];refs=knowledge.sources(ids)
        embedding=SliceEmbeddingConfig('synthetic-key-not-used')
        for source in sources:slices.save_embedding(source,embedding.space,[1.,0.])
        session=service.get_or_create_session('fixture-chat');session.next_turn()
        text='A concrete fixture decision was made.'
        session.messages=[{'turn':1,'role':'user','content':text,'_source_message_id':'1'}]
        service._live_turn={'session':session,'turn':1,'user_text':text}
        event={'summary':'A fixture decision','core':text,'significance':8,'kind':'project','focus':'1'}
        args={'event':event,'recall_routes':[{'cue_type':'lexical','cue':'fixture'}],
              'quote_spans':[{'start_quote':text,'end_quote':text}]}
        wire={str(m['message_id']):m for s in sources for m in s['messages']}
        def provider(sid,message_ids,instance='default'):
            return [dict(wire[str(mid)]) for mid in message_ids]
        with patch.dict(os.environ,{'EMBER_MODEL_NETWORK_DISABLED':'1'}), \
             patch.object(source_client,'fetch_source_path',side_effect=provider), \
             patch.object(source_client,'SOURCE_URL',''), \
             patch('ember.slice_recall.SliceEmbeddingConfig.load',return_value=embedding), \
             patch('ember.slice_embedding.SliceEmbeddingClient.embed',return_value=[1.,0.]):
            results['write_event']=service.sidecar_call_tool('write_event',args)
            created=results['write_event'].get('event_id')
            if not created:raise RuntimeError('Documentation fixture failed to write Event')
            results['revise_memory_event']=service.sidecar_call_tool('revise_memory_event',{'event_id':created,'core':'Updated fixture decision'})
            results['pin_memory']=service.sidecar_call_tool('pin_memory',{'event_id':created})
            results['unpin_memory']=service.sidecar_call_tool('unpin_memory',{'event_id':created})
            # A separate complete, attested fixture gives read_memory_source a full source path.
            for index,source in enumerate(sources):
                eid='EVT-fixture-source-'+str(index)
                ev=Event(event_id=eid,version_id='v1',source_ref='fixture',raw_ref='fixture',
                    event_time='2026-01-0'+str(index+1),ingest_time='2026-01-01',summary='Fixture event',core='fixture concrete experience')
                service.store.write_event(ev);knowledge.save_event_sources(eid,knowledge.sources([source['slice_id']]))
                corpus=[dict(m,turn=1,_source_session_id=source['session_id'],_source_message_id=m['message_id']) for m in source['messages']]
                service.event_sources.capture(eid,'v1',source['session_id'],[(1,1)],corpus)
                service.turn_spans.fill(eid,[(1,1)],filled_by='documentation-fixture')
            results['read_memory_source']=service.sidecar_call_tool('read_memory_source',{'event_id':'EVT-fixture-source-0'})
            results['search_memory']=service.sidecar_call_tool('search_memory',{'query':'fixture'})
            results['read_memory_slices']=dispatch(service,'read_memory_slices',{'slice_ids':ids})
            results['search_memory_slices']=dispatch(service,'search_memory_slices',{'query':'fixture','k':2})
            first=knowledge.create('pattern','Fixture pattern A',['fixture'],ids)['memory_id']
            second=knowledge.create('pattern','Fixture pattern B',['fixture'],ids)['memory_id']
            rows=catalog(service);items=[r for r in rows if r['kind']=='knowledge']
            knowledge_reviews(service,rows,{r['id']:[1.,0.] for r in items})
            life=Lifecycle(directory)
            generic=life.propose('pattern',refs,'Fixture new direction',{'direction':'fixture direction'})
            events=[r for r in rows if r['id'].startswith('EVT-fixture-source-')]
            entity=life.propose('entity_extract',refs,'Fixture shared object',{'items':events,'direction':'fixture object'})
            stage=SleepKnowledge(service);identity={'sleep_id':'fixture-sleep','session_id':'fixture-chat'}
            started=stage.start(identity);review=next(g['group_id'] for g in started['groups'] if g['kind']=='knowledge_review')
            counter=0
            def call(name,value):
                nonlocal counter
                counter+=1
                return stage.call(dict(identity,call_id='doc-'+str(counter),name=name,arguments=value))
            for group in started['groups']:
                call('read_memory_slices',{'group_id':group['group_id']})
            results['list_memory_groups']=call('list_memory_groups',{})
            results['list_memory_knowledge']=call('list_memory_knowledge',{})
            results['write_memory_knowledge']=call('write_memory_knowledge',{'kind':'pattern','content':'A fixture insight',
                'recall_cues':['fixture'],'source_slice_ids':ids,'confidence':8})
            memory=results['write_memory_knowledge'].get('memory_id')
            results['set_knowledge_pin']=call('set_knowledge_pin',{'memory_id':memory,'pinned':False})
            results['review_memory_knowledge']=call('review_memory_knowledge',{'group_id':review,'items':[
                {'memory_id':first,'action':'keep','confidence':8},{'memory_id':second,'action':'keep','confidence':8}]})
            results['extract_memory_entity']=call('extract_memory_entity',{'group_id':entity,'name':'Fixture object',
                'summary':'An isolated object','event_ids':[r['id'] for r in events]})
            results['write_promise']=service.sidecar_call_tool('write_promise',{'event':dict(event,kind='promise',due=None),'recall_routes':args['recall_routes'],'source_slice_ids':ids})
            promise_id=results['write_promise']['event_id']
            Promises(service).enter(promise_id,'cue',{'session_id':'fixture-chat','message_ids':['1']})
            results['list_pending_promises']=promise_dispatch(service,'list_pending_promises',{})
            results['process_promise']=promise_dispatch(service,'process_promise',{'event_id':promise_id,'log':'Fixture promise still open'})
            entity_id=results['extract_memory_entity']['entity_id']
            results['revise_entity_summary']=organization_dispatch(service,'revise_entity_summary',{'entity_id':entity_id,'summary':'Fixture corrected object'})
            results['read_entity_events']=organization_dispatch(service,'read_entity_events',{'entity_id':entity_id})
            relationship=Event('EVT-fixture-relationship','v1','fixture','fixture','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',core='Fixture relationship',kind='relationship')
            service.store.write_event(relationship)
            with Timeline(service).db() as db:
                db.execute('INSERT INTO points(id,text,event_ids,intervals) VALUES (?,?,?,?)',
                    ('PT-fixture','Fixture relationship point',json.dumps([relationship.event_id]),json.dumps([{'start':relationship.event_time,'end':relationship.event_time}])))
            results['read_relationship_point']=service.sidecar_call_tool('read_relationship_point',{'point':'PT-fixture'})
            pin_promises=[]
            for index in range(2):
                written=service.sidecar_call_tool('write_promise',{'event':dict(event,kind='promise',significance=10,due=None),
                    'recall_routes':args['recall_routes'],'source_slice_ids':ids})
                pin_promises.append(written['event_id'])
            results['list_memory_pins']=service.sidecar_call_tool('list_memory_pins',{})
            results['merge_memory_events']=service.sidecar_call_tool('merge_memory_events',{'event_ids':pin_promises,
                'summary':'One fixture promise','core':'One fixture commitment from both original spans'})
            results['set_slice_recall_state']=call('set_slice_recall_state',{'group_id':generic,'slice_ids':[ids[0]],
                'reason':'Fixture-only outdated example','evidence':sources[0]['messages'][0]['content']})
            results['finish_memory_group']=call('finish_memory_group',{'group_id':generic,'state':'reviewed'})
    return results


HANDLERS={
 'write_event':[('ember/service.py','sidecar_create_memory_event'),('ember/service.py','_create_memory_in_context'),('ember/service.py','write_validated_event'),('ember/writer.py','writer_error_result')],
 'read_memory_source':[('ember/service.py','read_source_event')],
 'revise_memory_event':[('ember/service.py','sidecar_revise_memory_event')],
 'search_memory':[('ember/service.py','sidecar_search_memory')],
 'pin_memory':[('ember/service.py','sidecar_pin_memory')],
 'unpin_memory':[('ember/service.py','sidecar_unpin_memory')],
 'write_promise':[('ember/knowledge_tools.py','write_event')],
 'write_memory_knowledge':[('ember/knowledge.py','create'),('ember/sleep_review.py','score'),('ember/sleep_review.py','set_confidence')],
 'set_knowledge_pin':[('ember/knowledge.py','pin')],
 'review_memory_knowledge':[('ember/sleep_review.py','review_knowledge'),('ember/sleep_review.py','group'),('ember/sleep_review.py','score')],
 'extract_memory_entity':[('ember/sleep_review.py','extract_entity'),('ember/sleep_review.py','group')],
 'set_slice_recall_state':[('ember/sleep_review.py','group'),('ember/memory_lifecycle.py','mark')],
 'read_memory_slices':[('ember/knowledge.py','sources'),('ember/knowledge.py','read_sources')],
 'finish_memory_group':[('ember/knowledge.py','finish_group')],
}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'工具规范审阅.md')
    parser.add_argument('--force',action='store_true',help='Overwrite an existing review file, including manual edits')
    args=parser.parse_args()
    if args.output.exists() and not args.force:parser.error('Review file exists; use --force only if manual edits are saved')
    specs,ordinary,sleep=specifications();receipts=isolated_receipts()
    if set(receipts)!=set(specs):raise RuntimeError('Incomplete success receipt coverage')
    lines=['# Ember工具描述、字段与回执审阅稿','',
        '仅供本地审阅和手工调整，不提交GitHub。来源是当前运行schema、参数验证及返回分支；没有补写缺失的描述。',
        '这是审阅稿，不会自动修改运行规范。修改此文件后需要再同步到代码。重新导出须显式--force，避免覆盖手工修改。','',
        f'共{len(specs)}个不同工具；普通MCP提供{len(ordinary)}个，Sleep提供{len(sleep)}个，部分工具重叠。',
        '成功样例来自临时记忆库和构造原文，模型网络被禁用，来源提供方与向量被隔离替代；不是实际用户记忆。',
        'JSON schema原样列出；无description明确标记。字段“必填”指其直接父对象存在时必填。数组[]表示每个成员。','',
        '## 通用调用与失败规则','',
        '- 普通MCP返回content中的JSON文本；isError在status=failed/rejected或ok=false时为true。',
        '- Sleep经专属HTTP工具入口，App把返回JSON作为tool_result；当前App按status=failed设置is_error。',
        '- recall_id/tool_call_id/run_id/sleep_id/session_id属于调用上下文，不是LLM应填写的工具参数。',
        '- schema验证之后还会校验当前轮、原文定位、冻结来源、已读证据、预算和置信度等业务条件。',
        '- 返回结构目前并不统一：有些没有status，有些失败仅error而没有code；下文保留实际差异，供规范调整。','']
    lines+=['- 当前没有统一的输出JSON schema；返回分支表达式、helper定义与实际回执一起构成输出参考。主动run目前只允许read_memory_source。','']
    for name,fn in specs.items():
        availability='、'.join(v for v,allowed in [('普通MCP',ordinary),('Sleep',sleep)] if name in allowed)
        lines+=['## '+name,'','可用入口：'+availability,'','### LLM看到的工具描述（原文）','',fn.get('description','（未提供description）'),'',
                '### 字段描述、类型和约束','']
        for path,required,schema in fields(fn['parameters']):
            typ=schema.get('type')
            if typ is None:typ='未显式声明type'+('；由enum限定为'+json.dumps(schema['enum'],ensure_ascii=False) if 'enum' in schema else '')
            constraints={k:v for k,v in schema.items() if k not in ('type','description','properties','items')}
            lines+=['#### '+path,'','- 类型：`'+str(typ)+'`','- 必填：'+('是' if required else '否'),
                    '- LLM字段描述：'+schema.get('description','**未提供description，模型只看到类型/约束。**')]
            if constraints:lines+=['- 约束：`'+json.dumps(constraints,ensure_ascii=False)+'`']
            lines+=['']
        lines+=['### 模型实际收到的完整参数schema','','```json',json.dumps(fn['parameters'],ensure_ascii=False,indent=2),'```','',
                '### 实际执行样例回执','','```json',json.dumps(receipts[name],ensure_ascii=False,indent=2),'```','']
        if receipts[name].get('status') in ('failed','rejected'):raise RuntimeError('Unsuccessful sample: '+name)
        failure=validate_tool_input({'__unknown_field':True},fn['parameters'])
        lines+=['### 实际schema失败样例','','输入包含未声明字段或缺少必填项时，当前校验器返回：','',
            '```json',json.dumps(failure,ensure_ascii=False,indent=2),'```','',
            '### 成功、失败与其他返回分支（代码原样表达式）','',
            '变量、helper调用和字典展开保留为Python表达式；上面的JSON是实际执行样例，不能把单个样例当作全部返回字段。','']
        handlers=HANDLERS.get(name,[])
        if name in {s['function']['name'] for s in tool_specs(sleep=True)+tool_specs()}:handlers=[('ember/knowledge_tools.py','dispatch')]+handlers
        for filename,function in handlers:
            lines+=['#### '+filename+' :: '+function,'']
            for number,condition,expression in source_returns(filename,function,name if function=='dispatch' else None):
                lines+=['路径（第'+str(number)+'行）：`'+condition+'`','','```python',expression,'```','']
            rejections=source_rejections(filename,function)
            if rejections:
                lines+=['业务拒绝条件（在dispatch中转换为failed/invalid_memory_material等回执，具体包装见上文）：','']
                for number,expression in rejections:
                    lines+=['第'+str(number)+'行：','','```python',expression,'```','']
    lines+=['## 共用上下文、幂等与验证失败分支','']
    for filename,function in [('ember/tool_validation.py','validate_tool_input'),('ember/service.py','sidecar_call_tool'),
            ('ember/service.py','_tool_receipt'),('ember/service.py','_generation_error'),
            ('ember/sleep_knowledge.py','call'),('ember/sleep_knowledge.py','fail'),('ember/runs.py','call'),('ember/runs.py','failure')]:
        lines+=['### '+filename+' :: '+function,'']
        for number,condition,expression in source_returns(filename,function):
            lines+=['路径（第'+str(number)+'行）：`'+condition+'`','','```python',expression,'```','']
    lines+=['## 你的修改记录','','可在这里记工具名、字段路径、拟改描述、类型/约束、成功回执与失败回执。','']
    args.output.write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps({'output':str(args.output),'tools':len(specs),'ordinary':len(ordinary),'sleep':len(sleep),
        'lines':len(lines),'receipts':len(receipts)},ensure_ascii=False))


if __name__=='__main__':main()
