"""Isolated worker routing and reproducible budgets; old experiments stay intact."""
from contextlib import contextmanager
from pathlib import Path

from widesearch_baselines.launcher import validate_config as validate_base
from widesearch_experiment.common import atomic_json, now, sha_file
from widesearch_fetch_backup.launcher import backup_runner
from . import REVISION, SYSTEM

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "deepseek.json"


def validate_config(cfg):
    validate_base(cfg)
    if cfg.get("odr_lite_revision") != REVISION or cfg.get("fetch_backup_revision") != "fetch-backup-v1":
        raise ValueError("Use the versioned ODR Lite configuration with the fetch backup")
    for key in ("odr_research_model_limit", "odr_search_limit", "max_pages", "search_results",
                "odr_page_excerpt_chars", "odr_tool_context_chars", "odr_final_context_chars",
                "odr_workflow_timeout_seconds", "max_output_tokens", "model_timeout_seconds"):
        if type(cfg.get(key)) is not int or cfg[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    if cfg["odr_research_model_limit"] > 7:
        raise ValueError("ODR Lite v1 permits at most 7 research requests plus 1 final table request")
    if cfg["conversion"] != "deterministic" or cfg["execution_attempts"] != 1 or cfg["judge_attempts"] != 1:
        raise ValueError("ODR Lite has no model format repair or whole-task/judge reruns")
    if (cfg["max_concurrent_research_units"], cfg["max_researcher_iterations"], cfg["max_react_tool_calls"]) != (2, 1, 2):
        raise ValueError("ODR Lite v1 uses two researchers, one delegation round, two researcher turns")
    if cfg["research_timeout_seconds"] < cfg["odr_workflow_timeout_seconds"] + cfg["model_timeout_seconds"] + 10:
        raise ValueError("Reserve time for the final table after research stops")


@contextmanager
def lite_runner(cfg):
    with backup_runner() as batch:
        previous_run, previous_hashes = batch.subprocess_run, batch.code_hashes

        async def dispatch(args, *pos, **kwargs):
            args = list(args)
            if args[1:3] == ["-m", "widesearch_experiment.runners.worker"]:
                args[2] = "widesearch_odr_lite.worker"
            elif args[1:3] == ["-m", "widesearch_experiment.evaluation.official"] and not cfg.get("odr_evaluate", False):
                destination = args[args.index("--output") + 1]
                atomic_json(destination, {"instance_id": args[args.index("--instance") + 1],
                            "status": "skipped", "reason": "evaluation_not_requested", "metrics": None,
                            "strict_success": None, "scored_at": now()})
                return 0, False
            return await previous_run(args, *pos, **kwargs)

        def hashes():
            return {**previous_hashes(), **{
                "../widesearch_odr_lite/" + str(p.relative_to(ROOT)): sha_file(p)
                for p in sorted(ROOT.rglob("*.py"))}}

        batch.subprocess_run, batch.code_hashes = dispatch, hashes
        try:
            yield batch
        finally:
            batch.subprocess_run, batch.code_hashes = previous_run, previous_hashes


async def run_batch(label, instances, cfg):
    validate_config(cfg)
    with lite_runner(cfg) as batch:
        return await batch.run_batch(label, [SYSTEM], None, cfg, trials=1, concurrency=1, instances=instances)
