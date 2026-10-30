"""Retain the legacy Event/Entity decision, append independent slice/knowledge paths."""
from .slice_recall import automatic_slice_recall
from .knowledge_tools import append_recall


def automatic_hybrid_recall(service,session,turn,text,context_start_id=None,deferred_entries=None):
    result=service.legacy_event_recall_pipeline(session,turn,text,
        context_start_id=context_start_id,deferred_entries=deferred_entries,allow_observe=False)
    raw=automatic_slice_recall(service,session,turn,text,context_start_id)
    result['slice_recall']=raw['slice_recall']
    from .slice_presentation import append_slice_presentations
    result=append_slice_presentations(service,result,text)
    return append_recall(service,result,text)
