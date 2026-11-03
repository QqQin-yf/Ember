"""Purpose-based model registry. Secrets are resolved from the environment only."""
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]


class ModelConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ModelSettings:
    purpose: str
    provider: str
    operation: str
    model: str
    base_url: str=''
    api_key: str=field(default='',repr=False)
    key_env: str=''
    timeout_seconds: float=30
    retries: int=0
    backoff_seconds: float=0.5
    proxy: str='environment'
    temperature: float | None=None
    max_tokens: int | None=None
    transport: str='http'
    container: str=''
    command: tuple=()
    arguments: tuple=()
    options: dict=field(default_factory=dict)


def registry(path=None):
    path=Path(path or os.environ.get('EMBER_MODELS_CONFIG') or ROOT/'config/models.json')
    try:
        data=json.loads(path.read_text(encoding='utf-8-sig'))
        if (not isinstance(data,dict) or set(data)-{'providers','purposes','policy'} or
            not isinstance(data.get('providers'),dict) or not isinstance(data.get('purposes'),dict)):
            raise ValueError
        def no_secrets(value):
            if isinstance(value,dict):
                if any(k.casefold() in ('api_key','key','secret','token','password') for k in value):
                    raise ValueError
                for v in value.values():no_secrets(v)
            elif isinstance(value,list):
                for v in value:no_secrets(v)
        no_secrets(data)
        return data
    except FileNotFoundError:
        raise ModelConfigError('model_config_missing') from None
    except (OSError,ValueError,TypeError,AttributeError):
        raise ModelConfigError('model_config_invalid') from None


def settings(purpose, *, require_key=True, path=None):
    data=registry(path)
    try:
        use=data['purposes'][purpose]
        if not isinstance(use,dict) or use.get('operation') not in ('chat','embedding','rerank'):
            raise ValueError
        def provider(name,seen=()):
            if name in seen:raise ValueError
            row=data['providers'][name]
            if not isinstance(row,dict):raise ValueError
            return {**(provider(row['inherits'],seen+(name,)) if row.get('inherits') else {}),**row}
        name=use['provider'];p=provider(name)
        policy={**data.get('policy',{}),**use}
        model=use['model'];transport=p.get('transport','http')
        if not isinstance(model,str) or not model.strip() or transport not in ('http','docker'):
            raise ValueError
        base_env=p.get('base_url_env','')
        if base_env and (not isinstance(base_env,str) or not re.fullmatch('[A-Z][A-Z0-9_]*',base_env)):
            raise ValueError
        base=(os.environ.get(base_env) or p.get('base_url','')).rstrip('/')
        key_env=p.get('key_env','')
        if transport=='http':
            u=urlsplit(base)
            if u.scheme!='https' or not u.hostname or u.username or u.password or u.query or u.fragment:
                raise ValueError
            if not isinstance(key_env,str) or not re.fullmatch('[A-Z][A-Z0-9_]*',key_env):
                raise ValueError
        timeout=policy.get('timeout_seconds',30);retries=policy.get('retries',0);backoff=policy.get('backoff_seconds',0.5)
        proxy=policy.get('proxy','environment');tokens=policy.get('max_tokens');temp=policy.get('temperature')
        if (type(timeout) not in (int,float) or not math.isfinite(timeout) or timeout<=0 or
            type(retries) is not int or not 0<=retries<=5 or type(backoff) not in (int,float) or not math.isfinite(backoff) or not 0<=backoff<=60 or
            not isinstance(proxy,str) or (proxy not in ('environment','direct') and not re.fullmatch('env:[A-Z][A-Z0-9_]*',proxy)) or
            (tokens is not None and (type(tokens) is not int or tokens<1)) or
            (temp is not None and (type(temp) not in (int,float) or not math.isfinite(temp))) or
            not isinstance(use.get('options',{}),dict)):
            raise ValueError
        if transport=='docker' and (not p.get('container') or not isinstance(p.get('command'),list) or not p['command'] or
                                   any(not isinstance(s,str) for s in p['command']+p.get('arguments',[]))):
            raise ValueError
    except (KeyError,ValueError,TypeError,AttributeError):
        raise ModelConfigError('model_purpose_invalid:'+str(purpose)) from None
    if require_key:
        from .env import load_env
        load_env(str(ROOT))
    key=os.environ.get(key_env,'').strip() if require_key and key_env else ''
    if require_key and transport=='http' and (not key or '\n' in key or '\r' in key):
        raise ModelConfigError('model_key_missing:'+key_env)
    return ModelSettings(purpose,name,use['operation'],model.strip(),base,key,key_env,timeout,retries,backoff,proxy,temp,tokens,
        transport,p.get('container',''),tuple(p.get('command',[])),tuple(p.get('arguments',[])),use.get('options',{}))


def profile(path,purpose):
    """Optional non-secret purpose reference used by CLI/test fixtures."""
    if path is None:
        return settings(purpose)
    try:
        data=json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if not isinstance(data,dict) or set(data)-{'purpose','enabled','config'}:
            raise ValueError
        if data.get('purpose')!=purpose:
            raise ValueError
        if data.get('enabled') is False:
            raise ModelConfigError('model_profile_disabled')
        config=(Path(path).parent/data['config']) if data.get('config') else None
        return settings(purpose,path=config)
    except FileNotFoundError:
        raise ModelConfigError('model_profile_missing') from None
    except (OSError,ValueError,TypeError,AttributeError) as exc:
        if isinstance(exc,ModelConfigError):raise
        raise ModelConfigError('model_profile_invalid') from None
