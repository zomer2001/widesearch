"""Offline end-to-end API/CLI checks against isolated fictional business data."""

import json

import pytest
from api.knowledge_handoff.cli import inspect_package
from api.knowledge_handoff.models import ResultEnvelope
from api.knowledge_handoff.package import export_package
from api.knowledge_handoff.service import HandoffService
from api.knowledge_handoff.summaries import save_summary
from api.research_materials import archive_session_pages, research_materials
from api.routes import research_database as routes
from fastapi import FastAPI
from fastapi.testclient import TestClient

from searchos.knowledge_store.database import ResearchDatabase
from searchos.local.library import LocalLibraryService
from searchos.socm.workspace import WorkspaceManager

TEXT = "Fictional Product-X supports publish-subscribe messaging. It uses fictional Tech-X."


@pytest.fixture
def context(tmp_path, monkeypatch):
    import socket

    monkeypatch.setattr(
        socket.socket,
        "connect",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network forbidden")),
    )
    store = ResearchDatabase(tmp_path / "business" / "research.sqlite")
    store.initialize()
    data = tmp_path / "library"
    workspace = WorkspaceManager(tmp_path / "workspaces", "business-only")
    workspace.write_page(
        "fiction", "https://example.invalid/product-x", TEXT, "Fictional Product-X"
    )
    archive_session_pages("business-only", tmp_path / "workspaces", data)
    material = research_materials(data)
    doc = material["documents"][0]

    def catalog(slug):
        return {
            "library_id": material["library_id"],
            "documents": material["documents"] if slug == "anduril" else [],
        }

    service = HandoffService(store.path, data, catalog)
    envelope = {
        "schema_version": "knowledge-handoff-v2",
        "document_id": doc["document_id"],
        "source_revision": doc["source_revision"],
        "status": "completed",
        "outputs": [
            {
                "entities": [
                    {
                        "entity_key": "product",
                        "kind": "product",
                        "name": "Product-X",
                        "identity_quote": TEXT,
                        "fields": [
                            {
                                "field_id": "product_supports_publish_subscribe",
                                "value": True,
                                "quote": TEXT,
                            }
                        ],
                    }
                ]
            }
        ],
        "summary": {"kind": "extractive", "text": TEXT, "quotes": [TEXT]},
    }
    monkeypatch.setattr(routes, "DATABASE_PATH", store.path)
    monkeypatch.setattr(routes, "LOCAL_DATA_ROOT", data)
    monkeypatch.setattr(routes, "available_documents", catalog)
    app = FastAPI()
    app.include_router(routes.router)
    return service, envelope, store, material, TestClient(app)


def test_preview_commit_and_summary_are_independent(context):
    service, envelope, store, material, client = context
    source = service.source("anduril", envelope["document_id"])
    body = source.text
    before = store.verify()["counts"]
    response = client.post(
        "/api/research-database/handoff/preview", json={"slug": "anduril", "result": envelope}
    )
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["sql_statements"] and plan["can_commit"]
    assert store.verify()["counts"] == before
    assert service.document("anduril", envelope["document_id"])["summaries"] == []
    result = client.post(
        "/api/research-database/handoff/commit",
        json={"slug": "anduril", "plan_id": plan["plan_id"]},
    )
    assert result.status_code == 200, result.text
    assert result.json()["model_calls"] == 0
    document = client.get(
        f"/api/research-database/handoff/documents/{envelope['document_id']}"
    ).json()
    assert document["text"] == body
    assert document["summaries"][0]["text"] == TEXT
    assert document["summaries"][0]["current"] is True
    assert service.get_plan("anduril", plan["plan_id"])["status"] == "committed"
    counts = store.verify()["counts"]
    assert service.commit("anduril", plan["plan_id"])["reused"]
    assert store.verify()["counts"] == counts


def test_summary_alone_does_not_create_fact_rows(context):
    service, envelope, store, _, client = context
    envelope["outputs"] = []
    before = store.verify()["counts"]
    response = client.post(
        "/api/research-database/handoff/summaries", json={"slug": "anduril", "result": envelope}
    )
    assert response.status_code == 200, response.text
    assert store.verify()["counts"] == before
    envelope["summary"]["text"] = "This unsupported paraphrase is not an extract."
    response = client.post(
        "/api/research-database/handoff/preview", json={"slug": "anduril", "result": envelope}
    )
    assert response.status_code == 422
    assert not list((service.root / "plans").glob("*.json"))


