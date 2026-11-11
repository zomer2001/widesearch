"""Topic isolation and durable progress; no model or remote calls."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from api import topic_store as store_module
from api.knowledge_database import KnowledgeDatabase
from api.knowledge_extraction import extract_document
from api.routes import topics as routes
from api.topic_store import TopicStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

from searchos.local.library import LocalLibraryService


@pytest.fixture
def store(tmp_path):
    return TopicStore(tmp_path / "local", tmp_path / "topics.sqlite", tmp_path / "knowledge.sqlite")


def manual_entity(store, name="Product", kind="product", **attrs):
    return KnowledgeDatabase(store.data_root, store.knowledge_database).create_entity(
        {"name": name, "kind": kind, "attributes": attrs}
    )


def link(store, topic_id, entity_id):
    with store._connection() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO topic_entities VALUES(?,?,0)", (topic_id, entity_id)
        )


def test_stable_topic_identity_aliases_and_ambiguous_query(store):
    first = store.resolve_topic("anduril")
    assert store.resolve_topic("调查 Anduril 的技术路线")["id"] == first["id"]
    assert store.create_topic("安杜里尔")["id"] == first["id"]
    other = store.create_topic("Example Robotics")
    with pytest.raises(ValueError, match="多个"):
        store.resolve_topic("比较 Anduril 与 Example Robotics")
    assert store.resolve_topic("任意复杂任务", topic_id=other["id"])["id"] == other["id"]
    assert store.resolve_topic("andurilian")["id"] != first["id"]


def test_unknown_full_intent_is_not_silently_truncated(store):
    intent = "调研激光通信技术路线、行业开源项目与成熟度证据"
    assert store.resolve_topic(intent)["title"] == intent
    with pytest.raises(ValueError, match="先创建专题"):
        store.resolve_topic("调查" * 200)


def test_topic_snapshot_is_scoped_and_gaps_are_not_invented_values(store):
    a, b = store.create_topic("Anduril"), store.create_topic("Other")
    entity = manual_entity(store, "Wisp", manufacturer="Manufacturer")
    link(store, a["id"], entity["id"])
    snap_a, snap_b = store.snapshot(a["id"]), store.snapshot(b["id"])
    assert snap_a["knowledge"]["counts"]["entities"] == 1
    assert snap_b["knowledge"]["entities"] == []
    assert snap_b["knowledge"]["evidence"] == []
    assert snap_b["knowledge"]["title"] == "Other 知识库"
    assert snap_a["coverage"]["filled"] == 1
    assert snap_a["coverage"]["missing"] == len(snap_a["gaps"])
    assert {"family", "parameters", "maturity"} <= {gap["field"] for gap in snap_a["gaps"]}
    assert "manufacturer" not in {gap["field"] for gap in snap_a["gaps"]}
    assert "family" not in snap_a["knowledge"]["entities"][0]["attributes"]


def test_remove_membership_keeps_shared_entity_and_tombstone(store):
    a, b = store.create_topic("A"), store.create_topic("B")
    entity = manual_entity(store)
    link(store, a["id"], entity["id"])
    link(store, b["id"], entity["id"])
    store.remove_entity(a["id"], entity["id"])
    link(store, a["id"], entity["id"])
    assert store.snapshot(a["id"])["knowledge"]["entities"] == []
    assert len(store.snapshot(b["id"])["knowledge"]["entities"]) == 1
    assert (
        len(KnowledgeDatabase(store.data_root, store.knowledge_database).snapshot()["entities"])
        == 1
    )


def test_durable_run_task_and_incremental_changes(store):
    topic = store.create_topic("A")
    run = store.create_run(topic["id"], "collect", "openai", "gpt-4o", max_tasks=3)
    with pytest.raises(ValueError, match="正在执行"):
        store.create_run(topic["id"], "duplicate", "openai", "gpt-4o")
    task = store.add_task(run["id"], title="产品谱系", query="collect lineage")
    store.update_task(task["id"], status="running", session_id="session-a", topic_id="bad")
    store.update_run(run["id"], status="running", message="读取资料")
    reload = TopicStore(store.data_root, store.database, store.knowledge_database)
    assert reload.list_pending_runs()[0]["id"] == run["id"]
    assert reload.snapshot(topic["id"])["tasks"][0]["topic_id"] == topic["id"]
    entity = manual_entity(store)
    link(store, topic["id"], entity["id"])
    change = store.sync_topic_changes(topic["id"], run["id"])
    assert change["entities_added"] == 1
    assert change["changed"] is True
    second = store.sync_topic_changes(topic["id"], run["id"])
    assert second["changed"] is False
    assert len(store.snapshot(topic["id"])["changes"]) == 1
    store.update_run(run["id"], status="completed")
    assert reload.list_pending_runs() == []


def test_changes_cannot_be_attributed_to_other_topic_run(store):
    a, b = store.create_topic("A"), store.create_topic("B")
    run = store.create_run(a["id"], "q", "openai", "gpt-4o")
    with pytest.raises(KeyError, match="不属于"):
        store.sync_topic_changes(b["id"], run["id"])


def test_read_routes_and_safe_membership_delete(store, monkeypatch):
    monkeypatch.setattr(routes, "_store", lambda: store)
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    result = client.post("/api/topics", json={"title": "A"})
    assert result.status_code == 200
    topic_id = result.json()["id"]
    assert client.get("/api/topics").json()["default_topic_id"] == topic_id
    assert client.get(f"/api/topics/{topic_id}").json()["runs"] == []
    assert client.get("/api/topics/missing").status_code == 404
    assert client.post("/api/topics", json={"title": "  "}).status_code == 422
    entity = manual_entity(store)
    link(store, topic_id, entity["id"])
    assert client.delete(f"/api/topics/{topic_id}/entities/{entity['id']}").status_code == 204
    assert client.get(f"/api/topics/{topic_id}").json()["knowledge"]["entities"] == []


def test_active_phase_duplicate_and_concurrent_start(store):
    topic = store.create_topic("A")

    def start():
        try:
            return store.create_run(topic["id"], "q", "openai", "gpt-4o")["id"]
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: start(), range(2)))
    assert len([value for value in outcomes if value]) == 1
    run_id = next(value for value in outcomes if value)
    for status in ("planning", "researching", "ingesting", "stopping"):
        store.update_run(run_id, status=status)
        assert store.list_pending_runs()[0]["id"] == run_id
        assert start() is None


def test_late_archive_and_extraction_after_stop_are_projected_without_resurrection(
    store, tmp_path, monkeypatch
):
    topic = store.create_topic("Anduril")
    other = store.create_topic("Other")
    store.register_session(topic["id"], "session-a")
    documents = []
    service = LocalLibraryService(store.data_root)
    library_id = service.create_library("原始资料").library_id
    monkeypatch.setattr(
        store_module,
        "research_materials",
        lambda *args: {
            "library_id": library_id,
            "documents": documents,
        },
    )
    assert store.snapshot(topic["id"])["sources"] == []
    quote = "Anduril Atlas provides a documented public network interface for exchanging messages."
    path = tmp_path / "original.md"
    path.write_text("# Anduril Atlas\n\n" + quote + "\n", encoding="utf-8")
    saved = service.save_original_file(
        library_id,
        path,
        path.name,
        origin={
            "title": "Anduril Atlas",
            "final_url": "https://www.anduril.com/test",
            "saved_form": "research_page_markdown_copy",
        },
    )
    asyncio.run(service.parse_saved_document(library_id, saved["document_id"]))
    document = service.list_documents(library_id)[0]
    document["session_ids"] = ["session-a"]
    documents.append(document)
    assert len(store.snapshot(topic["id"])["sources"]) == 1
    assert store.snapshot(other["id"])["sources"] == []
    reference = store.snapshot(topic["id"])["sources"][0]["ref_id"]
    assert service.resolve_citation(reference)["document_id"] == document["document_id"]

    def extracted(request):
        segment = next(item["segment_id"] for item in request["segments"] if quote in item["text"])
        return {
            "entities": [
                {
                    "key": "p",
                    "kind": "product",
                    "name": "Atlas",
                    "namespace": "anduril",
                    "summary": "公开网络接口",
                    "category": "数据平台",
                    "quote": quote,
                    "segment_id": segment,
                    "attributes": [],
                }
            ],
            "relations": [],
            "gaps": [],
        }

    extract_document(
        store.data_root,
        library_id,
        document["document_id"],
        document["source_revision"],
        extractor=extracted,
        database=store.knowledge_database,
        scope="namespace: anduril",
    )
    snapshot = store.snapshot(topic["id"])
    assert len(snapshot["knowledge"]["entities"]) == 1
    assert store.snapshot(other["id"])["knowledge"]["entities"] == []
    store.remove_entity(topic["id"], snapshot["knowledge"]["entities"][0]["id"])
    assert store.snapshot(topic["id"])["knowledge"]["entities"] == []
    assert len(store.snapshot(topic["id"])["sources"]) == 1
    service.delete_document(library_id, document["document_id"])
    assert store.snapshot(topic["id"])["sources"] == []


def test_no_legacy_migration_of_unrelated_manual_knowledge(store):
    manual_entity(store, "Unrelated")
    store.ensure_legacy_topic()
    assert {topic["title"] for topic in store.list_topics()} == {"Anduril", "Palantir", "华为"}
    assert all(topic["counts"]["entities"] == 0 for topic in store.list_topics())


def test_unknown_fields_do_not_count_as_filled(store):
    topic = store.create_topic("A")
    entity = manual_entity(store, maturity={"stage": "未知", "basis": ""}, family="未公开")
    link(store, topic["id"], entity["id"])
    assert store.snapshot(topic["id"])["coverage"]["filled"] == 0
