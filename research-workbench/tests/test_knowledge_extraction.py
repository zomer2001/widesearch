"""Original-to-database regressions: source provenance, identity and persistence."""

from __future__ import annotations

import asyncio
import json

import pytest
from api.knowledge_database import KnowledgeDatabase
from api.knowledge_extraction import MAX_CHUNK_CHARACTERS, extract_document, scope_namespace

from searchos.local.library import LocalLibraryService

PRODUCT = "Anduril Atlas is a modular data exchange platform with a public network interface."
TECH = "Atlas exchanges messages between connected devices using a typed network interface."


def archive(
    tmp_path, name="original.md", extra="", *, body=None, url="https://www.anduril.com/test"
):
    service = LocalLibraryService(tmp_path / "local")
    libraries = service.list_libraries()
    library_id = libraries[0].library_id if libraries else service.create_library("原文").library_id
    path = tmp_path / name
    path.write_text(body or f"# Anduril Atlas\n\n{PRODUCT}\n\n{TECH}\n\n{extra}", encoding="utf-8")
    saved = service.save_original_file(
        library_id,
        path,
        name,
        origin={"title": "Anduril Atlas", "final_url": url},
    )
    asyncio.run(service.parse_saved_document(library_id, saved["document_id"]))
    record = next(
        row
        for row in service.list_documents(library_id)
        if row["document_id"] == saved["document_id"]
    )
    return service, library_id, saved["document_id"], record["source_revision"]


def payload(request, summary="原文描述产品通过公开接口交换消息。", attribute_value="原文接口甲"):
    def location(quote):
        return next(
            segment["segment_id"] for segment in request["segments"] if quote in segment["text"]
        )

    return {
        "entities": [
            {
                "key": "p",
                "kind": "product",
                "name": "Atlas",
                "namespace": "anduril",
                "summary": summary,
                "category": "网络平台",
                "quote": PRODUCT,
                "segment_id": location(PRODUCT),
                "attributes": [
                    {
                        "name": "公开接口",
                        "value": attribute_value,
                        "quote": PRODUCT,
                        "segment_id": location(PRODUCT),
                    }
                ],
            },
            {
                "key": "t",
                "kind": "technology",
                "name": "类型化消息交换",
                "namespace": "anduril",
                "summary": "原文描述使用类型化网络接口交换设备消息。",
                "category": "通信机制",
                "quote": TECH,
                "segment_id": location(TECH),
                "attributes": [],
            },
        ],
        "relations": [
            {
                "source_key": "p",
                "target_key": "t",
                "predicate": "uses_technology",
                "quote": TECH,
                "segment_id": location(TECH),
            }
        ],
        "gaps": [],
    }


def invoke(archived, database, extractor=payload, **kwargs):
    service, library_id, document_id, revision = archived
    return extract_document(
        service.data_root,
        library_id,
        document_id,
        revision,
        extractor=extractor,
        database=database,
        scope="调查 Anduril 技术",
        **kwargs,
    )


def test_real_original_extracts_multiple_entities_typed_edge_and_replay_costs_zero(tmp_path):
    archived = archive(tmp_path)
    database = tmp_path / "knowledge.sqlite"
    calls = []

    def extract(request):
        calls.append(request)
        return payload(request)

    result = invoke(archived, database, extract)
    assert result["status"] == "completed" and result["model_calls"] == 1
    assert result["counts"] == {"entities": 2, "relations": 1, "evidence": 2}
    replay = invoke(archived, database, lambda _: pytest.fail("cached replay cannot call model"))
    assert replay["cached"] and replay["model_calls"] == 0
    assert len(calls) == 1
    snapshot = KnowledgeDatabase(archived[0].data_root, database).snapshot()
    assert len(snapshot["entities"]) == 2 and len(snapshot["relations"]) == 1
    assert all(
        item["ref_id"].startswith("local://") and item["available"] for item in snapshot["evidence"]
    )
    assert snapshot["relations"][0]["status"] == "source_backed"
    product = next(item for item in snapshot["entities"] if item["kind"] == "product")
    evidence = {item["id"]: item for item in snapshot["evidence"]}
    assert evidence[product["field_evidence"]["公开接口"][0]]["quote"] == PRODUCT
    assert product["attributes"]["document_observations"][0]["field_evidence"]["公开接口"]


def test_invalid_quote_and_invalid_relation_never_enter_graph(tmp_path):
    archived = archive(tmp_path)
    database = tmp_path / "knowledge.sqlite"

    def invalid(request):
        data = payload(request)
        data["entities"][1]["quote"] = "This invented mechanism was never in the original document."
        return data

    result = invoke(archived, database, invalid)
    snapshot = KnowledgeDatabase(archived[0].data_root, database).snapshot()
    assert result["status"] == "partial" and result["rejected_count"] == 2
    assert len(snapshot["entities"]) == 1
    assert snapshot["relations"] == []


