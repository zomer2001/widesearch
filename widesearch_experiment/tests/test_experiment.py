import asyncio
import json
from pathlib import Path

import pytest

from widesearch_experiment.common import ROOT, atomic_json, public_tasks, validate_task
from widesearch_experiment.conversion import convert, deterministic, table_markdown
from widesearch_experiment.services import Journal
from widesearch_experiment.analysis.summarize import bootstrap_paired


def test_public_data_has_no_gold_or_evaluation():
    tasks = public_tasks()
    assert len(tasks) == 200
    assert sum(t["language"] == "en" for t in tasks) == 100
    for t in tasks:
        validate_task(t)
    with pytest.raises(ValueError):
        validate_task({**tasks[0], "evaluation": {"unique_columns": ["forbidden"]}})


def test_balanced_splits_nested_and_answer_independent():
    splits = json.loads((ROOT / "datasets/public/splits.json").read_text())
    lookup = {t["instance_id"]: t["language"] for t in public_tasks()}
    assert len(splits["pilot10"]) == 10
    assert len(splits["smoke2"]) == 2
    assert set(splits["smoke2"]) <= set(splits["pilot10"])
    assert len(splits["batch30"]) == 30
    assert set(splits["pilot10"]) <= set(splits["batch30"]) <= set(splits["full200"])
    assert sum(lookup[i] == "en" for i in splits["pilot10"]) == 5


def test_table_formats_and_multitable_ambiguity():
    assert deterministic('| name | year |\n|---|---|\n| A | 2024 |')[1] == [["A", "2024"]]
    assert deterministic('[{"name":"A","year":2024}]') == (["name", "year"], [["A", 2024]])
    assert deterministic('name,year\nA,2024') == (["name", "year"], [["A", "2024"]])
    assert deterministic('| A | B |\n|---|---|\n|x|y|\n\n| C | D |\n|---|---|\n|p|q|') is None


def test_conversion_receives_only_query_and_response(tmp_path):
    class FakeServices:
        cfg = {"conversion": "deterministic_then_llm", "conversion_model": "test"}
        async def complete(self, prompt, **kwargs):
            assert "ORIGINAL QUERY" in prompt and "EXISTING RESPONSE" in prompt
            assert kwargs["stage"] == "conversion"
            return '|name|year|\n|---|---|\n|A|2024|'
    result = asyncio.run(convert("Find name and year", "A was founded in 2024", tmp_path, FakeServices()))
    assert result["mode"] == "llm_format_only"
    assert (tmp_path / "prediction.csv").read_text().splitlines()[1] == "A,2024"


def test_unknown_token_usage_is_not_zero(tmp_path):
    j = Journal(tmp_path)
    j.emit("model_start", stage="research")
    j.emit("model_end", stage="research", input_tokens=None, output_tokens=None)
    j.emit("search_start", query="test")
    j.emit("search_error", type="Timeout")
    assert j.counts()["input_tokens"] is None
    assert j.counts()["search_calls"] == 1
    assert j.counts()["model_calls"] == 1


def test_stage_costs_do_not_mix(tmp_path):
    j = Journal(tmp_path)
    j.emit("model_start", stage="research")
    j.emit("model_end", stage="research", input_tokens=12, output_tokens=4)
    j.emit("model_start", stage="conversion")
    j.emit("model_end", stage="conversion", input_tokens=100, output_tokens=20)
    assert j.counts()["input_tokens"] == 12
    assert j.counts()["model_calls"] == 1


def test_pair_bootstrap_uses_common_tasks_and_known_difference():
    result = bootstrap_paired({"a": .5, "b": .8, "unpaired": 1}, {"a": .3, "b": .6}, seed=1, samples=200)
    assert result["paired_tasks"] == 2
    assert result["difference"] == pytest.approx(.2)
    assert result["ci95"] == pytest.approx([.2, .2])


def test_adapters_do_not_import_scoring_or_private_dataset():
    for path in (ROOT / "adapters").glob("*.py"):
        source = path.read_text()
        assert "datasets/private" not in source
        assert "widesearch_gold" not in source
        assert "from ..evaluation" not in source


def test_atomic_replace_complete_json(tmp_path):
    target = tmp_path / "record.json"
    atomic_json(target, {"state": "one"})
    atomic_json(target, {"state": "two"})
    assert json.loads(target.read_text()) == {"state": "two"}
    assert not list(tmp_path.glob(".writing-*"))
