"""Reuse the existing scheduler and scorer with process-local adapter routing."""
from contextlib import contextmanager
from pathlib import Path

from widesearch_experiment.common import sha_file
from . import REVISION

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "deepseek.json"


def validate_config(cfg):
    for name in ("model_provider", "search_provider", "fetch_provider"):
        if cfg.get(name) != "deepseek":
            raise ValueError(f"This baseline profile requires {name}=deepseek")
    if cfg.get("model_api") != "responses" or cfg.get("judge_api") != "responses":
        raise ValueError("This profile requires the Responses API")
    if not cfg.get("model", "").startswith("deepseek-"):
        raise ValueError("A DeepSeek model is required")
    if cfg.get("judge_model") != cfg["model"] or cfg.get("conversion_model") != cfg["model"]:
        raise ValueError("Research, conversion and judging must use the same DeepSeek model")
    if cfg.get("baseline_adapter_revision") != REVISION:
        raise ValueError("Configuration does not match the baseline adapter revision")


@contextmanager
def routed_runner():
    from widesearch_experiment.runners import batch
    original_run, original_hashes = batch.subprocess_run, batch.code_hashes

    async def dispatch(args, *pos, **kwargs):
        args = list(args)
        if args[1:3] == ["-m", "widesearch_experiment.runners.worker"]:
            args[2] = "widesearch_baselines.worker"
        return await original_run(args, *pos, **kwargs)

    def hashes():
        return {**original_hashes(), **{
            "../widesearch_baselines/" + str(p.relative_to(ROOT)): sha_file(p)
            for p in sorted(ROOT.rglob("*.py"))}}

    batch.subprocess_run, batch.code_hashes = dispatch, hashes
    try:
        yield batch
    finally:
        batch.subprocess_run, batch.code_hashes = original_run, original_hashes


async def run_batch(label, systems, split, cfg, trials, concurrency, instances=None):
    validate_config(cfg)
    with routed_runner() as batch:
        return await batch.run_batch(label, systems, split, cfg, trials, concurrency, instances)
