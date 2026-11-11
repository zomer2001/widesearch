"""Score saved partial tables and make the existing batch analysis use them.

Only completed jobs are updated. Original predictions, scores and selection are
retained. A chronological recovery policy applies equally to every method.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import fcntl
import hashlib
import json
from pathlib import Path
import re
import shutil
import time

from widesearch_experiment.common import ROOT, WORKSPACE, atomic_json, credential_env, now
from widesearch_experiment.conversion import table_markdown
from widesearch_experiment.runners.batch import subprocess_run
from .recover import atomic_text, batch_running, choose_table, read_json


POLICY = "last_completed_attempt_with_nonempty_table; final_else_latest_valid_checkpoint; no_merge_no_score_selection"


def recovery_candidate(root, selection_path):
    """Do not inspect gold or scores when selecting an available prediction."""
    backup = selection_path.with_name("selected.before_recovery.json")
    original = read_json(backup if backup.exists() else selection_path)
    if not original.get("result"):
        return None
    original_result = read_json(root / original["result"])
    if original_result.get("status") in {"success", "running"}:
        return None
    directories = sorted(selection_path.parent.glob("attempt_*"),
                         key=lambda p: int(p.name.removeprefix("attempt_")), reverse=True)
    for directory in directories:
        record = read_json(directory / "result.json")
        if not record or record.get("status") == "running":
            continue
        table, kind = choose_table(directory, record["status"])
        if table is None or not table["rows"]:
            continue
        original_prediction = root / original.get("prediction", "missing")
        formatted = table_markdown(table["header"], table["rows"]) + "\n"
        if original_prediction.exists() and original_prediction.read_text().strip() == formatted.strip():
            return None
        return {"original_selection": original, "original_result": original_result,
                "source_result": str((directory / "result.json").relative_to(root)), "record": record,
                "source": str(table["path"].relative_to(root)), "source_sha256": table["sha256"],
                "kind": kind, "rows": len(table["rows"]), "columns": len(table["header"]),
                "text": formatted, "prediction_sha256": hashlib.sha256(formatted.encode()).hexdigest()}
    return None


def publish_selection(root, selection_path, candidate, destination, score_path):
    """Atomic switch after a judge attempt; failures stay unscored, not zero."""
    backup = selection_path.with_name("selected.before_recovery.json")
    if not backup.exists():
        atomic_json(backup, candidate["original_selection"])
    recovery = {"policy": POLICY, "source": candidate["source"],
                "source_result": candidate["source_result"], "source_sha256": candidate["source_sha256"],
                "prediction_sha256": candidate["prediction_sha256"],
                "original_selection": str(backup.relative_to(root)), "recovered_at": now()}
    record = copy.deepcopy(candidate["record"])
    # Data quality and execution reliability are separate measurements.
    record.update(response=candidate["text"], status=candidate["original_result"]["status"],
                  recovery=recovery, source_execution_status=candidate["record"]["status"],
                  conversion={"status": "ok", "mode": "saved_table_recovery", "rows": candidate["rows"],
                              "columns": candidate["columns"]})
    atomic_json(destination / "result.json", record)
    events = (root / candidate["source_result"]).parent / "events.jsonl"
    if events.exists():
        shutil.copyfile(events, destination / "events.jsonl")
    selected = copy.deepcopy(candidate["original_selection"])
    selected.update(result=str((destination / "result.json").relative_to(root)),
                    prediction=str((destination / "prediction.md").relative_to(root)),
                    score=str(score_path.relative_to(root)), recovery=recovery)
    atomic_json(selection_path, selected)


async def score_job(root, selection_path, cfg):
    candidate = recovery_candidate(root, selection_path)
    if candidate is None:
        return False
    current = read_json(selection_path)
    if current.get("recovery", {}).get("prediction_sha256") == candidate["prediction_sha256"]:
        return False
    job = selection_path.parent.relative_to(root / "raw")
    destination = root / "recovery" / job
    destination.mkdir(parents=True, exist_ok=True)
    receipt = destination / "candidate.json"
    previous = read_json(receipt)
    if previous and previous.get("prediction_sha256") != candidate["prediction_sha256"]:
        raise RuntimeError("Recovery source changed after completion; refusing to mix grading records")
    atomic_json(receipt, {k: candidate[k] for k in ("source", "source_result", "source_sha256", "prediction_sha256", "rows", "columns", "kind")})
    atomic_text(destination / "prediction.md", candidate["text"])
    env = credential_env(cfg)
    env["PYTHONPATH"] = str(WORKSPACE)
    output = None
    for attempt in range(1, cfg["judge_attempts"] + 1):
        output = destination / f"score_{attempt}.json"
        if not output.exists():
            print(json.dumps({"recovering": str(job), "rows": candidate["rows"], "judge_attempt": attempt}, ensure_ascii=False), flush=True)
            code, timed_out = await subprocess_run([
                str(ROOT / ".venv/bin/python"), "-m", "widesearch_experiment.evaluation.official",
                "--instance", candidate["record"]["instance_id"], "--prediction", str(destination / "prediction.md"),
                "--output", str(output), "--config", str(root / "config.json"), "--manifest", str(root / "frozen.json")],
                output.with_suffix(".log"), env, 1200)
            if not output.exists():
                atomic_json(output, {"instance_id": candidate["record"]["instance_id"], "status": "judge_error",
                                     "metrics": None, "strict_success": None, "exit_code": code, "timeout": timed_out})
        if read_json(output).get("status") == "ok":
            break
    publish_selection(root, selection_path, candidate, destination, output)
    grade = read_json(output)
    print(json.dumps({"recovered_scoring": str(job), "status": grade.get("status"), "rows": candidate["rows"],
                      "metrics": grade.get("metrics")}, ensure_ascii=False), flush=True)
    return True


def update_analysis(root):
    from widesearch_experiment.analysis.summarize import summarize
    summarize(root.name)
    selected = [read_json(p) for p in (root / "raw").glob("*/*/trial_*/selected.json")]
    changed = [s for s in selected if s.get("recovery")]
    summary = {"updated_at": now(), "policy": POLICY, "jobs_in_statistics": len(selected),
               "recovered_jobs": len(changed), "recoveries": [
                   {"result": s["result"], "score": s["score"], "source": s["recovery"]["source"],
                    "score_status": read_json(root / s["score"]).get("status")} for s in changed]}
    atomic_json(root / "recovery/scoring_status.json", summary)
    report = ROOT / "reports" / root.name / "report.md"
    if report.exists():
        text = report.read_text()
        text += (f"\n## 已保存表格恢复评分\n\n本汇总已有 {len(changed)} 个任务使用恢复表的官方评分。"
                 "对失败任务按时间倒序取最后一次有可用表格的尝试，优先最终表，否则取该次最后完整快照；"
                 "不合并不同尝试，不按得分挑选。执行失败状态和全部重试消耗继续统计。"
                 "原选取记录保存为 selected.before_recovery.json，原预测与原评分文件保持原样。\n")
        atomic_text(report, text)


async def scan(root):
    cfg = read_json(root / "config.json")
    changed = False
    for selection_path in sorted((root / "raw").glob("*/*/trial_*/selected.json")):
        if await score_job(root, selection_path, cfg):
            update_analysis(root)
            changed = True
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.label):
        parser.error("Invalid batch label")
    root = ROOT / "results" / args.label
    if not (root / "frozen.json").exists():
        parser.error("Batch does not exist")
    lock = (root / ".recovery-scoring.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    atomic_json(root / "recovery_policy.json", {"policy": POLICY, "enabled_at": now(),
                "research_code_unchanged": True, "scoring_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    try:
        while True:
            asyncio.run(scan(root))
            if not args.watch or not batch_running(root):
                asyncio.run(scan(root))
                update_analysis(root)
                break
            time.sleep(15)
    finally:
        lock.close()


if __name__ == "__main__":
    main()
