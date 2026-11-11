"""New-process routing; current baseline workers and frozen hashes stay intact."""
from contextlib import contextmanager
from pathlib import Path

from widesearch_baselines.launcher import routed_runner, validate_config
from widesearch_experiment.common import sha_file
from . import REVISION

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "deepseek.json"


@contextmanager
def backup_runner():
    with routed_runner() as batch:
        previous_run, previous_hashes = batch.subprocess_run, batch.code_hashes
        async def dispatch(args, *pos, **kwargs):
            args = list(args)
            if args[1:3] == ["-m", "widesearch_experiment.runners.worker"]:
                args[2] = "widesearch_fetch_backup.worker"
            return await previous_run(args, *pos, **kwargs)
        def hashes():
            return {**previous_hashes(), **{
                "../widesearch_fetch_backup/" + str(p.relative_to(ROOT)): sha_file(p)
                for p in sorted(ROOT.rglob("*.py"))}}
        batch.subprocess_run, batch.code_hashes = dispatch, hashes
        try:
            yield batch
        finally:
            batch.subprocess_run, batch.code_hashes = previous_run, previous_hashes


async def run_batch(label, systems, split, cfg, trials, concurrency, instances=None):
    validate_config(cfg)
    if cfg.get("fetch_backup_revision") != REVISION:
        raise ValueError("Use the versioned fetch backup configuration")
    with backup_runner() as batch:
        return await batch.run_batch(label, systems, split, cfg, trials, concurrency, instances)
