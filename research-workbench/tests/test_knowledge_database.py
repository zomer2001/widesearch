"""Offline relational store regressions with a real versioned local original."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from api.knowledge_database import KnowledgeDatabase
from api.routes import knowledge as routes
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts import build_anduril_reader as builder
from searchos.local.library import LocalLibraryService


def test_manual_field_overlay_preserves_archive_and_relations(archived):
    database, reader, *_ = archived
    database.sync(reader)
    before = database.snapshot()
    entity = before["entities"][0]
    database.update_field(entity["id"], "mechanism", "人工补充机制")
    changed = database.snapshot()
    record = next(item for item in changed["entities"] if item["id"] == entity["id"])
    assert record["attributes"]["mechanism"] == "人工补充机制"
    assert record["field_overrides"]["mechanism"]["value"] == "人工补充机制"
    assert record["origin"] == entity["origin"]
    assert record["evidence_ids"] == entity["evidence_ids"]
    assert changed["relations"] == before["relations"]
    database.update_field(entity["id"], "mechanism", None)
    restored = next(item for item in database.snapshot()["entities"] if item["id"] == entity["id"])
    assert restored["attributes"].get("mechanism") == entity["attributes"].get("mechanism")
    assert not restored["field_overrides"]


def test_field_overlay_rejects_metadata_and_deleted_entity(archived):
    database, reader, *_ = archived
    database.sync(reader)
    identity = database.snapshot()["entities"][0]["id"]
    with pytest.raises(ValueError):
        database.update_field(identity, "evidence_ids", "fake")
    database.delete_entity(identity)
    with pytest.raises(KeyError):
        database.update_field(identity, "mechanism", "cannot resurrect")


PRODUCT_QUOTE = "This documented product connects public network interfaces."
TECH_QUOTE = "The interface exchanges messages between connected devices."


@pytest.fixture
def archived(tmp_path):
    service = LocalLibraryService(tmp_path / "local")
    library = service.create_library("原始资料")
    original = tmp_path / "original.md"
    original.write_text(f"# Product\n\n{PRODUCT_QUOTE}\n\n{TECH_QUOTE}\n", encoding="utf-8")
    record = service.save_original_file(library.library_id, original, "original.md")
    asyncio.run(service.parse_saved_document(library.library_id, record["document_id"]))
    spec = builder.SourceSpec("Product", library.library_id, record["document_id"], ((1, 100),))
    bundle = builder.select_source(service, spec)

    def segment(quote):
        return next(item["segment_id"] for item in bundle.excerpts if quote in item["text"])

    converted = builder.convert_extraction(
        bundle,
        {
            "name": "产品A",
            "family": None,
            "category": "网络系统",
            "overview": "unused",
            "technical_details": [
                {
                    "label": "接口",
                    "text": "通过公开网络接口连接。",
                    "quote": PRODUCT_QUOTE,
                    "segment_id": segment(PRODUCT_QUOTE),
                }
            ],
            "technologies": [
                {
                    "name": "消息交换",
                    "mechanism": "设备之间交换消息。",
                    "quote": TECH_QUOTE,
                    "segment_id": segment(TECH_QUOTE),
                }
            ],
            "routes": [
                {
                    "name": "接口通信",
                    "problem": "消息交换",
                    "mechanism": "通过公开接口交换。",
                    "tradeoffs": [],
                    "quote": TECH_QUOTE,
                    "segment_id": segment(TECH_QUOTE),
                }
            ],
            "maturity": {"stage": "未知", "basis": "", "quote": "", "segment_id": ""},
            "limitations": [],
        },
        service,
    )
    reader = tmp_path / "reader"
    reader.mkdir()
    converted["technologies"][0]["route_ids"] = [converted["routes"][0]["id"]]
    payload = {
        "products": [converted["product"]],
        "technologies": converted["technologies"],
        "routes": converted["routes"],
        "sources": converted["sources"],
        "reproduction_units": [],
        "gaps": [],
        "status": "partial",
    }
    path = reader / "knowledge.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    database = KnowledgeDatabase(service.data_root, tmp_path / "knowledge.sqlite")
    return database, reader, service, library.library_id, record["document_id"], payload


def test_sync_is_relational_durable_idempotent_and_uses_explicit_document_links(archived):
    database, reader, service, *_ = archived
    first = database.sync(reader)
    second = KnowledgeDatabase(service.data_root, database.database).sync(reader)
    assert (
        first["counts"]
        == second["counts"]
        == {
            "entities": 3,
            "relations": 3,
            "products": 1,
            "technologies": 1,
            "routes": 1,
            "references": 0,
        }
    )
    assert {item["id"] for item in first["relations"]} == {
        item["id"] for item in second["relations"]
    }
    assert all(item["status"] == "source_backed" for item in second["entities"])
    assert all(item["evidence_ids"] for item in second["relations"])
    technology = next(item for item in second["entities"] if item["kind"] == "technology")
    technology_edge = next(
        edge for edge in second["relations"] if edge["predicate"] == "uses_technology"
    )
    assert technology_edge["evidence_ids"] == technology["evidence_ids"]
    quotes = {item["id"]: item["quote"] for item in second["evidence"]}
    assert [quotes[key] for key in technology_edge["evidence_ids"]] == [TECH_QUOTE]
    assert PRODUCT_QUOTE not in [quotes[key] for key in technology_edge["evidence_ids"]]
    with sqlite3.connect(database.database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert {
            "entities",
            "relations",
            "evidence",
            "entity_evidence",
            "relation_evidence",
        } <= tables
        assert connection.execute("SELECT count(*) FROM relations").fetchone()[0] == 3


def test_shared_document_without_explicit_association_does_not_invent_relation(archived):
    database, reader, _, _, _, payload = archived
    payload["products"][0]["technology_ids"] = []
    payload["technologies"][0]["product_ids"] = []
    payload["technologies"][0]["route_ids"] = []
    payload["routes"][0]["product_ids"] = []
    (reader / "knowledge.json").write_text(json.dumps(payload), encoding="utf-8")
    result = database.sync(reader)
    assert len(result["entities"]) == 3
    assert result["relations"] == []


def test_source_deletion_hides_archive_entities_and_edges_without_sync(archived):
    database, reader, service, library_id, document_id, _ = archived
    before = database.sync(reader)
    assert before["counts"]["relations"] == 3
    service.delete_document(library_id, document_id)
    after = database.snapshot()
    assert after["entities"] == after["relations"] == after["evidence"] == []
    assert any("不可用" in gap for gap in after["gaps"])


def test_soft_deleted_entities_and_relations_do_not_return_after_sync(archived):
    database, reader, *_ = archived
    initial = database.sync(reader)
    edge = initial["relations"][0]
    database.delete_relation(edge["id"])
    after_edge_delete = database.sync(reader)
    assert edge["id"] not in {item["id"] for item in after_edge_delete["relations"]}
    product = next(item for item in initial["entities"] if item["kind"] == "product")
    database.delete_entity(product["id"])
    after_node_delete = database.sync(reader)
    assert product["id"] not in {item["id"] for item in after_node_delete["entities"]}
    assert all(
        product["id"] not in (item["source_id"], item["target_id"])
        for item in after_node_delete["relations"]
    )
    with sqlite3.connect(database.database) as connection:
        assert connection.execute(
            "SELECT deleted_at FROM entities WHERE id=?", (product["id"],)
        ).fetchone()[0]


def test_imported_edit_becomes_manual_and_sync_never_revalidates_it(archived):
    database, reader, service, library_id, document_id, _ = archived
    initial = database.sync(reader)
    product = next(item for item in initial["entities"] if item["kind"] == "product")
    updated = database.update_entity(product["id"], {"summary": "人工补充内容"})
    assert updated["origin"] == updated["status"] == "manual"
    assert updated["evidence_ids"] == []
    synced = database.sync(reader)
    current = next(item for item in synced["entities"] if item["id"] == product["id"])
    assert current["summary"] == "人工补充内容"
    for edge in synced["relations"]:
        if product["id"] in (edge["source_id"], edge["target_id"]):
            assert edge["origin"] == edge["status"] == "manual"
            assert edge["evidence_ids"] == []
    service.delete_document(library_id, document_id)
    after = database.snapshot()
    assert len(after["entities"]) == 1 and after["entities"][0]["status"] == "manual"
    assert after["relations"] == []


def test_manual_persistence_typed_edges_and_parameterized_ids(tmp_path):
    database = KnowledgeDatabase(tmp_path / "local", tmp_path / "db.sqlite")
    product = database.create_entity({"kind": "product", "name": "名称 '); DROP TABLE entities;--"})
    technology = database.create_entity({"kind": "technology", "name": "技术"})
    edge = database.create_relation(
        {"source_id": product["id"], "target_id": technology["id"], "predicate": "uses_technology"}
    )
    assert edge["status"] == "manual" and edge["evidence_ids"] == []
    with pytest.raises(ValueError, match="须从产品"):
        database.create_relation(
            {
                "source_id": technology["id"],
                "target_id": product["id"],
                "predicate": "uses_technology",
            }
        )
    with pytest.raises(KeyError):
        database.delete_entity("'; DROP TABLE entities;--")
    assert database.snapshot()["counts"]["entities"] == 2
    database.delete_entity(technology["id"])
    assert database.snapshot()["relations"] == []


def test_concurrent_manual_inserts_have_no_lost_updates(tmp_path):
    database = KnowledgeDatabase(tmp_path / "local", tmp_path / "db.sqlite")
    database.snapshot()  # Initialize before parallel write transactions.
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(
            executor.map(
                lambda index: database.create_entity(
                    {
                        "kind": "product",
                        "name": f"条目{index}",
                    }
                ),
                range(8),
            )
        )
    assert database.snapshot()["counts"]["entities"] == 8


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "LOCAL_DATA_ROOT", tmp_path / "local")
    monkeypatch.setattr(routes, "DATABASE_PATH", tmp_path / "db.sqlite")
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        yield client


def test_api_crud_validation_and_export(client):
    assert client.get("/api/knowledge").json()["counts"]["entities"] == 0
    created = client.post("/api/knowledge/entities", json={"kind": "product", "name": "测试产品"})
    assert created.status_code == 201
    identity = created.json()["id"]
    assert (
        client.patch(f"/api/knowledge/entities/{identity}", json={"name": "新名字"}).status_code
        == 200
    )
    assert client.get("/api/knowledge?query=新名字&kind=product").json()["counts"]["entities"] == 1
    assert (
        client.post("/api/knowledge/entities", json={"kind": "product", "name": "   "}).status_code
        == 422
    )
    assert (
        client.patch(f"/api/knowledge/entities/{identity}", json={"summary": None}).status_code
        == 422
    )
    assert client.get("/api/knowledge?kind=forged").status_code == 422
    assert (
        client.post(
            "/api/knowledge/entities",
            json={"kind": "product", "name": "fake", "evidence_ids": ["forged"]},
        ).status_code
        == 422
    )
    exported = client.get("/api/knowledge/export?format=json")
    assert exported.status_code == 200 and len(exported.json()["entities"]) == 1
    assert client.get("/api/knowledge/export?format=csv").text.startswith("\ufeff实体ID")
    assert client.delete(f"/api/knowledge/entities/{identity}").status_code == 204
    assert client.get("/api/knowledge").json()["entities"] == []
    assert client.delete(f"/api/knowledge/entities/{identity}").status_code == 404
    assert client.patch("/api/knowledge/entities/missing", json={"name": "x"}).status_code == 404
    assert client.delete("/api/knowledge/relations/missing").status_code == 404
    assert (
        client.post(
            "/api/knowledge/relations",
            json={"source_id": "x", "target_id": "y", "predicate": "uses_technology"},
        ).status_code
        == 404
    )
