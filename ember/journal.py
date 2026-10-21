"""Durable JSONL effect append; isolate an interrupted tail before recovery."""
from __future__ import annotations

import json
import os


def append_record(path: str, record: dict) -> None:
    data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    with open(path, "a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell():
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                stream.write(b"\n")
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
