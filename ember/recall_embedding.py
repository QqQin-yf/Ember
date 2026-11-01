"""Shared recall_embed gateway and vector helpers for active search and indexes."""
import math
import os

from .env import load_env

load_env(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def embedding_model():
    from .model_config import settings
    return settings('recall_embed',require_key=False).model


def embed_text(text: str, timeout=None, retries=None) -> list[float]:
    """Recall vector space selected by the recall_embed purpose."""
    from .model_gateway import call_model, ModelCallError
    data=call_model('recall_embed',{'input':text},timeout_seconds=timeout,retries=retries)
    try:
        vector=data['data'][0]['embedding']
        if (not isinstance(vector,list) or not vector or any(type(v) not in (int,float) or
            not math.isfinite(v) for v in vector) or not any(vector)):
            raise ValueError
        return vector
    except (KeyError,TypeError,ValueError,IndexError):
        raise ModelCallError('response_invalid') from None


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


