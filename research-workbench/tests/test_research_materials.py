from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from api import research_materials as materials

from searchos.local.library import LocalLibraryService
from searchos.socm.workspace import WorkspaceManager


def _workspace(tmp_path: Path, session_id="research-one"):
    root = tmp_path / "workspaces"
    workspace = WorkspaceManager(root, session_id)
    workspace.path.mkdir(parents=True, exist_ok=True)
    return root, workspace


def _page(
    workspace,
    page_id="abc123",
    text="# A page\n\nSaved research evidence.",
    url="https://example.com/news",
):
    workspace.write_page(page_id, url, text, "研究网页", original_html="<p>Research evidence.</p>")


def test_archive_preserves_successful_versions_and_readable_local_copy(tmp_path, monkeypatch):
    from searchos.local import pipeline

    def forbid_model(*args, **kwargs):
        raise AssertionError("Archive must never resolve or call a model")

    monkeypatch.setattr(pipeline, "_profile_model", forbid_model)
    root, workspace = _workspace(tmp_path)
    _page(workspace, text="# Version one\n\nOriginal evidence.")
    _page(workspace, text="# Version two\n\nUpdated evidence.")
    _page(workspace, text="[FETCH_ERROR] HTTP 403")
    data_root = tmp_path / "library"
    result = materials.archive_session_pages("research-one", root, data_root)
    view = materials.research_materials(data_root, "research-one")
    assert result["saved_documents"] == 2
    assert result["error_count"] == 0
    assert len(view["documents"]) == 2
    assert all(
        doc["searchable"] and doc["session_ids"] == ["research-one"] for doc in view["documents"]
    )
    service = LocalLibraryService(data_root)
    for doc in view["documents"]:
        assert doc["origin"]["saved_form"] == "research_page_markdown_copy"
        assert doc["origin"]["request_url"] == "https://example.com/news"
        index = service.document_index(view["library_id"], doc["document_id"])
        assert index["segments"]
        segment = service.document_segment(
            view["library_id"], doc["document_id"], index["segments"][0]["segment_id"]
        )
        assert "evidence" in segment["content"]
        assert service.original_path(view["library_id"], doc["document_id"]).suffix == ".md"
    assert materials.archive_session_pages("research-one", root, data_root)["saved_documents"] == 0


def test_deleted_document_and_library_are_not_recreated_on_replay(tmp_path):
    root, workspace = _workspace(tmp_path)
    _page(workspace)
    data_root = tmp_path / "library"
    materials.archive_session_pages("research-one", root, data_root)
    before = materials.research_materials(data_root)
    service = LocalLibraryService(data_root)
    service.delete_document(before["library_id"], before["documents"][0]["document_id"])
    assert materials.archive_session_pages("research-one", root, data_root)["saved_documents"] == 0
    after = materials.research_materials(data_root)
    assert after["documents"] == []
    assert after["sessions"][0]["deleted_count"] == 1
    service.delete_library(before["library_id"])
    replay = materials.archive_session_pages("research-one", root, data_root)
    assert replay["library_deleted"] is True
    assert materials.research_materials(data_root)["library_deleted"] is True
    assert service.list_libraries() == []


def test_cross_task_content_dedup_retains_both_origins_and_filters(tmp_path):
    root, first = _workspace(tmp_path, "one")
    _, second = _workspace(tmp_path, "two")
    _page(first)
    _page(second, page_id="def456", url="https://mirror.example.com/news")
    data_root = tmp_path / "library"
    materials.archive_session_pages("one", root, data_root)
    assert materials.archive_session_pages("two", root, data_root)["saved_documents"] == 0
    view = materials.research_materials(data_root)
    assert len(view["documents"]) == 1
    document = view["documents"][0]
    assert document["session_ids"] == ["one", "two"]
    assert {source["session_id"] for source in document["origin"]["sources"]} == {"one", "two"}
    assert (
        materials.research_materials(data_root, "two")["documents"][0]["document_id"]
        == document["document_id"]
    )
    assert materials.research_materials(data_root, "unknown")["documents"] == []


def test_deleted_body_is_not_revived_by_unimported_historical_task(tmp_path):
    root, first = _workspace(tmp_path, "one")
    _, second = _workspace(tmp_path, "two")
    _page(first)
    _page(second)
    data_root = tmp_path / "library"
    materials.archive_session_pages("one", root, data_root)
    view = materials.research_materials(data_root)
    service = LocalLibraryService(data_root)
    service.delete_document(view["library_id"], view["documents"][0]["document_id"])
    result = materials.archive_session_pages("two", root, data_root)
    assert result["saved_documents"] == 0
    assert result["error_count"] == 0
    assert materials.research_materials(data_root)["documents"] == []
    assert materials.research_materials(data_root, "two")["sessions"][0]["deleted_count"] == 1
    # A separate, explicit manual import remains possible.
    original = tmp_path / "manual.md"
    original.write_text("# A page\n\nSaved research evidence.")
    assert (
        service.save_original_file(view["library_id"], original, "manual.md")["status"] == "saved"
    )


