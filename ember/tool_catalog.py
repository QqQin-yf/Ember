"""MCP schemas for ordinary chat and Ember maintenance."""
from copy import deepcopy

from . import briefing_edits, extraction, knowledge_tools, organization_tools, pin_policy, privacy, promises, proposals, timeline
from .knowledge import KnowledgeStore
from .writer import build_pin_tool_specs, build_revise_tool_spec, build_search_tool_spec, build_tools_spec


def _schemas():
    specs = (build_tools_spec() + [build_revise_tool_spec()[0]]
             + build_search_tool_spec() + build_pin_tool_specs() + knowledge_tools.tool_specs()
             + promises.tool_specs() + organization_tools.tool_specs() + timeline.tool_specs()
             + pin_policy.tool_specs() + privacy.tool_specs() + extraction.tool_specs()
             + briefing_edits.tool_specs() + proposals.tool_specs())
    return {spec['function']['name']: spec['function'] for spec in specs}


_MAINTENANCE_ONLY = (extraction.TOOL_NAMES | briefing_edits.TOOL_NAMES | proposals.TOOLS
                     | {'review_memory_knowledge'})
_LEGACY_NAMES = {'pin_memory', 'unpin_memory', 'set_knowledge_pin',
                 'search_memory_slices', 'read_memory_slices'}


def _advertised(legacy):
    specs = deepcopy(legacy)
    search = specs['search_memory']
    search['description'] = '定位上下文以外的记忆或原文。返回命中数、条目 ID、日期和原话片段。选中后用 read_memory_source 阅读完整出处。'
    search['parameters']['properties']['scope'] = {
        'enum': ['event', 'source'],
        'description': 'event 搜 Event、认知和规律；source 搜原文切片。'}
    search['parameters']['required'].append('scope')
    source = specs['read_memory_source']
    source['description'] = '读选中记忆或切片的出处。event_id、knowledge_id 或 slice_ids 三选一。'
    source['parameters']['required'] = []
    source['parameters']['properties'].update(
        knowledge_id={'type': 'string', 'minLength': 1},
        slice_ids={'type': 'array', 'items': {'type': 'string', 'minLength': 1},
                   'minItems': 1, 'maxItems': 100, 'uniqueItems': True})
    specs['set_memory_pin'] = {
        'name': 'set_memory_pin',
        'description': '设置或取消记忆在简报中的常驻。event 含约定；knowledge 含认知和规律。Event 仅在用户要求时钉住。',
        'parameters': {'type': 'object', 'additionalProperties': False,
                       'required': ['kind', 'id', 'pinned'], 'properties': {
                           'kind': {'enum': ['event', 'knowledge']},
                           'id': {'type': 'string', 'minLength': 1},
                           'pinned': {'type': 'boolean'}}}}
    review = specs['review_memory_knowledge']
    review['description'] = ('空调用领取当前待审材料。按材料提交每条认知的决定，成功时返回下一组；'
                             '失败时根据错误重新空调用领取。')
    review['parameters']['properties'].pop('group_id')
    review['parameters']['required'] = []
    return specs


_LEGACY = _schemas()
_ADVERTISED = _advertised(_LEGACY)


def visible_specs(service, context):
    mode = service.extraction_tool_mode(context.get('recall_id')) if service else None
    if mode == 'briefing':
        names = briefing_edits.TOOL_NAMES
    elif mode == 'proposals':
        names = proposals.TOOLS
    elif mode == 'raw':
        from .extraction_pages import ALLOWED_TOOLS
        names = ALLOWED_TOOLS - _LEGACY_NAMES - briefing_edits.TOOL_NAMES
    else:
        names = _ADVERTISED.keys() - _MAINTENANCE_ONLY - _LEGACY_NAMES
    return [_ADVERTISED[name] for name in _ADVERTISED if name in names]


def call_schema(name, args):
    # Calls from integrations with cached schemas can finish on the old contract.
    args = args if isinstance(args, dict) else {}
    legacy = ((name == 'search_memory' and 'scope' not in args)
              or (name == 'review_memory_knowledge' and 'group_id' in args))
    spec = (_LEGACY if legacy or name in _LEGACY_NAMES else _ADVERTISED).get(name)
    return spec['parameters'] if spec else None


def set_memory_pin(service, args):
    kind, item_id, pinned = (args.get(key) for key in ('kind', 'id', 'pinned'))
    if kind not in ('event', 'knowledge') or not isinstance(item_id, str) or not item_id.strip() or type(pinned) is not bool:
        return service._generation_error('invalid_pin_operation', 'Choose a memory kind, ID and pin state.')
    if kind == 'knowledge':
        return knowledge_tools.dispatch(service, 'set_knowledge_pin', {'memory_id': item_id, 'pinned': pinned})
    operation = service.sidecar_pin_memory if pinned else service.sidecar_unpin_memory
    return operation({'event_id': item_id})


def read_memory_source(service, args):
    keys = [key for key in ('event_id', 'knowledge_id', 'slice_ids') if args.get(key)]
    if len(keys) != 1:
        return service._generation_error('invalid_source_target', 'Choose exactly one of event_id, knowledge_id or slice_ids.')
    key = keys[0]
    if key == 'event_id':
        return service.sidecar_read_memory_source(args[key])
    if key == 'slice_ids':
        return knowledge_tools.dispatch(service, 'read_memory_slices', {key: args[key]})
    store = KnowledgeStore(service.store.root, service.source_instance)
    item = next((row for row in store.all() if row['memory_id'] == args[key]), None)
    if item is None:
        return service._generation_error('knowledge_not_found', 'Unknown knowledge_id.')
    return knowledge_tools.dispatch(service, 'read_memory_slices', {'memory_id': args[key]})
