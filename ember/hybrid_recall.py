"""Retain the legacy Event/Entity decision, append independent slice/knowledge paths."""
from .slice_recall import automatic_slice_recall
from .knowledge_tools import append_recall


def automatic_hybrid_recall(service,session,turn,text,context_start_id=None,deferred_entries=None,
                            context_message_ids=None,visible_event_ids=None):
    raw=automatic_slice_recall(service,session,turn,text,context_start_id,context_message_ids)
    current_slices = raw['slice_recall']
    from .event_recall import append_events
    from .memory_lifecycle import Lifecycle, filter_recall
    life=Lifecycle(service.store.root,service.source_instance)
    blocked=filter_recall(service,raw,session,context_start_id,context_message_ids,include_ineligible=False)
    if visible_event_ids is None:
        from .timeline import Timeline
        timeline = Timeline(service)
        blocked.update(e.event_id for e in service.store.all_events()
                       if e.kind == 'relationship' and timeline.mode(e.event_id) == 'original')
    else:
        blocked.update(life.canonical(eid) for eid in visible_event_ids)
    result=append_events(service,raw,session,turn,text,context_start_id,deferred_entries,
                         context_message_ids,visible=blocked)
    def allowed(old):
        return all(p.get('event_id') not in blocked and
            (not p.get('event_id') or (service.sleep_state.recall_allowed(p['event_id']) and
              not service.pin_store.is_pinned(p['event_id']) and
              (not p.get('version_id') or str(service.store.get_event(p['event_id']).version_id)==p['version_id'])))
            for p in old.get('presentations',[]))
    result=life.hold(session.session_id,turn,result,allowed)
    result['slice_recall'] = current_slices
    from .slice_presentation import append_slice_presentations
    result=append_slice_presentations(service,result,text,blocked_events=blocked)
    from .organization_tools import append_entity_cards
    result=append_entity_cards(service,result)
    result=append_recall(service,result,text,session.session_id,context_message_ids)
    if result.get('prefix'):
        result['prefix']='记忆仅供参考；与当前对话中用户所说冲突时，以当前用户说法为准。\n'+result['prefix']
    return result
