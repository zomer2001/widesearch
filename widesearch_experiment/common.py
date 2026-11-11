from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
APP = WORKSPACE / "research-workbench"
SYSTEMS = ("scrapy", "crawl4ai", "mindsearch", "open_deep_research", "searchos", "ours")


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".writing-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False, default=str)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path):
    return json.loads(Path(path).read_text())


def safe_error(exc):
    # No request headers, payloads, or provider error bodies in public errors.
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    return {"type": type(exc).__name__, "status_code": status}


def credential_env(run_cfg=None):
    """Only called by launcher/scorer, never included in manifests or agent inputs."""
    env = dict(os.environ)
    private = APP / ".workbench/private.json"
    cfg = read_json(private) if private.exists() else {}
    provider = (run_cfg or {}).get("model_provider", "openai")
    if provider not in {"openai", "deepseek"}:
        raise ValueError(f"Unsupported model provider: {provider}")
    endpoint = cfg.get(provider, {})
    prefix = provider.upper()
    default_base = "https://api.deepseek.com" if provider == "deepseek" else "https://api.openai.com/v1"
    key = env.get(f"{prefix}_API_KEY") or endpoint.get("api_key", "")
    base = env.get(f"{prefix}_BASE_URL") or endpoint.get("api_base") or default_base
    if not key:
        raise RuntimeError(f"Missing {prefix}_API_KEY and saved workbench {provider} credential")
    # Compatible SDK aliases are process-local. Never send the OpenAI key to DeepSeek.
    env[f"{prefix}_API_KEY"], env[f"{prefix}_BASE_URL"] = key, base
    env["OPENAI_API_KEY"], env["OPENAI_BASE_URL"] = key, base
    env["LANGSMITH_TRACING"] = "false"
    env["LANGCHAIN_TRACING_V2"] = "false"
    return env


def public_tasks():
    rows = [json.loads(x) for x in (ROOT / "datasets/public/tasks.jsonl").read_text().splitlines() if x.strip()]
    assert all(set(x) == {"instance_id", "query", "language"} for x in rows)
    return rows


def validate_task(task):
    if set(task) != {"instance_id", "query", "language"}:
        raise ValueError("Agent input must contain only instance_id, query, language")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", task["instance_id"]):
        raise ValueError("Invalid task id")


def config(path=None):
    value = read_json(path or ROOT / "configs/unified.json")
    if value.get("ready") is False:
        raise ValueError("Native configuration is a template; configure its model services first")
    return value


def sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
