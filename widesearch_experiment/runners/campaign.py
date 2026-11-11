"""Ordered phases with explicit engineering gates; survives terminal closure."""
import argparse
import asyncio
from collections import Counter

from ..common import ROOT, SYSTEMS, atomic_json, config, now, read_json
from ..analysis.summarize import load_records, summarize
from ..evaluation.regrade import regrade
from .batch import run_batch


async def campaign(prefix, concurrency, full=False):
    cfg = config()
    status_path = ROOT / "results" / f"{prefix}-campaign.json"
    phases = [("pilot10", "pilot10", list(SYSTEMS), 1), ("batch30", "batch30", list(SYSTEMS), 1),
              ("full200", "full200", list(SYSTEMS), 3), ("ablation", "full200", ["ours_off", "ours"], 3)]
    if not full:
        phases = [("smoke2", "smoke2", list(SYSTEMS), 1)]
    for phase, split, systems, trials in phases:
        label = f"{prefix}-{phase}"
        atomic_json(status_path, {"status": "running", "phase": phase, "label": label, "updated_at": now()})
        await run_batch(label, systems, split, cfg, trials, concurrency)
        await regrade(label)
        summarize(label)
        rows = load_records(ROOT / "results" / label)
        expected = len(read_json(ROOT / "datasets/public/splits.json")[split]) * len(systems) * trials
        unscored = sum(r["score_status"] != "ok" for r in rows)
        no_success = [s for s in systems if not any(r["system"] == s and r["status"] == "success" for r in rows)]
        if len(rows) != expected or unscored or no_success:
            atomic_json(status_path, {"status": "needs_engineering_review", "phase": phase, "label": label,
                        "completed": len(rows), "expected": expected, "unscored": unscored,
                        "systems_without_valid_output": no_success, "updated_at": now()})
            return
        # A low quality score is not a gate. Only execution/format failures may
        # stop expansion; no optimization is performed against the gold answers.
        rates = {s: sum(r["status"] != "success" for r in rows if r["system"] == s) /
                    sum(r["system"] == s for r in rows) for s in systems}
        if phase in {"pilot10", "batch30"} and any(v > .2 for v in rates.values()):
            atomic_json(status_path, {"status": "needs_engineering_review", "phase": phase, "label": label,
                                     "execution_failure_rates": rates, "updated_at": now()})
            return
    if full:
        for phase in ("full200", "ablation"):
            await regrade(f"{prefix}-{phase}", snapshots=True)
            summarize(f"{prefix}-{phase}")
    atomic_json(status_path, {"status": "complete", "updated_at": now()})


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--prefix", default="unified-v1")
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--full", action="store_true", help="Explicitly enable 10/30/200/ablation campaign; default is smoke2 only")
    args = p.parse_args()
    asyncio.run(campaign(args.prefix, args.concurrency, args.full))