def test_deleted_source_and_topic_mismatch_reject_commit(context):
    service, envelope, store, material, client = context
    plan = service.preview("anduril", envelope)
    assert (
        client.get(
            f"/api/research-database/handoff/plans/{plan['plan_id']}?slug=huawei"
        ).status_code
        == 422
    )
    LocalLibraryService(service.data_root).delete_document(
        material["library_id"], envelope["document_id"]
    )
    with pytest.raises(ValueError, match="删除"):
        service.commit("anduril", plan["plan_id"])
    assert store.verify()["counts"]["claims"] == 0
    assert (
        client.get(
            f"/api/research-database/handoff/documents/{envelope['document_id']}"
        ).status_code
        == 422
    )


def test_package_check_computes_progress_from_results_not_claimed_progress(context, tmp_path):
    service, envelope, store, material, _ = context
    destination = tmp_path / "handoff"
    result = export_package(
        data_root=service.data_root,
        database=store.path,
        slug="anduril",
        documents=material["documents"],
        library_id=material["library_id"],
        output_dir=destination,
    )
    assert result["model_calls"] == 0
    state = inspect_package(destination)
    assert state["counts"]["pending"] == 1
    manifest = json.loads((destination / "manifest.json").read_text())
    (destination / "errors.jsonl").write_text(
        json.dumps(
            {
                "source_id": manifest["sources"][0]["source_id"],
                "stage": "extraction",
                "reason": "fictional temporary failure",
                "retryable": True,
            }
        )
        + "\n"
    )
    assert inspect_package(destination)["counts"]["failed"] == 1
    output = destination / manifest["sources"][0]["result_path"]
    output.write_text(json.dumps(envelope))
    (destination / "progress.json").write_text('{"completed": 99999}')
    state = inspect_package(destination, write_report=True)
    assert state["counts"]["completed"] == 1 and state["counts"]["total"] == 1
    assert (destination / "validation-report.json").exists()
    (destination / manifest["sources"][0]["path"]).write_text("changed body")
    assert inspect_package(destination)["counts"]["invalid"] == 1
    assert store.verify()["counts"]["claims"] == 0


def test_summary_revision_retains_old_derivative_and_flags_it(context):
    service, envelope, _, _, _ = context
    source = service.source("anduril", envelope["document_id"])
    save_summary(service.root, source, envelope["summary"], slug="anduril")
    from api.knowledge_handoff.summaries import list_summaries

    records = list_summaries(
        service.root, slug="anduril", document_id=source.source_id, source_revision="0" * 64
    )
    assert len(records) == 1 and records[0]["current"] is False


def test_no_relevant_status_and_experiment_roots_are_guarded(tmp_path):
    with pytest.raises(ValueError):
        ResultEnvelope.model_validate(
            {"document_id": "x", "source_revision": "0" * 64, "outputs": [], "status": "completed"}
        )
    with pytest.raises(ValueError, match="实验"):
        HandoffService(tmp_path / "widesearch_experiment/research.sqlite", tmp_path, lambda s: {})


def test_empty_completed_output_is_rejected():
    with pytest.raises(ValueError, match="空结果"):
        ResultEnvelope.model_validate(
            {
                "document_id": "x",
                "source_revision": "0" * 64,
                "outputs": [{}],
                "status": "completed",
            }
        )


def test_submission_and_summary_cannot_be_changed_after_preview(context):
    service, envelope, store, _, _ = context
    plan = service.preview("anduril", envelope)
    path = service.root / "submissions" / (plan["plan_id"] + ".json")
    saved = json.loads(path.read_text())
    saved["result"]["summary"]["text"] = "unreviewed replacement"
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="被修改"):
        service.commit("anduril", plan["plan_id"])
    assert store.verify()["counts"]["claims"] == 0
    assert service.status("anduril")["errors"][0]["plan_id"] == plan["plan_id"]
