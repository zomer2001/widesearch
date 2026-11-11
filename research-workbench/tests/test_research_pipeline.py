"""End-to-end source, model, typed SQL and API checks; all fixture facts are fictional."""

import pytest
from api.research_database import snapshot
from api.research_extraction import extract_document
from api.research_ingestion import ResearchIngestionJobs
from api.research_mapping import Mapper, Source, chunks
from api.research_materials import archive_session_pages, research_materials
from api.routes import research_database as routes
from fastapi import FastAPI
from fastapi.testclient import TestClient

from searchos.knowledge_store.database import ResearchDatabase
from searchos.socm.workspace import WorkspaceManager

TEXT = (
    "# Fictional Research\n\nProduct-X v2 operates for 2 hours. "
    "Team-X belongs to Institute-X and researches Route-X. "
    "Route-X implements Technique-X. Product-X v2 is used in Case-X."
)


def output():
    def ent(kind, name, fields=None):
        return {
            "entity_key": name,
            "kind": kind,
            "name": name,
            "identity_quote": TEXT,
            "fields": fields or [],
        }

    return {
        "entities": [
            ent(
                "product",
                "Product-X v2",
                [
                    {
                        "field_id": "product_endurance",
                        "value": {"value": 2, "unit": "h", "raw": "2 hours"},
                        "quote": TEXT,
                        "qualifiers": {"version": "v2"},
                    }
                ],
            ),
            ent("technology", "Technique-X"),
            ent("route", "Route-X"),
            ent("team", "Team-X"),
            ent("organization", "Institute-X"),
            ent("application", "Case-X"),
            ent("event", "Research", [{"field_id": "wrong_field", "value": "x", "quote": TEXT}]),
        ],
        "relations": [
            {
                "source_key": "Route-X",
                "target_key": "Technique-X",
                "predicate": "implements",
                "quote": TEXT,
            }
        ],
        "records": [
            {
                "subject_key": "Team-X",
                "kind": "participation",
                "label": "affiliated",
                "target_key": "Institute-X",
                "quote": TEXT,
            },
            {
                "subject_key": "Team-X",
                "kind": "participation",
                "label": "researcher",
                "target_key": "Route-X",
                "quote": TEXT,
            },
            {
                "subject_key": "Product-X v2",
                "kind": "participation",
                "label": "applied_product",
                "target_key": "Case-X",
                "quote": TEXT,
            },
        ],
    }


@pytest.fixture
def store(tmp_path):
    db = ResearchDatabase(tmp_path / "research.sqlite")
    db.initialize()
    return db


@pytest.fixture
def archive(tmp_path):
    workspaces, data = tmp_path / "runs", tmp_path / "library"
    ws = WorkspaceManager(workspaces, "test")
    ws.write_page("fiction", "https://example.invalid/fiction", TEXT, "Fictional Research")
    archive_session_pages("test", workspaces, data)
    material = research_materials(data, "test")
    return workspaces, data, material


def test_document_maps_typed_facts_with_evidence_and_retains_unmapped(store):
    source = Source("https://example.invalid/fiction", "fiction", TEXT, "fiction.md", "source")
    result = Mapper(store, source).apply(output())
    assert result["counts"]["fields"] == 4
    assert result["counts"]["relations"] == 1
    assert len(result["unmapped"]) == 1
    before = store.verify()["counts"]
    Mapper(store, source).apply(output())
    assert store.verify()["counts"] == before
    view = snapshot(store)
    assert any(row["label"] == "续航时长" for row in view["records"])
    assert {row["dimension_id"] for row in view["records"]} >= {2, 3, 4, 5, 6, 7}
    assert all(row["evidence"] for row in view["records"])


def test_false_quotes_and_mismatched_fields_are_not_published(store):
    value = output()
    value["entities"][0]["fields"][0]["quote"] = "this does not occur"
    source = Source("https://example.invalid/fiction", "fiction", TEXT, "fiction.md", "source")
    receipt = Mapper(store, source).apply(value)
    assert len(receipt["unmapped"]) == 2
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM metric_observations").fetchone()[0] == 0


