"""Validate model purposes without calling providers or printing credentials."""
from pathlib import Path
import json
import os
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from ember.env import load_env
from ember.model_config import registry,settings,ModelConfigError


def main():
    load_env(str(ROOT))
    try:
        rows=[]
        for purpose in registry()['purposes']:
            cfg=settings(purpose,require_key=False)
            rows.append({'purpose':purpose,'provider':cfg.provider,'model':cfg.model,
                'key_env':cfg.key_env,'credential_present':bool(os.environ.get(cfg.key_env)) if cfg.key_env else None,
                'timeout_seconds':cfg.timeout_seconds,'retries':cfg.retries,'proxy':cfg.proxy,'transport':cfg.transport})
        print(json.dumps({'status':'ok','purposes':rows},ensure_ascii=False,indent=2))
        return 0
    except ModelConfigError as exc:
        print(json.dumps({'status':'failed','code':str(exc)}));return 1


if __name__=='__main__':raise SystemExit(main())