def test_version_mismatch_and_fetch_failure_rejected_before_model(tmp_path):
    archived = archive(tmp_path)
    service, library, document, _ = archived
    with pytest.raises(ValueError, match="VERSION_CHANGED"):
        extract_document(
            service.data_root,
            library,
            document,
            "a" * 64,
            extractor=lambda _: pytest.fail("invalid source cannot call model"),
            database=tmp_path / "db.sqlite",
        )
    failed = archive(
        tmp_path,
        "failed.md",
        "[NOTE] Live fetch failed; this is a search-engine snippet, not full page.",
    )
    with pytest.raises(ValueError, match="NOT_ORIGINAL"):
        invoke(failed, tmp_path / "db.sqlite", lambda _: pytest.fail("snippet cannot call model"))


def test_source_removed_during_model_call_cannot_publish_late_callback(tmp_path):
    archived = archive(tmp_path)
    service, library, document, _ = archived

    def deleted(request):
        data = payload(request)
        service.delete_document(library, document)
        return data

    database = tmp_path / "knowledge.sqlite"
    with pytest.raises((KeyError, ValueError, FileNotFoundError)):
        invoke(archived, database, deleted)
    assert KnowledgeDatabase(service.data_root, database).snapshot()["entities"] == []


def test_multi_document_identity_and_field_provenance_survive_one_source_deletion(tmp_path):
    first = archive(tmp_path, "first.md", "First observation.")
    second = archive(tmp_path, "second.md", "Second observation.")
    database = tmp_path / "knowledge.sqlite"
    invoke(first, database, lambda request: payload(request, "原文甲描述公开接口。", "接口甲"))
    invoke(second, database, lambda request: payload(request, "原文乙描述设备消息。", "接口乙"))
    store = KnowledgeDatabase(first[0].data_root, database)
    before = store.snapshot()
    assert len(before["entities"]) == 2 and len(before["relations"]) == 1
    product = next(item for item in before["entities"] if item["kind"] == "product")
    assert len(product["attributes"]["document_observations"]) == 2
    second[0].delete_document(second[1], second[2])
    after = store.snapshot()
    assert len(after["entities"]) == 2 and len(after["relations"]) == 1
    product_after = next(item for item in after["entities"] if item["kind"] == "product")
    assert product_after["id"] == product["id"]
    assert "接口甲" in json.dumps(product_after, ensure_ascii=False)
    assert "接口乙" not in json.dumps(product_after, ensure_ascii=False)
    assert product_after["summary"] == "原文甲描述公开接口。"


def test_manual_edits_deletions_and_seed_sync_never_overwrite_auto_records(tmp_path):
    archived = archive(tmp_path)
    database = tmp_path / "knowledge.sqlite"
    invoke(archived, database)
    store = KnowledgeDatabase(archived[0].data_root, database)
    current = store.snapshot()
    product = next(item for item in current["entities"] if item["kind"] == "product")
    technology = next(item for item in current["entities"] if item["kind"] == "technology")
    empty_reader = tmp_path / "reader"
    empty_reader.mkdir()
    (empty_reader / "knowledge.json").write_text("{}")
    assert store.sync(empty_reader)["counts"]["entities"] == 2
    store.update_entity(product["id"], {"summary": "用户自己的补充说明"})
    store.delete_entity(technology["id"])
    invoke(archived, database, force=True)
    after = store.snapshot()
    assert len(after["entities"]) == 1
    assert after["entities"][0]["summary"] == "用户自己的补充说明"
    assert after["entities"][0]["status"] == "manual"
    assert after["relations"] == []


def test_three_window_limit_is_partial_and_never_claims_complete_document(tmp_path):
    archived = archive(
        tmp_path, extra=("This is additional archived technical information and context. " * 1000)
    )
    calls = []

    def empty(request):
        calls.append(request)
        assert sum(len(segment["text"]) for segment in request["segments"]) <= MAX_CHUNK_CHARACTERS
        return {"entities": [], "relations": [], "gaps": []}

    result = invoke(archived, tmp_path / "knowledge.sqlite", empty)
    assert len(calls) == 3 and result["model_calls"] == 3
    assert result["truncated"] and result["status"] == "partial"
    assert any("尚未抽取" in gap for gap in result["gaps"])


def test_namespace_is_explicit_or_scope_isolated_and_anduril_batches_share_identity():
    assert scope_namespace("Anduril 产品") == scope_namespace("调查安杜里尔技术") == "anduril"
    assert scope_namespace("namespace:Acme 收集产品") == "acme"
    assert scope_namespace("调查甲公司") != scope_namespace("调查乙公司")


