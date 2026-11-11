import json

from widesearch_experiment.analysis.summarize import load_records
from widesearch_experiment.common import atomic_json
from widesearch_recovery.recover import atomic_text, recover_batch
from widesearch_recovery.scoring import publish_selection, recovery_candidate


def fixture(root, *, final="", status="failed"):
    job = root / "raw/ours/ws_test/trial_1"
    for attempt in (1, 2):
        d = job / f"attempt_{attempt}"
        atomic_json(d / "request.json", {"task": {"query": "Collect values"}})
        atomic_json(d / "result.json", {"instance_id": "ws_test", "system": "ours", "language": "en",
            "trial": 1, "attempt": attempt, "status": status, "response": "", "elapsed_seconds": 30,
            "model_calls": 2, "search_calls": 3, "pages_fetched": 2, "input_tokens": 10, "output_tokens": 5})
        atomic_text(d / "prediction/prediction.md", final if attempt == 2 else "")
        atomic_text(d / "events.jsonl", '{"kind":"search_start"}\n')
    metrics = {f"{name}_by_{unit}": 0.0 for unit in ("item", "row") for name in ("precision", "recall", "f1")}
    atomic_json(root / "scores/original.json", {"status": "ok", "metrics": metrics, "gold_rows": 2, "gold_columns": 2})
    selection = {"result": "raw/ours/ws_test/trial_1/attempt_2/result.json",
                 "prediction": "raw/ours/ws_test/trial_1/attempt_2/prediction/prediction.md",
                 "score": "scores/original.json", "attempt_totals": {"attempts": 2, "search_calls": 6}}
    atomic_json(job / "selected.json", selection)
    return job, selection


def test_falls_back_to_last_attempt_with_data_without_merging(tmp_path):
    job, _ = fixture(tmp_path, final="|name|value|\n|---|---|")
    atomic_text(job / "attempt_1/snapshots/000001.md", "|name|value|\n|---|---|\n|A|1|")
    candidate = recovery_candidate(tmp_path, job / "selected.json")
    assert candidate["record"]["attempt"] == 1 and candidate["rows"] == 1


def test_newer_nonempty_final_keeps_original_score_even_if_older_has_more_rows(tmp_path):
    job, _ = fixture(tmp_path, final="| name | value |\n| --- | --- |\n| A |  |\n")
    atomic_text(job / "attempt_1/snapshots/000001.md", "|name|value|\n|---|---|\n|A|1|\n|B|2|")
    assert recovery_candidate(tmp_path, job / "selected.json") is None


def test_no_research_output_does_not_create_an_answer(tmp_path):
    job, _ = fixture(tmp_path)
    assert recovery_candidate(tmp_path, job / "selected.json") is None


def test_successful_empty_table_is_not_replaced_with_superseded_facts(tmp_path):
    job, _ = fixture(tmp_path, final="|name|value|\n|---|---|", status="success")
    atomic_text(job / "attempt_1/snapshots/000001.md", "|name|value|\n|---|---|\n|A|1|")
    assert recovery_candidate(tmp_path, job / "selected.json") is None


def test_recovered_score_enters_existing_statistics_and_keeps_failures(tmp_path):
    job, selection = fixture(tmp_path, status="timeout")
    atomic_text(job / "attempt_2/snapshots/000001.md", "|name|value|\n|---|---|\n|A|1|")
    candidate = recovery_candidate(tmp_path, job / "selected.json")
    destination = tmp_path / "recovery/ours/ws_test/trial_1"
    atomic_text(destination / "prediction.md", candidate["text"])
    metrics = {f"{name}_by_{unit}": 0.5 for unit in ("item", "row") for name in ("precision", "recall", "f1")}
    score = destination / "score_1.json"
    atomic_json(score, {"status": "ok", "metrics": metrics, "strict_success": False, "gold_rows": 2, "gold_columns": 2})
    publish_selection(tmp_path, job / "selected.json", candidate, destination, score)
    rows = load_records(tmp_path)
    assert len(rows) == 1 and rows[0]["f1_by_item"] == 0.5
    assert rows[0]["status"] == "timeout" and rows[0]["all_attempts_search_calls"] == 6
    assert rows[0]["conversion_mode"] == "saved_table_recovery"
    assert json.loads((job / "selected.before_recovery.json").read_text()) == selection
    assert json.loads((tmp_path / "scores/original.json").read_text())["metrics"]["f1_by_item"] == 0
    assert (tmp_path / selection["prediction"]).read_text() == ""
    # A second scan still resolves immutable original sources, not itself.
    assert recovery_candidate(tmp_path, job / "selected.json")["source"] == candidate["source"]
    view = recover_batch(tmp_path, tmp_path / "viewer")
    chosen = [e for e in view["entries"] if e["is_selected_attempt"]]
    assert len(chosen) == 1 and chosen[0]["recovery_used_for_scoring"]
    assert chosen[0]["metrics"]["f1_by_item"] == 0.5
    assert view["recovered_predictions_regraded"] is True


def test_judge_failure_stays_unscored_instead_of_retaining_empty_zero(tmp_path):
    job, _ = fixture(tmp_path)
    atomic_text(job / "attempt_2/snapshots/000001.md", "|name|value|\n|---|---|\n|A|1|")
    candidate = recovery_candidate(tmp_path, job / "selected.json")
    destination = tmp_path / "recovery/ours/ws_test/trial_1"
    atomic_text(destination / "prediction.md", candidate["text"])
    score = destination / "score_1.json"
    atomic_json(score, {"status": "judge_error", "metrics": None})
    publish_selection(tmp_path, job / "selected.json", candidate, destination, score)
    assert load_records(tmp_path)[0]["f1_by_item"] is None
