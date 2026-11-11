"""Local-only promotion of saved research pages into the durable library.

The ledger lives outside the library so deleting a document (or the entire
library) cannot cause a later backfill to recreate it. No browser, model, or
configuration provider is used here. Research workspace archives remain intact.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from searchos.corpus.archive import _validate_record, canonical, digest, safe_file
from searchos.local.index.locks import advisory_lock
from searchos.local.index.source_registry import _atomic_write
from searchos.local.library import LocalLibraryService
from searchos.socm.workspace import resolve_session_path

logger = logging.getLogger(__name__)
LIBRARY_NAME = "研究网页资料"
_WORKER_ID = uuid.uuid4().hex
_backfill_tasks: dict[str, asyncio.Task] = {}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _root(data_root: str | Path) -> Path:
    return Path(data_root).expanduser().resolve() / "research_materials"


def _write(path: Path, data: dict) -> None:
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _read(path: Path, default: dict | None = None) -> dict:
    if not path.exists() and default is not None:
        return default
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("研究资料台账格式无效")
    return value


def _manifest_path(data_root: str | Path, session_id: str) -> Path:
    key = hashlib.sha256(session_id.encode()).hexdigest()
    return _root(data_root) / "sessions" / f"{key}.json"


def _page_key(url: str, body: str) -> str:
    return digest(canonical([url, digest(body.encode("utf-8"))]))


def _saved_pages(workspace: Path, errors: list[dict]) -> list[dict]:
    """Read verified immutable versions and successful legacy page aliases."""
    pages = workspace / "pages"
    archive = pages / "archive"
    output: dict[str, dict] = {}
    if not pages.resolve().is_relative_to(workspace.resolve()):
        errors.append({"source": "pages", "error": "UnsafePath"})
        return []

    def add(url, body, title="", page_id="", fetched_at=None, request_url=""):
        parsed = urlparse(url or "")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return
        if not body.strip() or body.lstrip().startswith("[FETCH_ERROR]"):
            return
        key = _page_key(url, body)
        output.setdefault(
            key,
            {
                "key": key,
                "url": url,
                "body": body,
                "title": title,
                "page_id": page_id,
                "fetched_at": fetched_at,
                "request_url": request_url or url,
            },
        )

    archive_paths = (
        sorted((archive / "records").glob("d_*.json"))
        if (archive.resolve().is_relative_to(pages.resolve()))
        else []
    )
    if not archive.resolve().is_relative_to(pages.resolve()):
        errors.append({"source": "archive", "error": "UnsafePath"})
    for path in archive_paths:
        try:
            path = safe_file(archive, f"records/{path.name}")
            record = _read(path)
            _validate_record(archive, record)
            body = safe_file(archive, record["text"]["path"]).read_text(encoding="utf-8")
            metadata = record.get("metadata") or {}
            add(
                record["source_url"],
                body,
                record.get("title", ""),
                metadata.get("page_id") or metadata.get("legacy_page_id", ""),
                record.get("fetched_at"),
                metadata.get("request_url", ""),
            )
        except Exception as exc:
            errors.append({"source": path.name, "error": type(exc).__name__})
    for path in sorted(pages.glob("*.meta.json")):
        try:
            meta = _read(safe_file(pages, path.name))
            body = safe_file(
                pages,
                meta.get("content_path")
                or path.name.replace(
                    ".meta.json",
                    ".md",
                ),
            ).read_text(encoding="utf-8")
            # Old workspaces occasionally stored a YAML header in the body.
            if body.startswith("---"):
                end = body.find("\n---\n", 4)
                if end >= 0:
                    body = body[end + 5 :].lstrip("\n")
            add(
                meta.get("source_url") or meta.get("url"),
                body,
                meta.get("title", ""),
                meta.get("page_id", path.name[:-10]),
                meta.get("fetched_at"),
                meta.get("url", ""),
            )
        except Exception as exc:
            errors.append({"source": path.name, "error": type(exc).__name__})
    return list(output.values())


def _filename(page: dict) -> str:
    value = page["title"] or urlparse(page["url"]).netloc or "网页资料"
    stem = re.sub(r"[^\w\-.\u4e00-\u9fff]+", "_", value).strip("._")
    return f"{(stem or '网页资料')[:90]}.md"


def archive_session_pages(
    session_id: str,
    workspace_root: str | Path,
    data_root: str | Path,
) -> dict:
    """Save and parse one session in a worker thread; isolate every page failure.

    Publishing the saved document ID before parsing makes retries safe: a saved
    original can be reparsed, while a user-deleted document is never reimported.
    """
    workspace = resolve_session_path(workspace_root, session_id)
    if not workspace.is_dir():
        raise FileNotFoundError("研究任务不存在")
    session_id = workspace.name
    errors: list[dict] = []
    candidates = _saved_pages(workspace, errors)
    root = _root(data_root)
    if not candidates and not _manifest_path(data_root, session_id).exists():
        state = _read(root / "state.json", {"library_id": ""})
        return {
            "session_id": session_id,
            "saved_documents": 0,
            "error_count": len(errors),
            "library_id": state["library_id"],
        }
    service = LocalLibraryService(data_root)
    with advisory_lock(root / ".lock"):
        path = _manifest_path(data_root, session_id)
        manifest = _read(path, {"session_id": session_id, "entries": {}, "errors": []})
        manifest["errors"] = errors
        state_path = root / "state.json"
        state = _read(state_path, {"schema_version": 1, "library_id": ""})
        if not candidates:
            manifest["updated_at"] = _now()
            _write(path, manifest)
            return {
                "session_id": session_id,
                "saved_documents": 0,
                "error_count": len(errors),
                "library_id": state["library_id"],
            }
        if not state["library_id"]:
            state["library_id"] = service.create_library(LIBRARY_NAME).library_id
            _write(state_path, state)
        library_id = state["library_id"]
        try:
            service.get_library(library_id)
        except KeyError:
            # Intentional library deletion is durable, including across restarts.
            return {
                "session_id": session_id,
                "saved_documents": 0,
                "error_count": 0,
                "library_id": library_id,
                "library_deleted": True,
            }
        available = {d["document_id"]: d for d in service.list_documents(library_id)}
        saved_count = 0
        for page in candidates:
            key = page["key"]
            entry = manifest["entries"].get(key)
            if entry and entry.get("status") == "deleted":
                continue
            if entry and entry.get("document_id"):
                if entry["document_id"] not in available:
                    entry["status"] = "deleted"
                    continue
                if entry.get("status") == "parsed":
                    continue
            try:
                if not entry or not entry.get("document_id"):
                    # Temporary files contain only the already saved page body.
                    root.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix=".page-", dir=root) as staging:
                        temporary = Path(staging) / "page.md"
                        temporary.write_text(page["body"], encoding="utf-8")
                        saved = service.save_original_file(
                            library_id,
                            temporary,
                            _filename(page),
                            origin={
                                "source": "research_task",
                                "session_id": session_id,
                                "page_id": page["page_id"],
                                "request_url": page["request_url"],
                                "final_url": page["url"],
                                "title": page["title"],
                                "fetched_at": page["fetched_at"],
                                "saved_form": "research_page_markdown_copy",
                            },
                            respect_deleted_revisions=True,
                        )
                    entry = {
                        "document_id": saved["document_id"],
                        "status": "saved",
                        "page_id": page["page_id"],
                        "source_url": page["url"],
                        "title": page["title"],
                        "saved_at": _now(),
                        "source_revision": digest(page["body"].encode("utf-8")),
                    }
                    manifest["entries"][key] = entry
                    available[saved["document_id"]] = saved
                    saved_count += int(not saved["deduplicated"])
                    _write(path, manifest)
                current = available[entry["document_id"]]
                if current.get("parse_status") == "pending":
                    current = asyncio.run(
                        service.parse_saved_document(library_id, entry["document_id"])
                    )
                    available[entry["document_id"]] = current
                if current.get("parse_status") == "failed":
                    raise ValueError("本地解析失败；已保留正文副本")
                entry["status"] = "parsed"
                entry.pop("error", None)
            except Exception as exc:
                if "SOURCE_DELETED" in str(exc):
                    manifest["entries"][key] = {
                        "document_id": "",
                        "status": "deleted",
                        "page_id": page["page_id"],
                        "source_url": page["url"],
                        "title": page["title"],
                        "saved_at": _now(),
                        "source_revision": digest(page["body"].encode("utf-8")),
                    }
                elif entry:
                    entry["error"] = type(exc).__name__
                if "SOURCE_DELETED" not in str(exc):
                    manifest["errors"].append({"source": key, "error": type(exc).__name__})
                    logger.warning("Could not archive research page (%s)", type(exc).__name__)
            manifest["updated_at"] = _now()
            _write(path, manifest)
        manifest["updated_at"] = _now()
        _write(path, manifest)
        return {
            "session_id": session_id,
            "saved_documents": saved_count,
            "error_count": len(manifest["errors"]),
            "library_id": library_id,
        }


async def archive_session_pages_safely(
    session_id: str,
    workspace_root: str | Path,
    data_root: str | Path,
) -> dict:
    try:
        return await asyncio.to_thread(archive_session_pages, session_id, workspace_root, data_root)
    except Exception as exc:
        logger.warning("Research materials archive failed (%s)", type(exc).__name__)
        return {
            "session_id": session_id,
            "saved_documents": 0,
            "error_count": 1,
            "error": type(exc).__name__,
        }


async def archive_and_schedule_knowledge(
    session_id: str,
    workspace_root: str | Path,
    data_root: str | Path,
    *,
    engine: str,
    model: str,
    scope: str,
) -> dict:
    """New Web/Hybrid research explicitly authorizes this extra model stage.

    The ordinary archive/backfill functions above remain entirely model-free.
    Queueing follows successful original parsing, never a generated report.
    """
    result = await archive_session_pages_safely(session_id, workspace_root, data_root)
    if not result.get("library_id") or result.get("library_deleted"):
        return result
    try:
        from api.knowledge_ingestion_jobs import schedule_for_session

        result["knowledge_ingestion"] = await schedule_for_session(
            session_id,
            workspace_root,
            data_root,
            engine=engine,
            model=model,
            scope=scope,
        )
    except Exception as exc:
        logger.warning("Knowledge extraction enqueue failed (%s)", type(exc).__name__)
        result["knowledge_ingestion"] = {"scheduled": 0, "error": type(exc).__name__}
    return result


def research_materials(data_root: str | Path, session_id: str = "") -> dict:
    root = _root(data_root)
    state = _read(root / "state.json", {"library_id": ""})
    library_id = state["library_id"]
    service = LocalLibraryService(data_root)
    deleted = False
    name = LIBRARY_NAME
    documents = []
    if library_id:
        try:
            name = service.get_library(library_id).name
            documents = service.list_documents(library_id)
        except KeyError:
            deleted = True
    by_id = {item["document_id"]: item for item in documents}
    associations: dict[str, dict[str, set]] = {}
    sessions = []
    selected = set()
    for path in sorted((root / "sessions").glob("*.json")):
        manifest = _read(path)
        sid = manifest["session_id"]
        if session_id and sid != session_id:
            continue
        live, removed = set(), set()
        for key, entry in manifest["entries"].items():
            doc_id = entry["document_id"]
            if doc_id in by_id:
                live.add(doc_id)
                assoc = associations.setdefault(doc_id, {"session_ids": set(), "page_ids": set()})
                assoc["session_ids"].add(sid)
                if entry["page_id"]:
                    assoc["page_ids"].add(entry["page_id"])
            else:
                removed.add(doc_id or key)
        selected.update(live)
        sessions.append(
            {
                "session_id": sid,
                "document_count": len(live),
                "deleted_count": len(removed),
                "error_count": len(manifest.get("errors", [])),
                "updated_at": manifest.get("updated_at", ""),
            }
        )
    documents = [
        {**by_id[key], **{field: sorted(values) for field, values in associations[key].items()}}
        for key in sorted(selected)
    ]
    jobs = sorted((root / "backfills").glob("backfill_*.json"), key=lambda p: p.name, reverse=True)
    latest = max((_read(p) for p in jobs), key=lambda j: j["created_at"], default=None)
    return {
        "library_id": library_id,
        "library_name": name,
        "library_deleted": deleted,
        "documents": documents,
        "sessions": sessions,
        "latest_backfill": _public_job(latest) if latest else None,
    }


def _job_path(data_root: str | Path, job_id: str) -> Path:
    if not re.fullmatch(r"backfill_[a-f0-9]{32}", job_id or ""):
        raise ValueError("资料归档作业 ID 无效")
    return _root(data_root) / "backfills" / f"{job_id}.json"


def _public_job(job: dict) -> dict:
    output = {key: value for key, value in job.items() if key != "worker_id"}
    if job["status"] in {"queued", "running"} and job.get("worker_id") != _WORKER_ID:
        output.update(
            status="interrupted", error="服务已重新启动；可重新提交存量归档，已保存资料不会重复导入"
        )
    return output


def get_backfill(data_root: str | Path, job_id: str) -> dict:
    return _public_job(_read(_job_path(data_root, job_id)))


def schedule_backfill(
    workspace_root: str | Path,
    data_root: str | Path,
    session_id: str = "",
) -> dict:
    """Explicit, asynchronous backfill. Startup never replays completed imports."""
    root = Path(workspace_root).expanduser().resolve()
    if session_id:
        workspace = resolve_session_path(root, session_id)
        if not workspace.is_dir():
            raise FileNotFoundError("研究任务不存在")
        session_ids = [workspace.name]
    else:
        session_ids = (
            sorted(
                path.name
                for path in root.iterdir()
                if path.is_dir() and not path.is_symlink() and (path / "pages").is_dir()
            )
            if root.exists()
            else []
        )
    for task_id in _backfill_tasks:
        current = _read(_job_path(data_root, task_id), {})
        if (
            current.get("status") in {"queued", "running"}
            and current.get("session_ids") == session_ids
        ):
            return _public_job(current)
    stamp = _now()
    job = {
        "job_id": f"backfill_{uuid.uuid4().hex}",
        "worker_id": _WORKER_ID,
        "status": "queued",
        "session_ids": session_ids,
        "total_sessions": len(session_ids),
        "completed_sessions": 0,
        "saved_documents": 0,
        "error_count": 0,
        "created_at": stamp,
        "updated_at": stamp,
        "error": "",
    }
    path = _job_path(data_root, job["job_id"])
    _write(path, job)

    async def run():
        try:
            job["status"] = "running"
            _write(path, job)
            for sid in session_ids:
                result = await archive_session_pages_safely(sid, root, data_root)
                job["completed_sessions"] += 1
                job["saved_documents"] += result["saved_documents"]
                job["error_count"] += result["error_count"]
                job["updated_at"] = _now()
                if result.get("library_deleted"):
                    job["error"] = "研究网页资料库已删除；已跳过归档"
                _write(path, job)
            job["status"] = (
                "completed_with_errors" if job["error_count"] or job["error"] else "completed"
            )
        except asyncio.CancelledError:
            job.update(status="interrupted", error="归档已中断；可重新提交")
            raise
        except Exception as exc:
            job.update(status="failed", error=type(exc).__name__)
        finally:
            job["updated_at"] = _now()
            _write(path, job)
            _backfill_tasks.pop(job["job_id"], None)

    _backfill_tasks[job["job_id"]] = asyncio.create_task(run())
    return _public_job(job)