def test_complete_chunk_coverage_and_stable_offsets():
    text = "paragraph\n\n" * 10000
    source = Source("https://example.invalid/long", "long", text, "long.txt", "long")
    parts = chunks(source, size=5000)
    assert len(parts) > 3
    assert "".join(p["text"] for p in parts) == text
    assert all(p["text"] == text[p["start"] : p["end"]] for p in parts)


def test_real_archived_original_to_database_with_injected_model(store, archive, tmp_path):
    workspaces, data, material = archive
    doc = material["documents"][0]
    calls = []

    def extract(payload):
        calls.append(payload)
        return output()

    result = extract_document(
        data,
        material["library_id"],
        doc["document_id"],
        doc["source_revision"],
        extractor=extract,
        database=store.path,
        scope="namespace: anduril",
        artifact_root=tmp_path / "artifacts",
    )
    assert calls and result["counts"]["fields"] == 4
    assert list((tmp_path / "artifacts").glob("*.json"))
    assert snapshot(store)["counts"]["fields"] >= 5


def test_job_retry_uses_cache_and_new_model_gets_independent_job(store, archive, tmp_path):
    workspaces, data, material = archive
    calls = []

    def factory(engine, model):
        def extract(payload):
            calls.append(model)
            return output()

        return extract

    jobs = ResearchIngestionJobs(data, store.path, extractor_factory=factory)
    first = jobs.schedule_session(
        "test", engine="openai", model="model-a", scope="namespace: anduril"
    )
    assert first["scheduled"] == 1
    job = jobs._list()[0]
    r1 = jobs._execute(job)
    r2 = jobs._execute(job)
    assert r1["model_calls"] == 1 and r2["model_calls"] == 0 and calls == ["model-a"]
    second = jobs.schedule_session(
        "test", engine="openai", model="model-b", scope="namespace: anduril"
    )
    assert second["scheduled"] == 1
    assert len(jobs._list()) == 2


def test_get_api_is_read_only_and_returns_scoped_fields_and_source(store, monkeypatch):
    source = Source("https://example.invalid/fiction", "fiction", TEXT, "fiction.md", "source")
    Mapper(store, source).apply(output())
    monkeypatch.setattr(routes, "DATABASE_PATH", store.path)
    monkeypatch.setattr(
        routes, "_jobs", lambda: ResearchIngestionJobs(store.path.parent / "library", store.path)
    )
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        response = client.get("/api/research-database?slug=anduril&dimension=5")
        assert response.status_code == 200
        assert response.json()["records"]
        assert all(r["dimension_id"] == 5 for r in response.json()["records"])
        assert client.get("/api/research-database?slug=huawei").json()["records"] == []
        assert (
            client.get("/api/research-database/sources/" + source.revision_key).json()[
                "text_content"
            ]
            == TEXT
        )
        assert client.get("/api/research-database/jobs").json()["jobs"] == []


def test_live_extraction_updates_legacy_read_model_without_another_model_call(store, archive):
    from api.knowledge_database import KnowledgeDatabase
    from api.research_extraction import project_legacy
    from api.research_mapping import library_source

    _, data, material = archive
    doc = material["documents"][0]
    result = extract_document(
        data,
        material["library_id"],
        doc["document_id"],
        doc["source_revision"],
        extractor=lambda payload: output(),
        database=store.path,
        scope="namespace: anduril",
    )
    source = library_source(
        data, material["library_id"], doc["document_id"], doc["source_revision"]
    )
    legacy = store.path.parent / "legacy.sqlite"
    projected = project_legacy(data, source, result, store, legacy)
    assert projected["model_calls"] == 0
    view = KnowledgeDatabase(data, legacy).snapshot()
    assert any(e["name"] == "Product-X v2" for e in view["entities"])


def test_automatic_scope_jobs_remain_visible_in_their_topic(store, archive):
    _, data, _ = archive
    jobs = ResearchIngestionJobs(data, store.path)
    jobs.schedule_session("test", engine="openai", model="test", scope="调查 Anduril 的技术")
    assert jobs.snapshot(scope="anduril")["counts"]["queued"] == 1
    assert jobs.snapshot(scope="huawei")["jobs"] == []