def test_non_chinese_categories_normalize_but_same_page_proper_names_do_not_prove_edge(tmp_path):
    software_quote = "Lattice is a software platform that integrates third-party capabilities."
    archived = archive(tmp_path, extra=software_quote)
    database = tmp_path / "knowledge.sqlite"

    def bad_link(request):
        data = payload(request, summary="Atlas通过公开接口交换设备消息。")
        data["entities"][0]["category"] = "Software Platform"
        item = data["entities"][1]
        item.update(
            name="Lattice",
            category="Software Platform",
            quote=software_quote,
            summary="Lattice是集成第三方能力的软件平台。",
            segment_id=next(
                segment["segment_id"]
                for segment in request["segments"]
                if software_quote in segment["text"]
            ),
        )
        # This is a real quote about Atlas, but it never names Lattice.
        return data

    result = invoke(archived, database, bad_link)
    snapshot = KnowledgeDatabase(archived[0].data_root, database).snapshot()
    assert result["counts"]["entities"] == 2 and result["rejected_count"] == 1
    assert snapshot["relations"] == []
    assert {item["category"] for item in snapshot["entities"]} == {"产品 / 平台", "技术机制"}
    assert all(item["summary"].startswith("厂商公开描述：") for item in snapshot["entities"])
    assert all(item["evidence_status"] == "manufacturer_claim" for item in snapshot["evidence"])
    with KnowledgeDatabase(archived[0].data_root, database)._connection() as connection:
        rows = [
            json.loads(row[0])
            for row in connection.execute("SELECT payload FROM entity_observations")
        ]
    assert all(row["original_category"] == "Software Platform" for row in rows)


def test_main_h1_excludes_mega_menu_teasers_and_cannot_supply_entity_quotes(tmp_path):
    nav_quote = "Lattice is a software platform that integrates third-party capabilities."
    body = f"### Lattice\n{nav_quote}\n\n# Anduril Atlas\n\n{PRODUCT}\n\n{TECH}"
    archived = archive(tmp_path, body=body)
    all_blocks = archived[0]._verified_blocks(archived[1], [archived[2]])
    nav_id = next(block.block_id for _, block in all_blocks if nav_quote in block.text)

    def includes_old_navigation(request):
        assert nav_quote not in "\n".join(segment["text"] for segment in request["segments"])
        data = payload(request)
        data["entities"].append(
            {
                "key": "nav",
                "kind": "technology",
                "name": "Lattice",
                "namespace": "anduril",
                "summary": "Lattice是公开的软件平台。",
                "category": "软件平台",
                "quote": nav_quote,
                "segment_id": nav_id,
                "attributes": [],
            }
        )
        return data

    result = invoke(archived, tmp_path / "db.sqlite", includes_old_navigation)
    assert result["counts"]["entities"] == 2 and result["rejected_count"] == 1


def test_absolute_manufacturer_effect_is_presented_as_its_claim(tmp_path):
    claim = "Atlas emits no signal and cannot be jammed."
    archived = archive(tmp_path, extra=claim)

    def manufacturer_claim(request):
        data = payload(request)
        segment_id = next(
            segment["segment_id"] for segment in request["segments"] if claim in segment["text"]
        )
        data["entities"][0]["attributes"].append(
            {
                "name": "被动特性",
                "value": "Atlas不发出信号且无法被干扰。",
                "quote": claim,
                "segment_id": segment_id,
            }
        )
        return data

    database = tmp_path / "db.sqlite"
    invoke(archived, database, manufacturer_claim)
    product = next(
        item
        for item in KnowledgeDatabase(archived[0].data_root, database).snapshot()["entities"]
        if item["kind"] == "product"
    )
    assert product["attributes"]["被动特性"].startswith("厂商宣称：")


def test_system_reference_status_never_inherits_model_quote(tmp_path):
    archived = archive(tmp_path, url="https://github.com/example/atlas")
    database = tmp_path / "knowledge.sqlite"

    def reference(request):
        data = payload(request)
        item = data["entities"][0]
        item["kind"] = "reference"
        item["attributes"] = [
            {
                "name": "execution_status",
                "value": "passed",
                "quote": PRODUCT,
                "segment_id": item["segment_id"],
            }
        ]
        return {"entities": [item], "relations": [], "gaps": []}

    invoke(archived, database, reference)
    entity = KnowledgeDatabase(archived[0].data_root, database).snapshot()["entities"][0]
    assert entity["attributes"]["execution_status"] == "not_run"
    assert "execution_status" not in entity["field_evidence"]
