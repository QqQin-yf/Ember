"""Background proposals are separate from formal memory authoring."""
import os

from .proposals import propose_day


def consume(queue):
    return os.environ.get('EMBER_ENABLE_PROPOSALS','0')=='1' and propose_day(queue.service)
