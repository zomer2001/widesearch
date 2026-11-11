import csv
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..common import ROOT, atomic_json, now, read_json

METRICS = ["precision_by_item", "recall_by_item", "f1_by_item", "precision_by_row", "recall_by_row", "f1_by_row"]


def bootstrap_paired(left, right, *, seed, samples):
    shared = sorted(set(left).intersection(right))
    if not shared:
        return {"paired_tasks": 0, "difference": None, "ci95": None}
    differences = np.array([left[k] - right[k] for k in shared])
    rng = np.random.default_rng(seed)
    distribution = np.array([np.mean(rng.choice(differences, size=len(shared), replace=True)) for _ in range(samples)])
    return {"paired_tasks": len(shared), "difference": float(differences.mean()),
            "ci95": [float(x) for x in np.quantile(distribution, [0.025, 0.975])]}


def load_records(root):
    records = []
    for path in sorted((root / "raw").glob("*/*/trial_*/selected.json")):
        selected = read_json(path)
        r = read_json(root / selected["result"])
        score = read_json(root / selected["score"]) if selected.get("score") else {}
        row = {k: r.get(k) for k in ("instance_id", "language", "system", "trial", "status", "elapsed_seconds",
                                     "model_calls", "search_calls", "pages_fetched", "input_tokens", "output_tokens")}
        row.update(score_status=score.get("status", "missing"), strict_success=score.get("strict_success"),
                   gold_rows=score.get("gold_rows"), gold_columns=score.get("gold_columns"),
                   conversion_mode=r.get("conversion", {}).get("mode"), attempt=r.get("attempt"))
        row.update(score.get("metrics") or {m: None for m in METRICS})
        totals = selected.get("attempt_totals", {})
        row.update({f"all_attempts_{k}": v for k, v in totals.items()})
        events_path = (root / selected["result"]).parent / "events.jsonl"
        events = [json.loads(x) for x in events_path.read_text().splitlines()] if events_path.exists() else []
        row["conversion_calls"] = sum(e["kind"] == "model_start" and e.get("stage") == "conversion" for e in events)
        row["search_service_input_tokens"] = sum((e.get("usage") or {}).get("input_tokens", 0) for e in events if e["kind"] == "search_end")
        row["search_service_output_tokens"] = sum((e.get("usage") or {}).get("output_tokens", 0) for e in events if e["kind"] == "search_end")
        row["retrieval_efficiency"] = (row["recall_by_item"] * row["gold_rows"] * row["gold_columns"] / row["search_calls"]
                                        if row.get("recall_by_item") is not None and row.get("search_calls") else None)
        row["retrieval_efficiency_all_attempts"] = (row["recall_by_item"] * row["gold_rows"] * row["gold_columns"] / totals["search_calls"]
                                        if row.get("recall_by_item") is not None and totals.get("search_calls") else None)
        records.append(row)
    return records


