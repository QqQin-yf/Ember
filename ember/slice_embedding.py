"""One full turn slice -> one SiliconFlow vector, with recoverable local state."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import math
from pathlib import Path
from .model_config import settings, profile, ModelConfigError
from .model_gateway import call_model, ModelCallError

from .turn_slices import TurnSliceStore


DEFAULT_MODEL = settings("embed",require_key=False).model
DEFAULT_BASE_URL = settings("embed",require_key=False).base_url
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
    def load(cls, path=None):
        try:
            value=profile(path,'embed')
            return cls(value.api_key,value.base_url,value.model,value.timeout_seconds)
        except ModelConfigError as exc:
            text=str(exc)
            code=('config_not_found' if text=='model_profile_missing' else
                  'api_key_missing' if text.startswith('model_key_missing:') or text=='model_profile_disabled' else 'config_invalid')
            raise SliceEmbeddingError(code) from None

    def request_config(self):
        return replace(settings('embed',require_key=False),api_key=self.api_key,base_url=self.base_url,
                       model=self.model,timeout_seconds=self.timeout_seconds)

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
        try:
            data=call_model('embed',{'input':text,'encoding_format':'float'},config=self.config.request_config())
        except ModelCallError as exc:
            raise SliceEmbeddingError('embedding_http_'+str(exc.status) if exc.status else
                'embedding_'+('response_invalid' if exc.kind=='response_invalid' else 'unreachable')) from None
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
