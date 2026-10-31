"""Child process only: real kill checkpoints, no production fault switches."""
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch
from tests.legacy_event_fixture import LegacyEventService as EmberService
from ember.store import EmberStore
from ember.llm import MockLLMClient
from tests.test_generations import TEXT
from tests.test_sidecar import write_args


def service(root):
    return EmberService(EmberStore(root), MockLLMClient())


def finish(svc):
    return svc.hook_turn_end("source", "Committed original generation reply", turn=1,
                             assistant_message_id=75, recall_id="writer")


def main():
    action, root, checkpoint = sys.argv[1:]
    if action == "prepare":
        svc = service(root)
        svc.hook_turn_start('source', TEXT, user_message_id=71, recall_id='writer')
        result = svc.sidecar_call_tool('create_memory_event', write_args(), recall_id='writer', tool_call_id='create')
        assert result['status'] == 'created', result
        for rid in ('reader', 'reader2'):
            result = svc.hook_turn_start(rid, 'Ember', user_message_id=101 if rid == 'reader' else 103, recall_id=rid)
            assert len(result['recall_refs']['events']) == 1, result
        print("prepared", flush=True)
        return
    svc = service(root)
    if action == "kill":
        update = svc.source_completions._update_views
        def block():
            Path(root, "checkpoint").write_text(checkpoint, encoding="utf-8")
            while True:
                time.sleep(0.05)
        if checkpoint == "binding_uncommitted":
            def stop(db, records, bindings):
                assert db.in_transaction
                assert db.execute("SELECT COUNT(*) FROM source_completions").fetchone()[0] == 1
                block()
            svc.source_completions._update_views = stop
        elif checkpoint == "first_view_uncommitted":
            class PausingDB:
                def execute(self, sql, *args):
                    result = db_for_pause.execute(sql, *args)
                    if sql.startswith("INSERT INTO recall_sources"):
                        assert db_for_pause.in_transaction
                        assert db_for_pause.execute("SELECT COUNT(*) FROM recall_sources").fetchone()[0] == 1
                        block()
                    return result
            def stop(db, records, bindings):
                nonlocal db_for_pause
                db_for_pause = db
                update(PausingDB(), records, bindings)
            db_for_pause = None
            svc.source_completions._update_views = stop
        elif checkpoint == "views_uncommitted":
            def stop(db, records, bindings):
                update(db, records, bindings)
                assert db.in_transaction
                assert db.execute("SELECT COUNT(*) FROM recall_sources").fetchone()[0] == 2
                block()
            svc.source_completions._update_views = stop
        else:
            reconcile = svc.source_completions.reconcile
            def stop():
                reconcile()  # committed before the terminal hook returns its ACK
                block()
            svc.source_completions.reconcile = stop
        finish(svc)
        raise AssertionError("checkpoint never reached")
    ack = finish(svc)
    assert ack["ok"], ack
    for _ in range(3):
        for rid in ("reader", "reader2"):
            view = svc.sidecar_recall_snapshot(rid)
            assert view["sources_revision"] == 1, view
            assert view["source_candidates"][0]["turn_messages"][0]["assistant_message_id"] == "75"
        assert finish(svc) == ack
    assert svc._live_turn is None and not svc._generation_contexts
    print(json.dumps({"revision": 1, "writer_reopened": False}), flush=True)

if __name__ == "__main__":
    main()