def test_structured_write_api_binds_original_and_records_receipt_without_model(
    store, archive, monkeypatch
):
    from api import knowledge_extraction_model

    _, data, material = archive
    document = material["documents"][0]
    jobs = ResearchIngestionJobs(data, store.path)
    monkeypatch.setattr(routes, "DATABASE_PATH", store.path)
    monkeypatch.setattr(routes, "LOCAL_DATA_ROOT", data)
    monkeypatch.setattr(routes, "_jobs", lambda: jobs)
    monkeypatch.setattr(
        routes,
        "available_documents",
        lambda slug: {
            **material,
            "documents": material["documents"] if slug == "anduril" else [],
        },
    )
    monkeypatch.setattr(
        knowledge_extraction_model,
        "make_extractor",
        lambda *a, **k: pytest.fail("offline submission cannot load a model"),
    )
    app = FastAPI()
    app.include_router(routes.router)
    body = {
        "slug": "anduril",
        "document_id": document["document_id"],
        "source_revision": document["source_revision"],
        "outputs": [output()],
    }
    with TestClient(app) as client:
        contract = client.get("/api/research-database/contract").json()
        assert len(contract["fields"]) == 425
        assert contract["runtime"]["model_enabled"] is False
        assert contract["runtime"]["model"] == "gpt-6-luna"
        assert contract["runtime"]["reasoning_effort"] == "low"
        first = client.post("/api/research-database/map", json=body)
        assert first.status_code == 200, first.text
        result = first.json()
        assert result["model_calls"] == 0 and result["counts"]["fields"] == 4
        assert len(result["unmapped"]) == 1
        before = store.verify()["counts"]
        repeat = client.post("/api/research-database/map", json=body)
        assert repeat.status_code == 200 and store.verify()["counts"] == before
        record = client.get("/api/research-database/jobs").json()["jobs"][0]
        assert record["mode"] == "structured" and record["model_calls"] == 0
        assert record["unmapped_count"] == 1
        assert len(jobs._list()) == 1
        assert (
            client.post(
                "/api/research-database/map",
                json={
                    **body,
                    "source_revision": "0" * 64,
                },
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/research-database/map",
                json={
                    **body,
                    "slug": "huawei",
                },
            ).status_code
            == 422
        )
        bad = output()
        bad["entities"][0]["identity_quote"] = "fabricated source sentence"
        assert (
            client.post(
                "/api/research-database/map",
                json={
                    **body,
                    "outputs": [bad],
                },
            ).status_code
            == 422
        )
        assert store.verify()["counts"] == before


def test_paused_model_paths_never_load_credentials_or_execute_on_startup(
    store, archive, monkeypatch
):
    import asyncio

    from api import knowledge_extraction_model, workbench
    from api.knowledge_ingestion_jobs import KnowledgeIngestionJobs
    from api.research_ingestion import schedule_for_session
    from api.routes import knowledge as legacy_routes

    workspaces, data, material = archive
    monkeypatch.setattr(workbench, "load_config", lambda: pytest.fail("no credentials needed"))
    monkeypatch.setattr(
        knowledge_extraction_model,
        "make_extractor",
        lambda *a, **k: pytest.fail("models are disabled"),
    )
    for cls in (KnowledgeIngestionJobs, ResearchIngestionJobs):
        jobs = cls(data, store.path, root=store.path.parent / cls.__name__)
        jobs.schedule_session("test", engine="cli", model="gpt-6-luna")
        assert jobs._claim() is None
        with pytest.raises(ValueError, match="已暂停"):
            jobs._execute(jobs._list()[0])

        async def restart():
            jobs.resume()
            await jobs.wait()

        asyncio.run(restart())
        assert jobs.snapshot()["counts"]["queued"] == 1
        assert jobs._list()[0]["model_calls"] == 0
    paused = asyncio.run(
        schedule_for_session("test", workspaces, data, engine="openai", model="gpt-4o")
    )
    assert paused["paused"] and paused["scheduled"] == 0
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(legacy_routes.router)
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/research-database/extract",
                json={
                    "document_ids": [material["documents"][0]["document_id"]],
                },
            ).status_code
            == 503
        )
        assert client.post("/api/knowledge/ingestion", json={}).status_code == 503
        assert client.post("/api/knowledge/ingestion/invalid/retry", json={}).status_code == 503
