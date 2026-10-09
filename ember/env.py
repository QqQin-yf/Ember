# -*- coding: utf-8 -*-
"""环境变量加载：启动时从项目根 .env 读入（不覆盖已有环境变量）。

密钥不进代码、不入 git——.env 被 .gitignore 排除。
"""
import os

_ENV_LOADED = False


def load_env(base_dir: str) -> None:
    """读 base_dir/.env（KEY=VALUE 每行一条，# 开头为注释），幂等。"""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    path = os.path.join(base_dir, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key and key not in os.environ:   # 已有的环境变量优先
                os.environ[key] = value
