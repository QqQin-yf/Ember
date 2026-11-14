"""Local token accounting for briefing output."""
from functools import lru_cache


@lru_cache(maxsize=1)
def encoding():
    from tiktoken import get_encoding
    return get_encoding('o200k_base')


@lru_cache(maxsize=2048)
def tokens(text):
    return len(encoding().encode_ordinary(text))


def join(blocks):
    return '\n'.join(block for block in blocks if block)


def fit(items, budget, text):
    selected=[]
    used=0
    for item in items:
        cost=tokens(text(item)) + bool(selected)
        if used+cost>budget:
            break
        selected.append(item)
        used+=cost
    while selected and tokens(join(text(item) for item in selected))>budget:
        selected.pop()
    return selected