def test_redirected_source_retains_requested_and_final_urls(tmp_path):
    root, workspace = _workspace(tmp_path)
    workspace.write_page(
        "abc123",
        "https://example.com/redirect",
        "# Final page\n\nEvidence.",
        source_url="https://example.com/final",
    )
    data_root = tmp_path / "library"
    materials.archive_session_pages("research-one", root, data_root)
    origin = materials.research_materials(data_root)["documents"][0]["origin"]
    assert origin["request_url"] == "https://example.com/redirect"
    assert origin["final_url"] == "https://example.com/final"


def test_legacy_pages_and_one_failed_save_do_not_block_other_pages(tmp_path, monkeypatch):
    root, workspace = _workspace(tmp_path)
    pages = workspace.path / "pages"
    pages.mkdir()
    for page_id, body in {
        "a": "# First\n\nSaved first.",
        "b": "# Second\n\nSaved second.",
        "c": "[FETCH_ERROR] unavailable",
        "d": "   ",
    }.items():
        (pages / f"{page_id}.md").write_text(body)
        (pages / f"{page_id}.meta.json").write_text(
            json.dumps(
                {
                    "page_id": page_id,
                    "url": f"https://example.com/{page_id}",
                    "title": page_id,
                }
            )
        )
    (pages / "bad.meta.json").write_text("invalid")
    original = LocalLibraryService.save_original_file

    def save(self, library_id, temporary, filename, **kwargs):
        if filename == "a.md":
            raise OSError("One page could not be saved")
        return original(self, library_id, temporary, filename, **kwargs)

    monkeypatch.setattr(LocalLibraryService, "save_original_file", save)
    data_root = tmp_path / "library"
    result = materials.archive_session_pages("research-one", root, data_root)
    assert result["saved_documents"] == 1
    assert result["error_count"] == 2
    assert len(materials.research_materials(data_root)["documents"]) == 1
    monkeypatch.setattr(LocalLibraryService, "save_original_file", original)
    assert materials.archive_session_pages("research-one", root, data_root)["saved_documents"] == 1


def test_local_parse_failure_keeps_copy_and_can_retry_without_reimport(tmp_path, monkeypatch):
    root, workspace = _workspace(tmp_path)
    _page(workspace)
    data_root = tmp_path / "library"
    original = LocalLibraryService.parse_saved_document

    async def fail(*args):
        raise RuntimeError("Parser unavailable")

    monkeypatch.setattr(LocalLibraryService, "parse_saved_document", fail)
    result = materials.archive_session_pages("research-one", root, data_root)
    saved = materials.research_materials(data_root)["documents"][0]
    assert result["error_count"] == 1
    assert saved["status"] == "saved"
    monkeypatch.setattr(LocalLibraryService, "parse_saved_document", original)
    assert materials.archive_session_pages("research-one", root, data_root)["saved_documents"] == 0
    parsed = materials.research_materials(data_root)["documents"][0]
    assert parsed["document_id"] == saved["document_id"]
    assert parsed["searchable"] is True


def test_published_failed_parse_retains_copy_without_overwriting_immutable_generation(
    tmp_path, monkeypatch
):
    from searchos.local.parsers.markdown import MarkdownParser

    root, workspace = _workspace(tmp_path)
    _page(workspace)
    original = MarkdownParser.parse

    async def fail(*args, **kwargs):
        raise ValueError("Local parser failed")

    monkeypatch.setattr(MarkdownParser, "parse", fail)
    data_root = tmp_path / "library"
    assert materials.archive_session_pages("research-one", root, data_root)["error_count"] == 1
    failed = materials.research_materials(data_root)["documents"][0]
    assert failed["parse_status"] == "failed"
    monkeypatch.setattr(MarkdownParser, "parse", original)
    result = materials.archive_session_pages("research-one", root, data_root)
    assert result["saved_documents"] == 0
    assert result["error_count"] == 1
    view = materials.research_materials(data_root)
    assert view["documents"][0]["document_id"] == failed["document_id"]
    service = LocalLibraryService(data_root)
    assert (
        "Saved research evidence"
        in service.original_path(view["library_id"], failed["document_id"]).read_text()
    )


def test_unsafe_page_paths_are_not_read(tmp_path):
    root, workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace.path / "pages").symlink_to(outside, target_is_directory=True)
    result = materials.archive_session_pages("research-one", root, tmp_path / "library")
    assert result["saved_documents"] == 0
    assert result["error_count"] == 1
    with pytest.raises(ValueError):
        materials.archive_session_pages("../outside", root, tmp_path / "library")


