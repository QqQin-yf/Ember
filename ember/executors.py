"""Compatibility facade for offline Direction tools; all model calls use the gateway."""
from .model_config import settings
from .model_gateway import call_model

ALLOWED_ENGINES=('glm','qwen3')
KNOWN_TASKS=('split','mount')
PURPOSES={'split':'direction_split','mount':'direction_mount'}


def load_executors(base_dir=None):
    return {task:('qwen3' if settings(purpose,require_key=False).transport=='docker' else 'glm')
            for task,purpose in PURPOSES.items()}


def load_engine(base_dir,task,override=None):
    if task not in PURPOSES:raise ValueError('Unknown executor task: '+task)
    if override is not None:
        if override not in ALLOWED_ENGINES:raise ValueError('Unknown engine: '+override)
        return override
    return load_executors()[task]


def call_llm(system,user,engine,*,task):
    if task not in PURPOSES:raise ValueError('Unknown executor task: '+task)
    if engine not in ALLOWED_ENGINES:raise ValueError('Unknown engine: '+engine)
    purpose='direction_local' if engine=='qwen3' else PURPOSES[task]
    data=call_model(purpose,{'messages':[{'role':'system','content':system},{'role':'user','content':user}]})
    return data['choices'][0]['message']['content']