def summarize(label):
    root = ROOT / "results" / label
    frozen = read_json(root / "frozen.json")
    report = ROOT / "reports" / label
    report.mkdir(parents=True, exist_ok=True)
    records = load_records(root)
    if not records:
        atomic_json(report / "status.json", {"status": "no_completed_results", "updated_at": now()})
        return
    df = pd.DataFrame(records)
    df.to_csv(report / "task_scores.csv", index=False)
    expected_trials = frozen["trials"]
    summary = []
    for system in frozen["systems"]:
        for language in ("all", "zh", "en"):
            group = df[df.system == system]
            if language != "all":
                group = group[group.language == language]
            if group.empty:
                continue
            row = {"system": system, "language": language, "completed_runs": len(group),
                   "scored_runs": int(group.f1_by_item.notna().sum()),
                   "execution_failure_rate": float((group.status != "success").mean()),
                   "unscored_rate": float((group.score_status != "ok").mean()),
                   "strict_success_rate_on_scored": pd.to_numeric(group.strict_success).mean()}
            for field in (*METRICS, "elapsed_seconds", "search_calls", "pages_fetched", "model_calls", "retrieval_efficiency"):
                row[field] = pd.to_numeric(group[field]).mean()
            summary.append(row)
    pd.DataFrame(summary).to_csv(report / "E1_overall_and_language.csv", index=False)
    complete = df[df.score_status == "ok"].groupby(["system", "instance_id"]).filter(
        lambda g: g.trial.nunique() == expected_trials)
    repetitions = []
    for system, group in complete.groupby("system"):
        row = {"system": system, "complete_tasks": group.instance_id.nunique(), "trials": expected_trials,
               "max_definition": "per_task_per_metric_best_trial"}
        for metric in METRICS:
            tasks = group.groupby("instance_id")[metric]
            row[f"{metric}_Mean@{expected_trials}"] = tasks.mean().mean()
            row[f"{metric}_Max@{expected_trials}"] = tasks.max().mean()
            row[f"{metric}_SD_across_batch_trial_means"] = group.groupby("trial")[metric].mean().std(ddof=1) if expected_trials > 1 else None
        repetitions.append(row)
    pd.DataFrame(repetitions).to_csv(report / "repeated_trials.csv", index=False)
    intervals = {}
    for comparator in ("searchos", "ours_off"):
        for metric in METRICS:
            per_task = complete.groupby(["system", "instance_id"])[metric].mean()
            left = per_task.get("ours", pd.Series(dtype=float)).to_dict()
            right = per_task.get(comparator, pd.Series(dtype=float)).to_dict()
            intervals[f"ours_minus_{comparator}_{metric}"] = bootstrap_paired(
                left, right, seed=frozen["config"]["seed"], samples=frozen["config"]["bootstrap_samples"])
    atomic_json(report / "paired_bootstrap.json", intervals)
    # Complexity thresholds are calculated over the full official dataset, not
    # chosen after seeing method scores; analysis process only reads the gold.
    sizes = []
    for p in (ROOT / "datasets/private/widesearch_gold").glob("*.csv"):
        with p.open() as f:
            rows = list(csv.reader(f))
        sizes.append((len(rows) - 1, len(rows[0])))
    row_boundary, column_boundary = np.median(np.array(sizes), axis=0)
    df["table_size_group"] = np.where(df.gold_rows <= row_boundary, "small", "large")
    df["column_count_group"] = np.where(df.gold_columns <= column_boundary, "few", "many")
    strata = []
    for dimension in ("language", "table_size_group", "column_count_group"):
        for (system, group_name), group in df.groupby(["system", dimension]):
            strata.append({"system": system, "dimension": dimension, "group": group_name,
                           "runs": len(group), "scored": int(group.f1_by_item.notna().sum()),
                           "item_f1": group.f1_by_item.mean(), "row_f1": group.f1_by_row.mean(),
                           "failure_rate": (group.status != "success").mean()})
    pd.DataFrame(strata).to_csv(report / "E3_complexity.csv", index=False)
    atomic_json(report / "complexity_thresholds.json", {"gold_rows_median": row_boundary, "columns_median": column_boundary,
                                                        "source_complexity": "not inferred; no validated labels available"})
    df[["system", "instance_id", "trial", "search_calls", "pages_fetched", "elapsed_seconds", "retrieval_efficiency",
        "f1_by_item", "f1_by_row"]].to_csv(report / "E2_final_efficiency.csv", index=False)
    plot_results(df, report)
    summarize_snapshots(root, report)
    expected = len(frozen["systems"]) * len(frozen["instances"]) * expected_trials
    cfg = frozen["config"]
    model_description = (f"配置 A：采集模型 {cfg['model']}，思考档 {cfg.get('reasoning_effort', '不适用')}；"
                         f"搜索 {cfg['search_provider']} / {cfg.get('search_model') or 'Search API'}；裁判 {cfg['judge_model']}。")
    text = [f"# WideSearch 实际运行记录：{label}", "", f"已保存 {len(df)} / {expected} 次执行；有效评分 {int(df.f1_by_item.notna().sum())} 次。",
            "", "当前数字仅来自该批次已完成的真实运行。未完成任务不计作零分；裁判或评分程序异常保留为未评分。",
            "", model_description + "Scrapy 使用固定同域广度采集，Crawl4AI 使用官方自适应采集，两者共用官方 LLM 抽取器。",
            "", "模型调用数与 Token 主字段仅统计研究过程；搜索服务自带模型开销、格式转换和裁判另存。不同网页读取器的差异保留在适配说明中。",
            "", "原始 SearchOS 与当前系统之间存在采集模块之外的工程差异；ours_off 与 ours 的同代码对照才隔离自适应模块的整体贡献。当前没有独立查询改写／联合路由模块。",
            "", "统计：Mean 为任务平均，Max@N 为逐题、逐指标取最佳；配对 Bootstrap 以任务为重采样单位，只使用双方重复次数完整的题目。",
            "", "过程曲线只使用采集时保存的实际表格快照；尚未评分的快照不绘制召回曲线。不用最终表格反推历史覆盖率。",
            "", "## 文件", "", "- task_scores.csv：逐题逐次结果与失败状态", "- E1_overall_and_language.csv：主指标和语言分组",
            "- repeated_trials.csv：Mean@N、Max@N 与重复波动", "- paired_bootstrap.json：配对差值及 95% 区间",
            "- E2_final_efficiency.csv：搜索、读取、耗时与信息效率", "- E3_complexity.csv：按字段数和表格规模分组",
            "- figures/：从真实运行表生成的 SVG 与 PDF", ""]
    (report / "report.md").write_text("\n".join(text))
    atomic_json(report / "status.json", {"completed": len(df), "expected": expected,
                "scored": int(df.f1_by_item.notna().sum()), "updated_at": now()})
    print(str(report))


