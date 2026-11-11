"""Durable queue tests use real archived originals and injected model calls only."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from api import research_materials
from api.knowledge_ingestion_jobs import KnowledgeIngestionJobs, _write
from api.routes import knowledge as routes
from fastapi import FastAPI
from fastapi.testclient import TestClient

from searchos.socm.workspace import WorkspaceManager


@pytest.fixture
def archives(tmp_path):
    workspace_root = tmp_path / "workspaces"
    data_root = tmp_path / "library"
    for sid in ("first", "second"):
        workspace = WorkspaceManager(workspace_root, sid)
        workspace.write_page(
            "a12345",
            "https://example.com/product",
            "# Anduril product\n\nThis is the documented public network interface of a product.",
            "归档产品原文",
        )
        workspace.write_page(
            "b12345",
            "https://example.com/technology",
            (
                "# Anduril technology\n\nThis second original describes "
                "a documented data conversion mechanism."
            ),
            "归档技术原文",
        )
        research_materials.archive_session_pages(sid, workspace_root, data_root)
    view = research_materials.research_materials(data_root, "first")
    return data_root, workspace_root, view


def queue(tmp_path, data_root, *, processor=None, factory=None):
    return KnowledgeIngestionJobs(
        data_root,
        tmp_path / "knowledge.sqlite",
        processor=processor,
        extractor_factory=factory or (lambda engine, model: lambda payload: {"ok": True}),
    )


def schedule(jobs, session="first", **kwargs):
    return jobs.schedule_session(
        session, engine="openai", model="gpt-4o", scope="Anduril 产品研究", **kwargs
    )


def result(status="completed", entities=1, relations=0):
    return {
        "status": status,
        "counts": {"entities": entities, "relations": relations},
        "model_calls": 1,
    }


def test_status_is_read_only_without_discovering_old_documents_or_loading_models(
    tmp_path, monkeypatch
):
    jobs = queue(tmp_path, tmp_path / "library")
    monkeypatch.setattr(
        research_materials,
        "research_materials",
        lambda *_: pytest.fail("GET cannot scan originals"),
    )
    assert jobs.snapshot()["counts"] == dict.fromkeys(
        ("queued", "running", "completed", "partial", "failed", "skipped"), 0
    )
    assert not jobs.root.exists()
    monkeypatch.setattr(routes, "_ingestion_jobs", lambda: jobs)
    from api import workbench

    monkeypatch.setattr(
        workbench, "load_config", lambda: pytest.fail("GET cannot load credentials")
    )
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        assert client.get("/api/knowledge/ingestion").json()["jobs"] == []
    assert not jobs.root.exists()


def test_schedule_deduplicates_document_version_and_preserves_both_session_associations(
    tmp_path, archives
):
    data_root, _, materials = archives
    jobs = queue(tmp_path, data_root)
    first = schedule(jobs)
    assert first == {"scheduled": 2, "reused": 0, "session_id": "first"}
    # CLI POST can omit scope after the automatic hook: inherit the existing
    # authorized namespace, rather than charge again under an empty namespace.
    second = jobs.schedule_session("first", engine="openai", model="gpt-4o", scope="")
    assert second["scheduled"] == 0 and second["reused"] == 2
    other = schedule(jobs, "second")
    assert other["scheduled"] == 0 and other["reused"] == 2
    assert len(jobs.snapshot()["jobs"]) == len(materials["documents"]) == 2
    assert len(jobs.snapshot("second")["jobs"]) == 2
    assert all(job["session_ids"] == ["first", "second"] for job in jobs.snapshot()["jobs"])
    assert all(job["model_calls"] == 0 for job in jobs.snapshot()["jobs"])
    # Distinct explicitly named research namespaces must not share entity scope.
    separate = jobs.schedule_session(
        "first", engine="openai", model="gpt-4o", scope="namespace:other-company"
    )
    assert separate["scheduled"] == 2


def test_document_filter_is_session_scoped_and_rejects_unknown_or_deleted_documents(
    tmp_path, archives
):
    data_root, _, materials = archives
    jobs = queue(tmp_path, data_root)
    selected = materials["documents"][0]["document_id"]
    assert schedule(jobs, document_ids=[selected])["scheduled"] == 1
    assert jobs.snapshot()["jobs"][0]["document_id"] == selected
    with pytest.raises(ValueError, match="本次研究"):
        schedule(jobs, document_ids=[selected, "unrelated-local-document"])
    assert len(jobs.snapshot()["jobs"]) == 1


def test_worker_is_serial_and_durable_and_repeated_schedule_never_reexecutes(tmp_path, archives):
    data_root, _, _ = archives
    seen = []

    def process(data, library, document, revision, *, extractor, **kwargs):
        seen.append(document)
        assert extractor({"document": document}) == {"ok": True}
        return result()

    jobs = queue(tmp_path, data_root, processor=process)

    async def run():
        schedule(jobs)
        jobs.kick()
        await jobs.wait()
        assert jobs.snapshot()["counts"]["completed"] == 2
        schedule(jobs)
        jobs.kick()
        await jobs.wait()

    asyncio.run(run())
    assert len(seen) == 2
    reloaded = queue(tmp_path, data_root, processor=lambda *_: pytest.fail("read must not extract"))
    assert reloaded.snapshot()["counts"]["completed"] == 2
    assert all(item["model_calls"] == 1 for item in reloaded.snapshot()["jobs"])


def test_failure_is_safe_not_retried_and_explicit_retry_reuses_completed_chunk_response(
    tmp_path, archives
):
    data_root, _, materials = archives
    called = []

    def factory(engine, model):
        def extract(payload):
            called.append(payload["chunk"])
            if payload["chunk"] == 2 and called.count(2) == 1:
                raise RuntimeError("secret-key-must-not-leak")
            return {"chunk": payload["chunk"]}

        return extract

    def process(*args, extractor, **kwargs):
        extractor({"chunk": 1})
        extractor({"chunk": 2})
        return result()

    jobs = queue(tmp_path, data_root, processor=process, factory=factory)

    async def run():
        schedule(jobs, document_ids=[materials["documents"][0]["document_id"]])
        jobs.kick()
        await jobs.wait()
        view = jobs.snapshot()
        assert view["counts"]["failed"] == 1
        assert view["jobs"][0]["model_calls"] == 2
        assert "secret-key-must-not-leak" not in json.dumps(view)
        jobs.kick()
        await jobs.wait()
        assert called == [1, 2]
        jobs.retry(view["jobs"][0]["id"])
        jobs.kick()
        await jobs.wait()
        assert jobs.snapshot()["counts"]["completed"] == 1
        assert jobs.snapshot()["jobs"][0]["model_calls"] == 1

    asyncio.run(run())
    assert called == [1, 2, 2]


def test_empty_and_rejected_results_cannot_look_like_successful_knowledge(tmp_path, archives):
    data_root, _, materials = archives
    jobs = queue(tmp_path, data_root, processor=lambda *args, **kwargs: result("completed", 0))

    async def run():
        schedule(jobs, document_ids=[materials["documents"][0]["document_id"]])
        jobs.kick()
        await jobs.wait()
        assert jobs.snapshot()["counts"]["skipped"] == 1

    asyncio.run(run())
    rejected = queue(
        tmp_path / "rejected", data_root, processor=lambda *args, **kwargs: result("rejected", 0)
    )

    async def reject():
        schedule(rejected, document_ids=[materials["documents"][0]["document_id"]])
        rejected.kick()
        await rejected.wait()
        job = rejected.snapshot()["jobs"][0]
        assert job["status"] == "partial" and "未形成可入库条目" in job["message"]

    asyncio.run(reject())


def test_startup_resumes_only_saved_authorized_queue_and_marks_dead_running_interrupted(
    tmp_path, archives
):
    data_root, _, _ = archives
    jobs = queue(tmp_path, data_root, processor=lambda *args, **kwargs: result())
    schedule(jobs)
    first, second = jobs._list()
    first.update(status="running", worker_pid=999999999, worker_id="dead")
    _write(jobs._path(first["id"]), first)
    # This imported invalid record is not sufficient to authorize a model call.
    second["authorized"] = False
    _write(jobs._path(second["id"]), second)

    async def run():
        jobs.resume()
        await jobs.wait()

    asyncio.run(run())
    view = jobs.snapshot()
    assert view["counts"]["failed"] == 1
    assert view["counts"]["queued"] == 1
    assert view["jobs"][0]["error"] == "" or view["jobs"][1]["error"] == "Interrupted"


def test_new_web_archive_hook_schedules_only_real_archived_docs_and_base_archive_stays_model_free(
    tmp_path, archives, monkeypatch
):
    data_root, workspace_root, materials = archives
    from api import knowledge_ingestion_jobs

    calls = []

    async def schedule_hook(session_id, workspaces, data, **kwargs):
        calls.append((session_id, kwargs))
        return {"scheduled": 2}

    monkeypatch.setattr(knowledge_ingestion_jobs, "schedule_for_session", schedule_hook)
    asyncio.run(research_materials.archive_session_pages_safely("first", workspace_root, data_root))
    assert calls == []
    response = asyncio.run(
        research_materials.archive_and_schedule_knowledge(
            "first",
            workspace_root,
            data_root,
            engine="openai",
            model="gpt-4o",
            scope="Anduril",
        )
    )
    assert response["knowledge_ingestion"]["scheduled"] == len(materials["documents"])
    assert calls == [("first", {"engine": "openai", "model": "gpt-4o", "scope": "Anduril"})]


def test_explicit_api_schedules_latest_session_only_and_rejects_outside_document_filter(
    tmp_path, archives, monkeypatch
):
    from api import field_extraction_policy

    # This fixture checks the enabled API contract with an injected local processor.
    monkeypatch.setattr(field_extraction_policy, "MODEL_EXTRACTION_ENABLED", True)
    data_root, _, materials = archives
    jobs = queue(tmp_path, data_root, processor=lambda *args, **kwargs: result())
    monkeypatch.setattr(routes, "_ingestion_jobs", lambda: jobs)
    monkeypatch.setattr(jobs, "kick", lambda: None)
    from api import workbench

    monkeypatch.setattr(
        workbench,
        "load_config",
        lambda: SimpleNamespace(engine="openai", openai=SimpleNamespace(model="gpt-4o")),
    )
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        bad = client.post(
            "/api/knowledge/ingestion",
            json={"session_id": "first", "document_ids": ["outside-session"]},
        )
        assert bad.status_code == 422
        good = client.post(
            "/api/knowledge/ingestion",
            json={
                "session_id": "first",
                "document_ids": [materials["documents"][0]["document_id"]],
            },
        )
        assert good.status_code == 200 and good.json()["scheduled"] == 1
        assert len(good.json()["jobs"]) == 1
        assert client.get("/api/knowledge/ingestion?session_id=unknown").json()["jobs"] == []
        assert client.post("/api/knowledge/ingestion/invalid/retry").status_code == 422
        missing = "ingest_" + "a" * 64
        assert client.post(f"/api/knowledge/ingestion/{missing}/retry").status_code == 404


def test_real_queue_to_extractor_to_sqlite_pipeline_uses_original_quotes(tmp_path, archives):
    from api.knowledge_database import KnowledgeDatabase

    from searchos.local.library import LocalLibraryService

    data_root, _, materials = archives
    document = next(
        item for item in materials["documents"] if item["origin"]["title"] == "归档产品原文"
    )
    seen = []

    def factory(engine, model):
        def extract(request):
            seen.append(request)
            segment = next(
                item for item in request["segments"] if "documented public network" in item["text"]
            )
            quote = segment["text"].strip()
            common = {
                "namespace": "anduril",
                "quote": quote,
                "segment_id": segment["segment_id"],
                "attributes": [],
            }
            return {
                "entities": [
                    {
                        **common,
                        "key": "product",
                        "kind": "product",
                        "name": "product",
                        "summary": "原文描述一种具有公开网络接口的产品。",
                        "category": "网络产品",
                    },
                    {
                        **common,
                        "key": "technology",
                        "kind": "technology",
                        "name": "公开网络接口",
                        "summary": "原文描述产品通过公开网络接口连接。",
                        "category": "通信机制",
                    },
                ],
                "relations": [
                    {
                        "source_key": "product",
                        "target_key": "technology",
                        "predicate": "uses_technology",
                        "quote": quote,
                        "segment_id": segment["segment_id"],
                    }
                ],
                "gaps": [],
            }

        return extract

    jobs = queue(tmp_path, data_root, factory=factory)

    async def run():
        schedule(jobs, document_ids=[document["document_id"]])
        jobs.kick()
        await jobs.wait()

    asyncio.run(run())
    assert len(seen) == 1
    view = jobs.snapshot()
    assert view["counts"]["completed"] == 1
    assert view["jobs"][0]["entities"] == 2 and view["jobs"][0]["relations"] == 1
    store = KnowledgeDatabase(data_root, jobs.database)
    snapshot = store.snapshot()
    assert snapshot["counts"]["entities"] == 2 and snapshot["counts"]["relations"] == 1
    evidence = snapshot["evidence"][0]
    resolved = LocalLibraryService(data_root).resolve_citation(evidence["ref_id"])
    assert evidence["quote"] in resolved["content"]
    # Reloading the app only reads the persisted graph; it doesn't repeat calls.
    assert KnowledgeDatabase(data_root, jobs.database).snapshot()["counts"]["entities"] == 2
    assert len(seen) == 1
