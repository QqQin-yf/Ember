"""Single model transport: purpose routing, auth, proxy, retries and safe logs."""
from dataclasses import replace
import json
import logging
import math
import os
import subprocess
import time
import urllib.error
import urllib.request
from .model_config import settings, ModelConfigError

log=logging.getLogger('ember.models')


def _vertex_request(cfg, body):
    from google.auth import default
    from google.auth.transport.requests import AuthorizedSession
    credentials, project = default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
    project = os.environ.get('GOOGLE_CLOUD_PROJECT') or project
    if not project:
        raise ModelConfigError('vertex_project_missing')
    from urllib.parse import quote
    base = 'https://aiplatform.googleapis.com/v1/projects/' + quote(project, safe='')
    url = base + '/locations/global/endpoints/openapi/chat/completions'
    with AuthorizedSession(credentials) as session:
        response = session.post(url, json=body, timeout=cfg.timeout_seconds)
        if not response.ok:
            raise ModelCallError('http', response.status_code)
        return response.json()


class ModelCallError(RuntimeError):
    def __init__(self,kind,status=None):
        self.kind,self.status=kind,status
        super().__init__('model_http_'+str(status) if status else 'model_'+kind)


def _safe_purpose(purpose):
    try:
        return settings(purpose, require_key=False).purpose
    except (ModelConfigError, TypeError):
        return 'unknown'


def call_model(purpose, payload, *, config=None, timeout_seconds=None, retries=None,
               source_root=None, sources=(), private=False, delayed=False, query_source=None,
               static_input=False):
    from .privacy import authorize, offline_sources, PrivateMaterial
    inherited = offline_sources.get()
    if inherited is not None and source_root is None:
        source_root, sources = inherited
    try:
        if query_source is not None and source_root is None and not static_input:
            raise PrivateMaterial('private_source_unverified')
        with authorize(source_root, sources, private=private, delayed=delayed, query_source=query_source,
                       source_required=not static_input and (query_source is None or purpose == 'rerank')):
            pass
    except PrivateMaterial as exc:
        reason = str(exc)
        if reason not in {'private_material', 'private_delay', 'private_cutover_unavailable',
                          'private_source_unverified'}:
            reason = 'private_policy_denied'
        log.warning('model egress denied purpose=%s reason=%s', _safe_purpose(purpose), reason)
        raise
    return _call_model(purpose, payload, config=config, timeout_seconds=timeout_seconds, retries=retries)


def _call_model(purpose,payload,*,config=None,timeout_seconds=None,retries=None):
    cfg=config or settings(purpose)
    if cfg.purpose!=purpose:
        raise ModelConfigError('model_purpose_mismatch')
    if timeout_seconds is not None:cfg=replace(cfg,timeout_seconds=timeout_seconds)
    if retries is not None:cfg=replace(cfg,retries=retries)
    if (type(cfg.timeout_seconds) not in (int,float) or not math.isfinite(cfg.timeout_seconds) or cfg.timeout_seconds<=0 or
            type(cfg.retries) is not int or not 0<=cfg.retries<=5):
        raise ModelConfigError('model_call_policy_invalid')
    if os.environ.get('EMBER_MODEL_NETWORK_DISABLED')=='1':
        raise ModelCallError('network_disabled')
    if 'model' in payload:
        raise ModelConfigError('model_is_selected_by_purpose')
    if cfg.transport=='docker':
        if cfg.operation!='chat':raise ModelConfigError('docker_chat_only')
        prompt='\n\n'.join(m['content'] for m in payload['messages'])
        try:
            result=subprocess.run(['docker','exec','-i',cfg.container,*cfg.command,cfg.model,*cfg.arguments],
                input=prompt.encode('utf-8'),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=cfg.timeout_seconds,check=True)
            log.info('model purpose=%s provider=%s status=ok',purpose,cfg.provider)
            return {'choices':[{'message':{'content':result.stdout.decode('utf-8',errors='replace')}}]}
        except (OSError,subprocess.SubprocessError):
            log.warning('model purpose=%s provider=%s status=local_failed',purpose,cfg.provider)
            raise ModelCallError('local_failed') from None
    body=dict(payload,model=cfg.model)
    if cfg.operation=='chat':
        if cfg.temperature is not None:body.setdefault('temperature',cfg.temperature)
        if cfg.max_tokens is not None:body.setdefault('max_tokens',cfg.max_tokens)
    endpoint={'chat':'/chat/completions','embedding':'/embeddings','rerank':'/rerank'}[cfg.operation]
    if cfg.transport == 'vertex':
        from google.auth.exceptions import GoogleAuthError
        from requests.exceptions import RequestException
        try:
            return _vertex_request(cfg, body)
        except (GoogleAuthError, RequestException):
            raise ModelCallError('vertex_unavailable') from None
    for attempt in range(cfg.retries+1):
        start=time.monotonic();failure=None
        try:
            request=urllib.request.Request(cfg.base_url+endpoint,data=json.dumps(body,ensure_ascii=False,allow_nan=False).encode('utf-8'),
                headers={'Content-Type':'application/json','Authorization':'Bearer '+cfg.api_key})
            if cfg.proxy=='environment':
                response=urllib.request.urlopen(request,timeout=cfg.timeout_seconds)
            else:
                proxies={}
                if cfg.proxy.startswith('env:'):
                    proxy=os.environ.get(cfg.proxy[4:],'')
                    if not proxy:raise ModelCallError('proxy_missing')
                    proxies={'http':proxy,'https':proxy}
                response=urllib.request.build_opener(urllib.request.ProxyHandler(proxies)).open(request,timeout=cfg.timeout_seconds)
            with response:
                data=json.loads(response.read())
            if not isinstance(data,dict):raise ValueError
            log.info('model purpose=%s provider=%s attempt=%s status=ok elapsed_ms=%s',purpose,cfg.provider,attempt+1,round((time.monotonic()-start)*1000))
            return data
        except urllib.error.HTTPError as exc:
            failure=ModelCallError('http',exc.code);exc.close()
            retryable=exc.code in (408,429) or 500<=exc.code<=599
        except (urllib.error.URLError,OSError,TimeoutError):
            failure=ModelCallError('unreachable');retryable=True
        except (ValueError,TypeError,UnicodeError):
            failure=ModelCallError('response_invalid');retryable=False
        log.warning('model purpose=%s provider=%s attempt=%s status=%s',purpose,cfg.provider,attempt+1,str(failure))
        if not retryable or attempt==cfg.retries:
            raise failure from None
        time.sleep(cfg.backoff_seconds*(2**attempt))
