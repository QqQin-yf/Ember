"""Fetch and persist one original conversation turn without invoking a model."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ember.env import load_env
load_env(str(Path(__file__).resolve().parents[1]))
from ember import source_client
from ember.turn_slices import TurnSliceStore, fetch_and_save_turn


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path,
                        help="Destination instance memory directory")
    parser.add_argument("--instance", default="default")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--user-message-id", type=int)
    parser.add_argument("--assistant-message-id", type=int, required=True)
    parser.add_argument("--user-is-system", action="store_true",
                        help="Exclude the system-generated user-side message from the slice")
    args = parser.parse_args()
    try:
        store = TurnSliceStore(str(args.data_dir))
        saved = fetch_and_save_turn(store, args.session_id, args.assistant_message_id,
            user_message_id=args.user_message_id, instance=args.instance,
            user_is_system=args.user_is_system)
        # Reopen the database to verify the durable copy, without printing text.
        stored = TurnSliceStore(str(args.data_dir)).get(saved["slice_id"])
        if stored != saved:
            raise RuntimeError("Saved slice did not round-trip")
    except source_client.SourceUnavailable as exc:
        print(json.dumps({"status": "failed", "code": exc.code}))
        return 1
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        return 1
    print(json.dumps({"status": "saved", "slice_id": stored["slice_id"],
        "path": str(store.path), "message_ids": [m["message_id"] for m in stored["messages"]],
        "roles": [m["role"] for m in stored["messages"]],
        "content_lengths": [len(m["content"]) for m in stored["messages"]],
        "user_is_system": stored["user_is_system"], "reopened_verified": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
