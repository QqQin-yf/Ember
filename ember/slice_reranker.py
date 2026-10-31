"""Independent SiliconFlow VL reranker; never receives legacy recall units."""
from dataclasses import dataclass, field, replace
import json
import math
import os
from pathlib import Path
from .model_config import settings, profile, ModelConfigError
from .model_gateway import call_model, ModelCallError

from .slice_embedding import slice_text


class RerankerError(Exception):
    pass


@dataclass(frozen=True)
class RerankerConfig:
    enabled: bool=False
    model: str=field(default_factory=lambda:settings('rerank',require_key=False).model)
    min_score: float=0.7
    image_min_score: float=0.5
    mixed_min_score: float=0.55
    source_budget_chars: int=4000
    timeout_seconds: float=30

    _settings: object=field(default=None,repr=False,compare=False)

    @classmethod
    def load(cls):
        path=os.environ.get('EMBER_SLICE_RERANK_CONFIG')
        try:
            if path:
                meta=json.loads(Path(path).read_text(encoding='utf-8-sig'))
                if not isinstance(meta,dict) or set(meta)-{'purpose','config','enabled'} or meta.get('purpose')!='rerank':
                    raise ValueError
                if meta.get('enabled') is False:return cls()
                value=profile(path,'rerank')
            else:
                value=settings('rerank',require_key=False)
                if value.options.get('enabled',False):value=settings('rerank')
            config=cls(model=value.model,timeout_seconds=value.timeout_seconds,_settings=value,**value.options)
            if (type(config.enabled) is not bool or
                any(type(score) not in (int,float) or not math.isfinite(score) or not 0<=score<=1
                    for score in (config.min_score,config.image_min_score,config.mixed_min_score)) or
                type(config.source_budget_chars) is not int or config.source_budget_chars<200):
                raise ValueError
            return config
        except FileNotFoundError:
            return cls()
        except (OSError,ValueError,TypeError):
            raise RerankerError('reranker_config_invalid') from None

    def score_floor(self, modality='text'):
        return {'text':self.min_score,'image':self.image_min_score,'mixed':self.mixed_min_score}[modality]


class SliceReranker:
    def __init__(self, config, embed_config):
        self.config,self.embed_config=config,embed_config

    def rank(self, query, sources):
        cfg=self.config._settings or replace(settings('rerank',require_key=False),model=self.config.model,
            base_url=self.embed_config.base_url,api_key=self.embed_config.api_key,timeout_seconds=self.config.timeout_seconds)
        try:
            data=call_model('rerank',{'query':query,'documents':[slice_text(s) for s in sources],
                'top_n':len(sources),'return_documents':False},config=cfg)
            results=data['results']
            if (not isinstance(results,list) or len(results)!=len(sources) or
                    any(not isinstance(r,dict) or type(r.get('index')) is not int or
                        type(r.get('relevance_score')) not in (int,float) or not math.isfinite(r['relevance_score']) or
                        not 0<=r['relevance_score']<=1 for r in results) or
                    {r['index'] for r in results}!=set(range(len(sources)))):
                raise ValueError
            return sorted(results,key=lambda r:(-r['relevance_score'],r['index']))
        except ModelCallError as exc:
            raise RerankerError('reranker_http_'+str(exc.status) if exc.status else
                'reranker_'+('response_invalid' if exc.kind=='response_invalid' else 'unreachable')) from None
        except (ValueError,TypeError,KeyError,IndexError):
            raise RerankerError('reranker_response_invalid') from None
