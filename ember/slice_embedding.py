"""One full turn slice -> one SiliconFlow vector, with recoverable local state."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from .turn_slices import TurnSliceStore


DEFAULT_MODEL = "Qwen/Qwen3-VL-Embedding-8B"
DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
INPUT_FORMAT = "turn-roles-v1"


class SliceEmbeddingError(Exception):
    """Only a fixed error code crosses the CLI/storage boundary."""


@dataclass(frozen=True)
class SliceEmbeddingConfig:
    api_key: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_seconds: float = 30

    @classmethod
    def load(cls, path: str | Path) -> SliceEmbeddingConfig:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            raise SliceEmbeddingError("config_not_found") from None
        except (OSError, ValueError):
            raise SliceEmbeddingError("config_invalid") from None
        if not isinstance(data, dict):
            raise SliceEmbeddingError("config_invalid")
        key = data.get("api_key", "")
        if not isinstance(key, str) or not key.strip():
            raise SliceEmbeddingError("api_key_missing")
        base = data.get("base_url", DEFAULT_BASE_URL)
        model = data.get("model", DEFAULT_MODEL)
        timeout = data.get("timeout_seconds", 30)
        if not isinstance(base, str) or not isinstance(model, str) or not model.strip():
            raise SliceEmbeddingError("config_invalid")
        try:
            parsed = urllib.parse.urlsplit(base)
        except ValueError:
            raise SliceEmbeddingError("config_invalid") from None
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
                parsed.password or parsed.query or parsed.fragment):
            raise SliceEmbeddingError("config_invalid")
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
            raise SliceEmbeddingError("config_invalid")
        if "\n" in key or "\r" in key:
            raise SliceEmbeddingError("config_invalid")
        return cls(key.strip(), base.rstrip("/"), model.strip(), timeout)

    @property
    def space(self) -> str:
        # Keep provider/model/serialization identifiable, without credentials.
        return json.dumps({"base_url": self.base_url, "model": self.model,
                           "input_format": INPUT_FORMAT}, sort_keys=True)


def slice_text(source: dict) -> str:
    """Preserve both originals and role boundaries; no summary or truncation."""
    return "\n\n".join(f"{m['role']}:\n{m['content']}" for m in source["messages"])


class SliceEmbeddingClient:
    def __init__(self, config: SliceEmbeddingConfig):
        self.config = config

    def embed(self, text: str) -> list[float]:
        body = json.dumps({"model": self.config.model, "input": text,
                           "encoding_format": "float"}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.config.base_url + "/embeddings", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.config.api_key})
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise SliceEmbeddingError(f"embedding_http_{code}") from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise SliceEmbeddingError("embedding_unreachable") from None
        except (ValueError, UnicodeError):
            raise SliceEmbeddingError("embedding_response_invalid") from None
        try:
            items = data["data"]
            if len(items) != 1 or items[0].get("index") != 0:
                raise ValueError
            if data.get("model", self.config.model) != self.config.model:
                raise ValueError
            vector = items[0]["embedding"]
            if (not isinstance(vector, list) or not vector or
                    any(type(v) not in (int, float) or not math.isfinite(v) for v in vector) or
                    not any(v != 0 for v in vector)):
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            raise SliceEmbeddingError("embedding_response_invalid") from None
        return vector


def embed_pending_slices(store: TurnSliceStore, client: SliceEmbeddingClient,
                         limit: int = 100) -> dict:
    space = client.config.space
    pending = store.pending_embeddings(space, limit)
    counts = {"selected": len(pending), "ready": 0, "failed": 0, "changed": 0}
    for source in pending:
        try:
            vector = client.embed(slice_text(source))
        except SliceEmbeddingError as exc:
            stored = store.save_embedding(source, space, None, error_code=str(exc))
            counts["failed" if stored else "changed"] += 1
        else:
            stored = store.save_embedding(source, space, vector)
            counts["ready" if stored else "changed"] += 1
    return counts
