"""Run the real sidecar on loopback with private, isolated integration data.

Prepare token.txt in an access-restricted runtime directory outside this repo.
No project .env is loaded; token contents never appear in arguments or output.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


def source_settings(path: Path | None, repo: Path) -> dict[str, str]:
    """Only explicitly supplied loopback source settings; no inherited secrets."""
    if path is None:
        return {"EMBER_SOURCE_URL": "", "EMBER_SOURCE_TOKEN": ""}
    path = path.resolve()
    if path == repo or repo in path.parents:
        raise ValueError("source-env-file must be outside the repository")
    allowed = {"EMBER_SOURCE_URL", "EMBER_SOURCE_TOKEN", "EMBER_SOURCE_TIMEOUT"}
    settings = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key.strip() not in allowed or key.strip() in settings:
            raise ValueError("source-env-file contains unsupported or duplicate settings")
        settings[key.strip()] = value.strip()
    from urllib.parse import urlsplit
    parsed = urlsplit(settings.get("EMBER_SOURCE_URL", ""))
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("isolated source must be an explicit loopback origin")
    secret = settings.get("EMBER_SOURCE_TOKEN", "")
    if len(secret) < 32 or not secret.isascii() or any(c.isspace() for c in secret):
        raise ValueError("isolated source token invalid")
    timeout = float(settings.get("EMBER_SOURCE_TIMEOUT", "8"))
    if not 0 < timeout <= 60:
        raise ValueError("isolated source timeout invalid")
    return settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8878)
    parser.add_argument("--instances", default="default,phase2")
    parser.add_argument("--source-env-file", type=Path)
    parser.add_argument("--source-sample", type=Path)
    parser.add_argument("--source-report", type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    runtime = args.runtime_dir.resolve()
    if runtime == repo or repo in runtime.parents:
        parser.error("runtime-dir must be outside the repository")
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    try:
        source = source_settings(args.source_env_file, repo)
    except (OSError, ValueError):
        parser.error("cannot load valid isolated source configuration (contents withheld)")
    if bool(args.source_sample) != bool(args.source_report):
        parser.error("source-sample and source-report must be provided together")
    if args.source_sample:
        if args.source_env_file is None:
            parser.error("source self-check requires explicit source-env-file")
        for candidate in (args.source_sample, args.source_report):
            if candidate.resolve() == repo or repo in candidate.resolve().parents:
                parser.error("source diagnostic files must be outside the repository")
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
    os.environ.update(source)
    # Only local integration destinations bypass environment/system proxies.
    bypass = os.environ.get("no_proxy", os.environ.get("NO_PROXY", ""))
    bypass = ",".join(filter(None, [bypass, "127.0.0.1", "localhost", "::1"]))
    os.environ["no_proxy"] = os.environ["NO_PROXY"] = bypass
    sys.path.insert(0, str(repo))
    from ember import env
    env._ENV_LOADED = True
    import sidecar
    if args.source_sample:
        from check_source_provider_process import run_checks
        run_checks(sidecar, args.source_sample, args.source_report)
    sidecar.main()


if __name__ == "__main__":
    main()
