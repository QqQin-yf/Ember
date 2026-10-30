"""Compute and persist missing/failed turn-slice vectors using a local config."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from ember.slice_embedding import (SliceEmbeddingConfig, SliceEmbeddingClient,
                                    SliceEmbeddingError, embed_pending_slices)
from ember.turn_slices import TurnSliceStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=HERE / "config/slice-embedding.local.json")
    parser.add_argument("--data-dir", type=Path, default=HERE / "data")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    try:
        config = SliceEmbeddingConfig.load(args.config)
        counts = embed_pending_slices(TurnSliceStore(str(args.data_dir)),
                                      SliceEmbeddingClient(config), args.limit)
    except SliceEmbeddingError as exc:
        print(json.dumps({"status": "failed", "code": str(exc)}))
        return 1
    except (sqlite3.Error, OSError):
        print(json.dumps({"status": "failed", "code": "slice_storage_unavailable"}))
        return 1
    print(json.dumps({"status": "completed" if not counts["failed"] and not counts["changed"] else "incomplete",
                      "model": config.model, **counts}))
    return 0 if not counts["failed"] and not counts["changed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
