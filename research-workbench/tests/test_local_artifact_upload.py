"""Generated collection reports can enter the library without model enrichment."""
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))
from api.routes import local_search
from searchos.local.library import LocalLibraryService


def test_local_only_upload_is_readable_deduplicated_and_never_resurrects_deleted(tmp_path, monkeypatch):
    service = LocalLibraryService(tmp_path / "data")
    library_id = service.create_library("Collection output").library_id
    monkeypatch.setattr(local_search, "_service", lambda: service)

    def forbid_model_job(*args, **kwargs):
        raise AssertionError("Generated artifacts must not schedule model enrichment")
    monkeypatch.setattr(local_search, "schedule_background_index", forbid_model_job)
    app = FastAPI()
    app.include_router(local_search.router)
    body = b"# Product and technology collection\n\nA source-backed technical report.\n"
    url = f"/api/local/libraries/{library_id}/uploads?local_only=true"
    with TestClient(app) as client:
        assert client.get("/api/local/capabilities").json()["local_only_upload"] is True
        first = client.post(url, files={"file": ("report.md", body, "text/markdown")})
        assert first.status_code == 200, first.text
        saved = first.json()
        assert saved["index_job"] is None
        assert saved["searchable"] is True
        doc_id = saved["document_id"]
        index = service.document_index(library_id, doc_id)
        segment = service.document_segment(library_id, doc_id, index["segments"][0]["segment_id"])
        assert "Product and technology" in segment["content"]
        second = client.post(url, files={"file": ("report.md", body, "text/markdown")})
        assert second.json()["document_id"] == doc_id
        service.delete_document(library_id, doc_id)
        replay = client.post(url, files={"file": ("report.md", body, "text/markdown")})
        assert replay.status_code == 409
        assert "SOURCE_DELETED" in replay.json()["detail"]
        assert service.list_documents(library_id) == []


def test_failed_local_parse_still_returns_saved_downloadable_original(tmp_path, monkeypatch):
    service = LocalLibraryService(tmp_path / "data")
    library_id = service.create_library("Collection output").library_id
    monkeypatch.setattr(local_search, "_service", lambda: service)
    async def fail_parse(*args, **kwargs):
        raise RuntimeError("temporary parser failure")
    monkeypatch.setattr(service, "parse_saved_document", fail_parse)
    app = FastAPI()
    app.include_router(local_search.router)
    with TestClient(app) as client:
        response = client.post(f"/api/local/libraries/{library_id}/uploads?local_only=true",
            files={"file": ("report.md", b"# Saved output", "text/markdown")})
    assert response.status_code == 200
    saved = response.json()
    assert saved["parse_status"] == "failed"
    assert saved["index_job"] is None
    assert service.original_path(library_id, saved["document_id"]).read_bytes() == b"# Saved output"
