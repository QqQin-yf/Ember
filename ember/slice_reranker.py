"""Independent SiliconFlow VL reranker; never receives legacy recall units."""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import urllib.error
import urllib.request

from .slice_embedding import slice_text


class RerankerError(Exception):
    pass


@dataclass(frozen=True)
class RerankerConfig:
    enabled: bool=False
    model: str='Qwen/Qwen3-VL-Reranker-8B'
    min_score: float=0.5
    source_budget_chars: int=4000
    timeout_seconds: float=30

    @classmethod
    def load(cls):
        path=os.environ.get('EMBER_SLICE_RERANK_CONFIG') or Path(__file__).resolve().parents[1]/'config/slice-reranker.local.json'
        try:
            value=json.loads(Path(path).read_text(encoding='utf-8-sig'))
            config=cls(**value)
            if (type(config.enabled) is not bool or not isinstance(config.model,str) or not config.model.strip() or
                    type(config.min_score) not in (int,float) or not math.isfinite(config.min_score) or not 0<=config.min_score<=1 or
                    type(config.source_budget_chars) is not int or config.source_budget_chars<200 or
                    type(config.timeout_seconds) not in (int,float) or not math.isfinite(config.timeout_seconds) or config.timeout_seconds<=0):
                raise ValueError
            return config
        except FileNotFoundError:
            return cls()
        except (OSError,ValueError,TypeError):
            raise RerankerError('reranker_config_invalid') from None


class SliceReranker:
    def __init__(self, config, embed_config):
        self.config,self.embed_config=config,embed_config

    def rank(self, query, sources):
        body=json.dumps({'model':self.config.model,'query':query,
            'documents':[slice_text(s) for s in sources],'top_n':len(sources),'return_documents':False},ensure_ascii=False).encode('utf-8')
        request=urllib.request.Request(self.embed_config.base_url+'/rerank',data=body,
            headers={'Content-Type':'application/json','Authorization':'Bearer '+self.embed_config.api_key})
        try:
            with urllib.request.urlopen(request,timeout=self.config.timeout_seconds) as response:
                data=json.loads(response.read())
            results=data['results']
            if (not isinstance(results,list) or len(results)!=len(sources) or
                    any(not isinstance(r,dict) or type(r.get('index')) is not int or
                        type(r.get('relevance_score')) not in (int,float) or not math.isfinite(r['relevance_score']) or
                        not 0<=r['relevance_score']<=1 for r in results) or
                    {r['index'] for r in results}!=set(range(len(sources)))):
                raise ValueError
            return sorted(results,key=lambda r:(-r['relevance_score'],r['index']))
        except urllib.error.HTTPError as exc:
            code=exc.code;exc.close()
            raise RerankerError(f'reranker_http_{code}') from None
        except (urllib.error.URLError,OSError,TimeoutError):
            raise RerankerError('reranker_unreachable') from None
        except (ValueError,TypeError,KeyError,IndexError):
            raise RerankerError('reranker_response_invalid') from None
