"""Retry/refresh grading without touching saved predictions or rerunning agents."""
import argparse
import asyncio
from pathlib import Path

from ..common import ROOT, atomic_json, credential_env, read_json
from ..runners.batch import subprocess_run


async def regrade(label, all_scores=False, snapshots=False):
    root = ROOT / "results" / label
    cfg = read_json(root / "config.json")
    env = credential_env(cfg)
    python = str(ROOT / ".venv/bin/python")
    for selected_file in sorted((root / "raw").glob("*/*/trial_*/selected.json")):
        selected = read_json(selected_file)
        record = read_json(root / selected["result"])
        job_id = f"{record['system']}/{record['instance_id']}/trial_{record['trial']}"
        score_dir = root / "scores" / job_id
        score_dir.mkdir(parents=True, exist_ok=True)
        previous = read_json(root / selected["score"]) if selected.get("score") else {}
        if all_scores or previous.get("status") != "ok":
            number = 1
            while (score_dir / f"regrade_{number}.json").exists():
                number += 1
            for attempt in range(cfg["judge_attempts"]):
                output = score_dir / f"regrade_{number + attempt}.json"
                await subprocess_run([python, "-m", "widesearch_experiment.evaluation.official", "--instance", record["instance_id"],
                    "--prediction", str(root / selected["prediction"]), "--output", str(output), "--config", str(root / "config.json"),
                    "--manifest", str(root / "frozen.json")],
                    output.with_suffix(".log"), env, 1200)
                if output.exists():
                    selected["score"] = str(output.relative_to(root))
                    atomic_json(selected_file, selected)
                    if read_json(output)["status"] == "ok":
                        break
        if snapshots:
            import json
            directory = (root / selected["result"]).parent
            events = [json.loads(x) for x in (directory / "events.jsonl").read_text().splitlines()]
            observations = [e for e in events if e["kind"] == "table_snapshot"]
            # Use the last actual table observed at or before each predeclared
            # call budget. Never create a past table from the final response.
            selected_events = {}
            for budget in [1, 3, 5, 10, 20, 40, 80, 160]:
                candidates = [e for e in observations if e["search_calls"] <= budget]
                if candidates:
                    selected_events[candidates[-1]["seq"]] = candidates[-1]
            for seq, event in selected_events.items():
                prediction = directory / "snapshots" / f"{seq:06}.md"
                if not prediction.exists():
                    continue
                target = score_dir / "snapshots" / f"{seq:06}.json"
                target.parent.mkdir(exist_ok=True)
                if not target.exists():
                    await subprocess_run([python, "-m", "widesearch_experiment.evaluation.official", "--instance", record["instance_id"],
                        "--prediction", str(prediction), "--output", str(target), "--config", str(root / "config.json"),
                        "--manifest", str(root / "frozen.json")],
                        target.with_suffix(".log"), env, 1200)
                if target.exists():
                    data = read_json(target)
                    data.update(search_calls=event["search_calls"], observed_elapsed_seconds=event["elapsed_seconds"],
                                snapshot_seq=seq, system=record["system"], trial=record["trial"])
                    atomic_json(target, data)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--label", required=True)
    p.add_argument("--all", action="store_true")
    p.add_argument("--snapshots", action="store_true")
    a = p.parse_args()
    asyncio.run(regrade(a.label, a.all, a.snapshots))