def plot_results(df, directory):
    import os
    cache = ROOT / ".plot-cache"
    cache.mkdir(exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache))
    os.environ.setdefault("XDG_CACHE_HOME", str(cache))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False,
                         "svg.fonttype": "none", "pdf.fonttype": 42})
    figures = directory / "figures"
    figures.mkdir(exist_ok=True)
    quality = df.groupby("system")[["f1_by_item", "f1_by_row"]].mean().dropna(how="all")
    if not quality.empty:
        fig, ax = plt.subplots(figsize=(9, 4.5), layout="constrained")
        quality.rename(columns={"f1_by_item": "Item F1", "f1_by_row": "Row F1"}).plot.bar(ax=ax, color=["#2962A3", "#35A991"], rot=20)
        ax.set(xlabel="", ylabel="Official F1", title="Observed completed runs")
        ax.legend(frameon=False)
        for suffix in ("svg", "pdf"):
            fig.savefig(figures / f"E1_quality.{suffix}")
        plt.close(fig)
    usable = df.dropna(subset=["search_calls", "f1_by_item"])
    if not usable.empty:
        fig, ax = plt.subplots(figsize=(7, 4.5), layout="constrained")
        for system, group in usable.groupby("system"):
            ax.scatter(group.search_calls, group.f1_by_item, label=system, alpha=.65, s=30)
        ax.set(xlabel="Submitted search queries", ylabel="Official Item F1")
        ax.legend(frameon=False, fontsize=8)
        for suffix in ("svg", "pdf"):
            fig.savefig(figures / f"E2_final_quality_vs_search.{suffix}")
        plt.close(fig)


def summarize_snapshots(root, report):
    import matplotlib.pyplot as plt
    rows = []
    for path in (root / "scores").glob("*/*/trial_*/snapshots/*.json"):
        if path.name.endswith(".judge.json"):
            continue
        score = read_json(path)
        if score.get("status") == "ok" and "search_calls" in score:
            rows.append({"system": score["system"], "instance_id": score["instance_id"], "trial": score["trial"],
                         "search_calls": score["search_calls"], "elapsed_seconds": score["observed_elapsed_seconds"],
                         "item_recall": score["metrics"]["recall_by_item"], "snapshot_seq": score["snapshot_seq"]})
    if not rows:
        return
    frame = pd.DataFrame(rows)
    frame.to_csv(report / "E2_observed_snapshots.csv", index=False)
    fig, ax = plt.subplots(figsize=(8, 4.5), layout="constrained")
    colors = {"searchos": "#2962A3", "ours": "#35A991", "ours_off": "#D99B45"}
    labeled = set()
    for (system, instance, trial), group in frame.groupby(["system", "instance_id", "trial"]):
        group = group.sort_values("snapshot_seq").drop_duplicates("search_calls", keep="last")
        ax.plot(group.search_calls, group.item_recall, marker="o", markersize=3, alpha=.4,
                color=colors.get(system, "#777777"), label=system if system not in labeled else None)
        labeled.add(system)
    ax.set(xlabel="Search calls when table was observed", ylabel="Official Item Recall",
           title="Actual intermediate tables (each line is a task trial)")
    ax.legend(frameon=False)
    for suffix in ("svg", "pdf"):
        fig.savefig(report / "figures" / f"E2_observed_coverage.{suffix}")
    plt.close(fig)
