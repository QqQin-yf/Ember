"""Run the real sidecar on loopback with private, isolated integration data.

Prepare token.txt in an access-restricted runtime directory outside this repo.
No project .env is loaded; token contents never appear in arguments or output.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8878)
    parser.add_argument("--instances", default="default,phase2")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    runtime = args.runtime_dir.resolve()
    if runtime == repo or repo in runtime.parents:
        parser.error("runtime-dir must be outside the repository")
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    token_path = runtime / "token.txt"
    token = token_path.read_text(encoding="utf-8-sig").strip()
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        parser.error("token.txt must contain an ASCII token of at least 32 characters")

    # Changes affect only this child process. Do not inherit production Ember
    # settings or remote-model credentials, or read them from the project .env.
    credentials = {"OPENAI_API_KEY", "SILICONFLOW_API_KEY", "ZHIPU_API_KEY",
                   "GLM_API_KEY", "ANTHROPIC_API_KEY"}
    for key in list(os.environ):
        if key.startswith("EMBER_") or key in credentials:
            del os.environ[key]
    os.environ.update({
        "EMBER_HOST": "127.0.0.1",
        "EMBER_SIDECAR_PORT": str(args.port),
        "EMBER_DATA_ROOT": str(runtime / "data"),
        "EMBER_INSTANCES": args.instances,
        "EMBER_TOKEN": token,
        "EMBER_SOURCE_URL": "",
        "EMBER_SOURCE_TOKEN": "",
        "PYTHONUTF8": "1",
    })
    sys.path.insert(0, str(repo))
    from ember import env
    env._ENV_LOADED = True
    import sidecar
    sidecar.main()


if __name__ == "__main__":
    main()