@pytest.mark.asyncio
async def test_asynchronous_backfill_is_durable_and_idempotent(tmp_path):
    root, first = _workspace(tmp_path, "one")
    _, second = _workspace(tmp_path, "two")
    _page(first)
    _page(second, text="# Another page\n\nAnother research source.")
    data_root = tmp_path / "library"
    job = materials.schedule_backfill(root, data_root)
    assert job["status"] == "queued"
    assert materials.schedule_backfill(root, data_root)["job_id"] == job["job_id"]
    await materials._backfill_tasks[job["job_id"]]
    final = materials.get_backfill(data_root, job["job_id"])
    assert final["status"] == "completed"
    assert final["completed_sessions"] == 2
    assert final["saved_documents"] == 2
    again = materials.schedule_backfill(root, data_root)
    await materials._backfill_tasks[again["job_id"]]
    assert materials.get_backfill(data_root, again["job_id"])["saved_documents"] == 0
    assert len(materials.research_materials(data_root)["documents"]) == 2


@pytest.mark.asyncio
async def test_safe_archive_boundary_isolates_storage_failure(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("Storage unavailable")

    monkeypatch.setattr(materials, "archive_session_pages", fail)
    result = await materials.archive_session_pages_safely("task", tmp_path, tmp_path)
    assert result["error_count"] == 1
    assert result["error"] == "OSError"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled", "archive_failed"])
async def test_research_finally_archives_after_complete_failure_and_cancellation(
    tmp_path, monkeypatch, outcome
):
    from types import SimpleNamespace

    from api import workbench
    from api.routes import search

    from searchos.config.settings import settings
    from searchos.harness import session
    from searchos.socm import SearchState

    monkeypatch.setattr(search, "WORKSPACE_ROOT", tmp_path / "workspaces")
    monkeypatch.setattr(search, "LOCAL_DATA_ROOT", tmp_path / "library")
    monkeypatch.setattr(search, "sessions", {})
    monkeypatch.setattr(settings, "max_parallel_agents", 3)

    async def select_engine(*args):
        return workbench.Selection("ollama", "fake-local-model", "http://127.0.0.1:11434/v1")

    class Harness:
        def __init__(self, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            workspace = WorkspaceManager(search.WORKSPACE_ROOT, kwargs["session_id"])
            _page(workspace)
            if outcome == "failed":
                raise ValueError("Research failed after saving a page")
            if outcome == "cancelled":
                raise asyncio.CancelledError()
            return SimpleNamespace(search_state=kwargs["initial_state"])

    monkeypatch.setattr(workbench, "select_engine", select_engine)
    monkeypatch.setattr(workbench, "build_models", lambda _: {"orchestrator": object()})
    monkeypatch.setattr(session, "SearchSession", Harness)
    original = materials.archive_session_pages_safely

    async def verify_settings_restored(*args):
        assert settings.max_parallel_agents == 3
        return await original(*args)

    monkeypatch.setattr(materials, "archive_session_pages_safely", verify_settings_restored)
    if outcome == "archive_failed":

        def fail(*args):
            raise OSError("Archive storage unavailable")

        monkeypatch.setattr(materials, "archive_session_pages", fail)
    response = await search._launch_search(
        req=search.SearchRequest(query="research", effort="medium"),
        session_id="task",
        state=SearchState(intent="research", source_scope={"mode": "local"}),
        context_preamble=None,
        follow_up=False,
        query="research",
    )
    task = search.sessions[response.session_id]["task"]
    if outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    expected = "completed" if outcome in {"completed", "archive_failed"} else "error"
    assert search.sessions["task"]["status"] == expected
    if outcome != "archive_failed":
        assert len(materials.research_materials(search.LOCAL_DATA_ROOT)["documents"]) == 1


@pytest.mark.asyncio
async def test_http_backfill_and_existing_view_delete_apis(tmp_path, monkeypatch):
    import httpx
    from api import topic_recap_jobs
    from api.routes import local_search
    from fastapi import FastAPI

    root, workspace = _workspace(tmp_path)
    _page(workspace)
    data_root = tmp_path / "library"
    monkeypatch.setattr(local_search, "WORKSPACE_ROOT", root)
    monkeypatch.setattr(local_search, "LOCAL_DATA_ROOT", data_root)
    monkeypatch.setattr(topic_recap_jobs, "schedule_topic_recaps", lambda *args: None)
    app = FastAPI()
    app.include_router(local_search.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/api/local/research-materials/backfill", json={})
        assert response.status_code == 202
        job = response.json()
        await materials._backfill_tasks[job["job_id"]]
        assert (await client.get(f"/api/local/research-materials/backfill/{job['job_id']}")).json()[
            "status"
        ] == "completed"
        view = (
            await client.get("/api/local/research-materials", params={"session_id": "research-one"})
        ).json()
        document = view["documents"][0]
        prefix = f"/api/local/libraries/{view['library_id']}/documents/{document['document_id']}"
        index = (await client.get(prefix + "/index")).json()
        segment = await client.get(
            prefix + "/segment", params={"segment_id": index["segments"][0]["segment_id"]}
        )
        assert "Saved research evidence" in segment.json()["content"]
        assert (await client.get(prefix + "/original")).status_code == 200
        assert (await client.delete(prefix)).status_code == 200
        assert (await client.get("/api/local/research-materials")).json()["documents"] == []
