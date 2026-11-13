"""Background proposals are separate from formal memory authoring."""
from .proposals import propose_day


def consume(queue):
    return propose_day(queue.service)
