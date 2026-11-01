"""Non-secret lifecycle thresholds, separate from model/transport selection."""
import json
import math
import os
from pathlib import Path


def lifecycle_config():
    path=Path(os.environ.get('EMBER_LIFECYCLE_CONFIG') or Path(__file__).resolve().parents[1]/'config/lifecycle.json')
    config=json.loads(path.read_text(encoding='utf-8'))
    for section in config.values():
        if not isinstance(section,dict):
            raise ValueError('invalid_lifecycle_configuration')
        for value in section.values():
            if isinstance(value,(int,float)) and (isinstance(value,bool) or not math.isfinite(value) or value<=0):
                raise ValueError('invalid_lifecycle_threshold')
    return config
