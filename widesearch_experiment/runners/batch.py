import asyncio
import fcntl
import json
import os
import random
import signal
import sys
from pathlib import Path

from ..common import ROOT, WORKSPACE, atomic_json, credential_env, digest, now, public_tasks, read_json, sha_file


def code_hashes():
    return {str(p.relative_to(ROOT)): sha_file(p) for p in sorted(ROOT.rglob("*.py"))
            if not {"vendor", "snapshots", "results", ".venv", ".venv-mindsearch"}.intersection(p.relative_to(ROOT).parts)}


async def subprocess_run(args, logfile, env, timeout):
    with Path(logfile).open("w") as log:
        process = await asyncio.create_subprocess_exec(*args, cwd=WORKSPACE, env=env,
                                                       stdout=log, stderr=log, start_new_session=True)
        try:
            return await asyncio.wait_for(process.wait(), timeout), False
        except (TimeoutError, asyncio.CancelledError) as exc:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                await asyncio.wait_for(process.wait(), 10)
            except (ProcessLookupError, TimeoutError):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            return process.returncode, True


async def run_batch(label, systems, split, cfg, trials, concurrency, instances=None):
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]+", label) or trials < 1 or concurrency < 1:
        raise ValueError("Label must be a simple directory name; trials and concurrency must be positive")
    root = ROOT / "results" / label
    root.mkdir(parents=True, exist_ok=True)
    lock = (root / ".lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    manifest = read_json(ROOT / "manifest.json")
    if sha_file(ROOT / "datasets/public/tasks.jsonl") != manifest["public_tasks_sha256"]:
        raise RuntimeError("Public task file differs from pinned dataset")
    manifest.pop("created_at", None)
    ids = instances or read_json(ROOT / "datasets/public/splits.json")[split]
    task_map = {t["instance_id"]: t for t in public_tasks()}
    assert set(ids).issubset(task_map)
    import subprocess
    dependencies = {}
    for runtime in (".venv", ".venv-mindsearch"):
        dependencies[runtime] = subprocess.check_output([str(ROOT / runtime / "bin/python"), "-m", "pip", "freeze", "--all"], text=True).splitlines()
    frozen = {"config": cfg, "systems": systems, "instances": ids, "trials": trials, "dependencies": dependencies,
              "manifest": manifest, "experiment_code": code_hashes()}
    fingerprint = digest(frozen)
    if (root / "frozen.json").exists():
        if read_json(root / "frozen.json")["fingerprint"] != fingerprint:
            raise RuntimeError("Frozen configuration/code differs. Use a new --label; never mix runs.")
    else:
        atomic_json(root / "frozen.json", {"fingerprint": fingerprint, "frozen_at": now(), **frozen})
    atomic_json(root / "config.json", cfg)
    env = credential_env(cfg)
    env["PYTHONPATH"] = str(WORKSPACE)
    main_python = str(ROOT / ".venv/bin/python")
    semaphore = asyncio.Semaphore(concurrency)
    jobs = [(s, i, t) for t in range(1, trials + 1) for i in ids for s in systems]
    random.Random(cfg["seed"]).shuffle(jobs)
    progress = {"label": label, "total": len(jobs), "completed": 0, "failed": 0, "running": [], "started_at": now()}

    def save_progress():
        atomic_json(root / "progress.json", {**progress, "updated_at": now()})

    async def one(system, instance, trial):
        async with semaphore:
            job_id = f"{system}/{instance}/trial_{trial}"
            target = root / "raw" / job_id
            target.mkdir(parents=True, exist_ok=True)
            if (target / "selected.json").exists():
                prior = read_json(target / "selected.json")
                if read_json(root / prior["result"])["status"] != "success":
                    progress["failed"] += 1
                progress["completed"] += 1
                save_progress()
                return
            progress["running"].append(job_id)
            save_progress()
            try:
                selected = None
                for attempt in range(1, cfg["execution_attempts"] + 1):
                    directory = target / f"attempt_{attempt}"
                    directory.mkdir(exist_ok=True)
                    request = {"task": task_map[instance], "system": system, "trial": trial, "attempt": attempt,
                               "config": {**cfg, "seed": cfg["seed"] + trial - 1,
                                          "_ours_snapshot": manifest["ours_snapshot"]}, "config_fingerprint": fingerprint}
                    atomic_json(directory / "request.json", request)
                    result_path = directory / "result.json"
                    if not result_path.exists() or read_json(result_path).get("status") == "running":
                        python = str(ROOT / ".venv-mindsearch/bin/python") if system == "mindsearch" else main_python
                        code, timed_out = await subprocess_run(
                            [python, "-m", "widesearch_experiment.runners.worker", "--request", str(directory / "request.json"),
                             "--directory", str(directory)], directory / "worker.log", env,
                            cfg["research_timeout_seconds"] + cfg["conversion_timeout_seconds"] + 120)
                        if not result_path.exists() or "ended_at" not in read_json(result_path):
                            partial = read_json(result_path) if result_path.exists() else {}
                            partial.update(instance_id=instance, system=system, trial=trial,
                                           status="timeout" if timed_out else "worker_error", ended_at=now(), exit_code=code)
                            for field in ("model_calls", "search_calls", "pages_fetched", "input_tokens", "output_tokens", "elapsed_seconds"):
                                partial.setdefault(field, None)
                            partial.setdefault("response", "")
                            atomic_json(result_path, partial)
                    result = read_json(result_path)
                    selected = directory
                    # Never select an attempt by its answer quality or score.
                    if result["status"] in {"success", "parse_error", "conversion_error"}:
                        break
                prediction = selected / "prediction/prediction.md"
                if not prediction.exists():
                    prediction.parent.mkdir(exist_ok=True)
                    prediction.write_text("")
                score_dir = root / "scores" / job_id
                score_dir.mkdir(parents=True, exist_ok=True)
                final_score = None
                for scoring_attempt in range(1, cfg["judge_attempts"] + 1):
                    score_path = score_dir / f"attempt_{scoring_attempt}.json"
                    if not score_path.exists():
                        await subprocess_run([main_python, "-m", "widesearch_experiment.evaluation.official",
                                              "--instance", instance, "--prediction", str(prediction),
                                              "--output", str(score_path), "--config", str(root / "config.json"),
                                              "--manifest", str(root / "frozen.json")],
                                             score_dir / f"attempt_{scoring_attempt}.log", env, 1200)
                    if score_path.exists():
                        final_score = score_path
                        if read_json(score_path)["status"] == "ok":
                            break
                selected_data = {"result": str((selected / "result.json").relative_to(root)),
                                 "prediction": str(prediction.relative_to(root)),
                                 "score": str(final_score.relative_to(root)) if final_score else None}
                attempts = [read_json(p) for p in target.glob("attempt_*/result.json")]
                totals = {"attempts": len(attempts), "failed_attempts": sum(r["status"] != "success" for r in attempts)}
                for field in ("elapsed_seconds", "model_calls", "search_calls", "pages_fetched", "input_tokens", "output_tokens"):
                    totals[field] = sum(r[field] for r in attempts) if all(r.get(field) is not None for r in attempts) else None
                selected_data["attempt_totals"] = totals
                atomic_json(target / "selected.json", selected_data)
                progress["completed"] += 1
                if result["status"] != "success":
                    progress["failed"] += 1
            except Exception as exc:
                atomic_json(target / "queue_error.json", {"type": type(exc).__name__, "at": now()})
                progress["failed"] += 1
            finally:
                progress["running"].remove(job_id)
                save_progress()
                print(json.dumps({"finished": job_id, "completed": progress["completed"], "total": len(jobs)}, ensure_ascii=False), flush=True)

    try:
        save_progress()
        await asyncio.gather(*(one(*job) for job in jobs))
        progress["ended_at"] = now()
        save_progress()
    finally:
        lock.close()
