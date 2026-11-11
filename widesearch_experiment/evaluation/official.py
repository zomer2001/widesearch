"""Unmodified official metric/parser source, with an OpenAI transport shim.

Transport failure detection is OUTSIDE official metrics: failed judge outputs are
retained but marked unscored, never silently entered into aggregates as zero.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
import types
from pathlib import Path

from ..common import ROOT, atomic_json, credential_env, now, read_json, safe_error, sha_file
from ..model_runtime import response_completion, responses_options, sdk_client_options


def judge_payload(content):
    """Add only the code fence required by the official parser; never repair values."""
    matches = re.findall(r"```json\s*(\{.*?\})\s*```", content or "", re.S)
    raw = matches[-1] if matches else (content or "").strip()
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None, content
    if not isinstance(parsed, dict):
        return None, content
    return parsed, content if matches else "```json\n" + raw + "\n```"


def score(instance_id, prediction_path, destination, cfg, manifest_path=None):
    os.environ.update({k: v for k, v in credential_env(cfg).items() if k.startswith("OPENAI_")})
    manifest = read_json(manifest_path or ROOT / "manifest.json")
    manifest = manifest.get("manifest", manifest)
    if "scoring_tasks_sha256" in manifest:
        assert sha_file(ROOT / "datasets/scoring_tasks.jsonl") == manifest["scoring_tasks_sha256"]
    gold_file = f"widesearch_gold/{instance_id}.csv"
    assert sha_file(ROOT / "datasets/private" / gold_file) == manifest["dataset_files"][gold_file]
    source = ROOT / "vendor/widesearch"
    for filename, expected in manifest["official_scorer_hashes"].items():
        if sha_file(source / "src/evaluation" / filename) != expected:
            raise RuntimeError("Official scorer source changed since freeze")
    sys.path.insert(0, str(source))
    errors, calls, empty_batches = [], [], []
    from openai import OpenAI
    client = OpenAI(timeout=cfg["model_timeout_seconds"], max_retries=1, **sdk_client_options(cfg))

    def completion(messages, tools=None, model_config_name=None):
        prompt = messages if isinstance(messages, str) else json.dumps(messages)
        if "====== response-start ======" in prompt:
            block = prompt.split("====== response-start ======", 1)[1].split("====== response-end", 1)[0].strip()
            if block == "{}":
                # Zero pairs require zero decisions. Avoid treating the model's
                # natural-language "there is nothing to grade" as judge failure.
                empty_batches.append(True)
                return types.SimpleNamespace(content="```json\n{}\n```")
        try:
            if cfg.get("judge_api") == "responses":
                judge_cfg = {**cfg, "reasoning_effort": cfg["judge_reasoning_effort"], "max_output_tokens": 16000}
                result = response_completion(client.responses.create(
                    **responses_options(judge_cfg, cfg["judge_model"]), input=messages))
            else:
                result = client.chat.completions.create(
                    model=cfg["judge_model"], temperature=0, seed=cfg["seed"], max_completion_tokens=16000,
                    messages=[{"role": "user", "content": messages}] if isinstance(messages, str) else messages)
            message = result.choices[0].message
            record = {"response": message.content, "usage": result.usage.model_dump() if result.usage else None,
                      "served_model": result.model, "finish_reason": result.choices[0].finish_reason}
            calls.append(record)
            parsed, parser_content = judge_payload(message.content)
            if parser_content != message.content:
                record["parser_content"] = parser_content
                record["format_normalization"] = "add_json_code_fence_only"
                message.content = parser_content
            if not isinstance(parsed, dict):
                errors.append({"type": "invalid_judge_json", "call": len(calls)})
            elif "====== response-start ======" in prompt:
                block = prompt.split("====== response-start ======", 1)[1].split("====== response-end", 1)[0]
                expected = set(re.findall(r"['\"](idx_\d+)['\"]\s*:", block))
                if not expected.issubset(parsed) or any(type(parsed[k]) is not int or parsed[k] not in (0, 1) for k in expected):
                    errors.append({"type": "incomplete_judge_scores", "call": len(calls)})
            elif any(not isinstance(v, str) for v in parsed.values()):
                errors.append({"type": "invalid_alignment_map", "call": len(calls)})
            return message
        except Exception as exc:
            errors.append(safe_error(exc))
            return None

    shim = types.ModuleType("src.utils.llm")
    shim.llm_completion = completion
    sys.modules["src.utils.llm"] = shim
    from loguru import logger
    logger.remove()
    from src.evaluation.data_loader import WideSearchDataLoader, WideSearchResponse
    from src.evaluation.evaluation import evaluate_single_query
    import pandas as pd
    # Upstream applies row metrics to a zero-row inner join. Pandas infers a
    # DataFrame for this empty apply, then raises during single-column assignment.
    # Supply the empty Series that a row-wise metric would produce: no comparison
    # or score is changed, and the official TP/denominator formulas still run.
    original_apply = pd.DataFrame.apply
    def empty_row_apply(frame, func, axis=0, *args, **kwargs):
        if frame.shape[0] == 0 and axis in (1, "columns"):
            return pd.Series(index=frame.index, dtype=object)
        return original_apply(frame, func, axis, *args, **kwargs)
    pd.DataFrame.apply = empty_row_apply
    loader = WideSearchDataLoader(str(ROOT / "datasets/scoring_tasks.jsonl"), str(ROOT / "datasets/private/widesearch_gold"))
    query = loader.load_query_by_instance_id(instance_id)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    response = WideSearchResponse(instance_id=instance_id, response=Path(prediction_path).read_text())
    result = evaluate_single_query(query, response, result_save_path=str(destination.with_suffix(".details.csv")),
                                   eval_model_config_name="experiment_openai")
    official = dataclasses.asdict(result)
    status = "judge_error" if errors else "evaluator_error" if "evaluator error:" in result.msg else "ok"
    keys = ["precision_by_item", "recall_by_item", "f1_by_item", "precision_by_row", "recall_by_row", "f1_by_row"]
    out = {"instance_id": instance_id, "status": status, "scored_at": now(),
           "official_commit": manifest["repositories"]["widesearch"]["commit"],
           "judge_model": cfg["judge_model"], "official_result": official,
           "judge_api": cfg.get("judge_api", "chat_completions"),
           "judge_reasoning_effort": cfg.get("judge_reasoning_effort"),
           "compatibility": "zero-row DataFrame.apply(axis=1) returns empty Series; official scorer files unchanged",
           "metrics": {k: official[k] for k in keys} if status == "ok" else None,
           "strict_success": all(abs(official[k] - 1) < 1e-9 for k in keys) if status == "ok" else None,
           "gold_rows": len(query.answer), "gold_columns": len(query.evaluation["required"]),
           "language": query.language, "judge_errors": errors, "judge_calls": len(calls),
           "empty_judge_batches_skipped": len(empty_batches)}
    atomic_json(destination.with_suffix(".judge.json"), calls)
    atomic_json(destination, out)
    print(json.dumps({k: out[k] for k in ("instance_id", "status", "metrics")}, ensure_ascii=False))
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--instance", required=True)
    p.add_argument("--prediction", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--manifest")
    args = p.parse_args()
    score(args.instance, args.prediction, args.output, read_json(args.config), args.manifest)


if __name__ == "__main__":
    main()
