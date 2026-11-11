"""Offline original-to-reader publication gates; no model or remote requests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from api.anduril_reader import load_anduril_knowledge, quote_matches

from scripts import build_anduril_reader as builder
from searchos.local.library import LocalLibraryService

ORIGINAL = (
    "The documented system exchanges messages between connected devices "
    "using a published network interface."
)


@pytest.fixture
def original(tmp_path):
    service = LocalLibraryService(tmp_path / "local")
    library = service.create_library("真实归档原文")
    temporary = tmp_path / "source.md"
    temporary.write_text(f"# Documented product\n\n{ORIGINAL}\n", encoding="utf-8")
    saved = service.save_original_file(
        library.library_id,
        temporary,
        "source.md",
        origin={"request_url": "https://example.com/original"},
    )
    asyncio.run(service.parse_saved_document(library.library_id, saved["document_id"]))
    spec = builder.SourceSpec(
        "Documented product", library.library_id, saved["document_id"], ((1, 100),)
    )
    bundle = builder.select_source(service, spec)
    return service, bundle


def extraction(bundle, quote=ORIGINAL):
    segment = next(item["segment_id"] for item in bundle.excerpts if ORIGINAL in item["text"])
    return {
        "name": "Documented product",
        "family": None,
        "category": "公开描述的系统",
        "overview": "This free-form overview must never be published.",
        "technical_details": [
            {
                "label": "消息交换",
                "text": "原文描述通过公开网络接口交换消息。",
                "quote": quote,
                "segment_id": segment,
            }
        ],
        "technologies": [
            {
                "name": "公开网络接口",
                "mechanism": "原文描述设备间消息交换。",
                "quote": quote,
                "segment_id": segment,
            }
        ],
        "routes": [
            {
                "name": "接口消息交换",
                "problem": "设备间消息交换",
                "mechanism": "使用公开网络接口",
                "tradeoffs": [],
                "quote": quote,
                "segment_id": segment,
            }
        ],
        "maturity": {
            "stage": "公开介绍",
            "basis": "原文公开介绍系统功能。",
            "quote": quote,
            "segment_id": segment,
        },
        "limitations": ["交付与使用进展待补充。"],
    }


def write_reader(directory: Path, converted):
    directory.mkdir()
    data = {
        "title": "Anduril 知识",
        "products": [converted["product"]] if converted["product"] else [],
        "technologies": converted["technologies"],
        "routes": converted["routes"],
        "sources": converted["sources"],
        "reproduction_units": [],
        "gaps": [],
        "status": "partial",
    }
    (directory / "knowledge.json").write_text(json.dumps(data), encoding="utf-8")


def test_quotes_require_exact_content_with_only_whitespace_normalization():
    assert quote_matches("an exact\nquote", "This contains an exact   quote.")
    assert not quote_matches("An exact quote", "an exact quote")
    assert not quote_matches("a changed quote", "an exact quote")
    assert not quote_matches("", "anything")
    assert not quote_matches("successful data", "[NOTE] Live fetch failed; successful data")


def test_nonmatching_quote_or_forged_segment_never_publishes(original):
    service, bundle = original
    result = builder.convert_extraction(bundle, extraction(bundle, "invented evidence"), service)
    assert result["product"] is None
    assert result["technologies"] == result["routes"] == result["sources"] == []
    assert len(result["dropped"]) == 4
    forged = extraction(bundle)
    for item in [
        *forged["technical_details"],
        *forged["technologies"],
        *forged["routes"],
        forged["maturity"],
    ]:
        item["segment_id"] = "forged"
    assert builder.convert_extraction(bundle, forged, service)["product"] is None


@pytest.mark.parametrize(
    "marker", ["[NOTE] Live fetch failed", "Search-engine snippet", "[FETCH_ERROR]"]
)
def test_failed_fetch_and_snippet_originals_are_rejected_before_model(original, marker):
    service, bundle = original
    pairs = service._verified_blocks(bundle.spec.library_id, [bundle.spec.document_id])
    record, block = pairs[0]
    bad_block = SimpleNamespace(text=f"{marker}: {ORIGINAL}")
    fake = SimpleNamespace(_verified_blocks=lambda *args: [(record, bad_block)])
    with pytest.raises(ValueError, match="failed_fetch_or_search_snippet"):
        builder.select_source(fake, bundle.spec)


def test_published_references_resolve_and_deleted_sources_remove_dependent_claims(
    original, tmp_path
):
    service, bundle = original
    converted = builder.convert_extraction(bundle, extraction(bundle), service)
    assert converted["product"]["overview"] == "原文描述通过公开网络接口交换消息。"
    assert "free-form" not in converted["product"]["overview"]
    source = converted["sources"][0]
    resolved = service.resolve_citation(source["ref_id"])
    assert resolved["document_id"] == bundle.spec.document_id
    assert quote_matches(source["quote"], resolved["content"])
    output = tmp_path / "reader"
    write_reader(output, converted)
    current = load_anduril_knowledge(service.data_root, output)
    assert current["counts"]["products"] == 1
    assert current["sources"][0]["available"] is True
    service.delete_document(bundle.spec.library_id, bundle.spec.document_id)
    after = load_anduril_knowledge(service.data_root, output)
    assert after["products"] == after["technologies"] == after["routes"] == []
    assert after["sources"][0]["available"] is False
    assert after["counts"]["maturity"] == 0
    assert any("不可用" in gap for gap in after["gaps"])


def test_unmatched_maturity_is_unknown_instead_of_publishing_inferred_stage(original):
    service, bundle = original
    payload = extraction(bundle)
    payload["maturity"].update(
        stage="公开使用", basis="A guessed operational stage", quote="not in this original"
    )
    result = builder.convert_extraction(bundle, payload, service)
    assert result["product"]["maturity"]["stage"] == "未知"
    assert result["product"]["maturity"]["source_ids"] == []
    assert "guessed" not in result["product"]["maturity"]["basis"]


def test_no_file_returns_empty_without_loading_model_configuration(tmp_path, monkeypatch):
    from api import workbench

    monkeypatch.setattr(
        workbench, "load_config", lambda: pytest.fail("No configuration read permitted")
    )
    result = load_anduril_knowledge(tmp_path / "local", tmp_path / "reader")
    assert result["status"] == "empty"
    assert all(value == 0 for value in result["counts"].values())
    assert builder.main(["--plan", "--data-root", str(tmp_path / "local")]) == 0


def test_successful_conversion_cache_reuses_original_revision_and_prompt(original, tmp_path):
    service, bundle = original
    calls = []

    def extract(current):
        calls.append(current.spec.document_id)
        return extraction(current)

    output = tmp_path / "reader"
    first = builder.build_reader(service, [bundle], output, extractor=extract)
    second = builder.build_reader(
        service,
        [bundle],
        output,
        extractor=lambda *_: pytest.fail("Cache must avoid another model request"),
    )
    assert calls == [bundle.spec.document_id]
    assert first["audit"]["model_requests"] == 1
    assert second["audit"]["model_requests"] == 0
    assert second["audit"]["cache_hits"] == 1
    assert (output / "知识概览.md").exists()
    assert "原文描述通过公开网络接口交换消息" in (output / "知识概览.md").read_text()


def test_external_reference_execution_is_never_upgraded_by_artifact(tmp_path):
    directory = tmp_path / "reader"
    directory.mkdir()
    external = {
        "reproduction_units": [
            {
                "id": "u",
                "name": "reference",
                "source_ids": ["s"],
                "status": "passed",
                "execution_status": "success",
                "related_technology_ids": [],
            }
        ],
        "sources": [
            {"id": "s", "title": "reference", "url": "https://github.com/example/reference"}
        ],
    }
    (directory / "external-reference-units.json").write_text(json.dumps(external))
    result = load_anduril_knowledge(tmp_path / "local", directory)
    assert result["reproduction_units"][0]["status"] == "not_run"
    assert result["reproduction_units"][0]["execution_status"] == "not_run"


def test_schema_requires_every_field_and_forbids_unexpected_properties():
    schema = builder.Extraction.model_json_schema()
    for current in [schema, *schema["$defs"].values()]:
        assert current["additionalProperties"] is False
        assert set(current["required"]) == set(current["properties"])


def test_short_technical_numbers_are_retained_while_navigation_is_skipped():
    assert builder._substantive("# 50,000+")
    assert builder._substantive("###### Fielded hours")
    assert builder._substantive("# 10 minutes")
    assert not builder._substantive("# Products")
    assert not builder._substantive("Contact us")
    assert not builder._substantive("[Home](https://example.com)")


def test_request_budget_never_exceeds_seven_even_with_eight_bundles(original, tmp_path):
    service, bundle = original
    calls = []

    def fail(current):
        calls.append(current.spec.document_id)
        raise RuntimeError("offline stub")

    result = builder.build_reader(service, [bundle] * 8, tmp_path / "reader", extractor=fail)
    assert len(calls) == 7
    assert result["audit"]["model_requests"] == 7


def test_get_route_loads_empty_state_without_starting_extraction(tmp_path, monkeypatch):
    from api import anduril_reader
    from api.routes import local_search

    monkeypatch.setattr(anduril_reader, "DEFAULT_READER_ROOT", tmp_path / "reader")
    monkeypatch.setattr(local_search, "LOCAL_DATA_ROOT", tmp_path / "local")
    monkeypatch.setattr(builder, "openai_extractor", lambda: pytest.fail("GET cannot extract"))
    result = asyncio.run(local_search.get_anduril_knowledge())
    assert result["status"] == "empty"
    assert result["counts"]["products"] == 0


def test_english_prose_is_pending_editing_instead_of_successful_publication(original):
    service, bundle = original
    payload = extraction(bundle)
    payload["technical_details"][0].update(
        label="Description", text="An English description with a valid original quotation."
    )
    result = builder.convert_extraction(bundle, payload, service)
    assert result["product"] is None
    assert result["technologies"] == []
    assert any(item["reason"] == "chinese_editing_required" for item in result["dropped"])
    assert not builder.chinese_prose("A long English description with only one token 中文")
    assert builder.chinese_prose("原文描述以 Lattice Mesh 转换不同平台的数据。")


def test_default_run_preserves_reviewed_artifact_and_never_uses_old_english_cache(
    original, tmp_path
):
    service, bundle = original
    converted = builder.convert_extraction(bundle, extraction(bundle), service)
    output = tmp_path / "reader"
    write_reader(output, converted)
    (output / "editorial-review.json").write_text('{"review_version":1}')
    original_bytes = (output / "knowledge.json").read_bytes()
    result = builder.build_reader(
        service,
        [bundle],
        output,
        extractor=lambda *_: pytest.fail("Reviewed default run cannot call a model"),
    )
    assert (output / "knowledge.json").read_bytes() == original_bytes
    assert result["audit"]["current_run_model_requests"] == 0
    assert result["counts"]["products"] == 1


def test_refresh_is_explicit_and_retains_the_previous_review(original, tmp_path):
    service, bundle = original
    output = tmp_path / "reader"
    write_reader(output, builder.convert_extraction(bundle, extraction(bundle), service))
    (output / "editorial-review.json").write_text('{"review_version":1}')
    result = builder.build_reader(
        service,
        [bundle],
        output,
        extractor=extraction,
        refresh=True,
    )
    assert result["counts"]["products"] == 1
    assert len(list((output / "revisions").glob("knowledge-*.json"))) == 1
    assert len(list((output / "revisions").glob("editorial-review-*.json"))) == 1
    assert not (output / "editorial-review.json").exists()
