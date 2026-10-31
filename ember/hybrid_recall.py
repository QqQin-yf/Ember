"""Retain the legacy Event/Entity decision, append independent slice/knowledge paths."""
from .slice_recall import automatic_slice_recall
from .knowledge_tools import append_recall


def automatic_hybrid_recall(service,session,turn,text,context_start_id=None,deferred_entries=None,context_message_ids=None):
    result=service.legacy_event_recall_pipeline(session,turn,text,
        context_start_id=context_start_id,deferred_entries=deferred_entries,context_message_ids=context_message_ids)
    from .memory_lifecycle import Lifecycle, filter_recall
    life=Lifecycle(service.store.root,service.source_instance)
    blocked=filter_recall(service,result,session,context_start_id,context_message_ids)
    def allowed(old):
        return all(p.get('event_id') not in blocked and
            (not p.get('event_id') or (service.sleep_state.recall_allowed(p['event_id']) and
              not service.pin_store.is_pinned(p['event_id']) and
              (not p.get('version_id') or str(service.store.get_event(p['event_id']).version_id)==p['version_id'])))
            for p in old.get('presentations',[]))
    result=life.hold(session.session_id,turn,result,allowed)
    raw=automatic_slice_recall(service,session,turn,text,context_start_id,context_message_ids)
    result['slice_recall']=raw['slice_recall']
    from .slice_presentation import append_slice_presentations
    result=append_slice_presentations(service,result,text)
    result=append_recall(service,result,text,session.session_id,context_message_ids)
    if result.get('prefix'):
        result['prefix']='记忆仅供参考；与当前对话中用户所说冲突时，以当前用户说法为准。\n'+result['prefix']
    return result
