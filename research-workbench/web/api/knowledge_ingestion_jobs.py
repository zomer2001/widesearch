"""Durable, explicitly authorized original-to-knowledge extraction jobs.

Reading status never scans source documents or resolves model credentials. Only
new Web/Hybrid research or an explicit POST can authorize extraction; startup
resumes saved queued work, never discovers historical documents on its own.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from api.knowledge_database import DEFAULT_DATABASE
from searchos.local.index.locks import advisory_lock

logger = logging.getLogger(__name__)
STATUSES = ("queued", "running", "completed", "partial", "failed", "skipped")
_WORKER_ID = uuid.uuid4().hex
_PUBLIC_FIELDS = (
    "id",
    "session_id",
    "session_ids",
    "document_id",
    "title",
    "status",
    "message",
    "engine",
    "model",
    "created_at",
    "updated_at",
    "entities",
    "relations",
    "error",
    "model_calls",
    "attempt",
)


def _now():
    return datetime.now(UTC).isoformat()


def _write(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".ingestion-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True


class KnowledgeIngestionJobs:
    def __init__(
        self,
        data_root: str | Path,
        database: str | Path | None = None,
        *,
        root: str | Path | None = None,
        processor: Callable | None = None,
        extractor_factory: Callable | None = None,
    ):
        self.data_root = Path(data_root)
        self.database = Path(database) if database is not None else DEFAULT_DATABASE
        self.root = Path(root) if root is not None else self.database.parent / "ingestion"
        self.processor = processor
        self.extractor_factory = extractor_factory
        self.worker: asyncio.Task | None = None
        self.stopping = False
        self._pending_kick = False

    def _path(self, identity):
        if not re.fullmatch(r"ingest_[a-f0-9]{64}", identity or ""):
            raise ValueError("自动入库作业 ID 无效")
        return self.root / f"{identity}.json"

    def _read(self, identity):
        data = json.loads(self._path(identity).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("id") != identity:
            raise ValueError("自动入库作业记录无效")
        return data

    def _list(self):
        jobs = []
        for path in self.root.glob("ingest_*.json"):
            try:
                job = self._read(path.stem)
                if job.get("status") in STATUSES:
                    jobs.append(job)
            except (OSError, ValueError, TypeError):
                logger.warning("Could not read ingestion job")
        return sorted(jobs, key=lambda item: (item["created_at"], item["id"]))

    def snapshot(self, session_id: str = ""):
        from api import field_extraction_policy

        jobs = [
            job
            for job in self._list()
            if not session_id or session_id in job.get("session_ids", [job["session_id"]])
        ]
        return {
            "enabled": field_extraction_policy.MODEL_EXTRACTION_ENABLED,
            "counts": {status: sum(job["status"] == status for job in jobs) for status in STATUSES},
            "jobs": [
                {
                    key: item[key]
                    for key in getattr(self, "public_fields", _PUBLIC_FIELDS)
                    if key in item
                }
                for item in reversed(jobs[-200:])
            ],
            "total_jobs": len(jobs),
            "scope_hint": "新网页研究自动入库；手动补抽取仅处理最近已归档研究任务。",
        }

    def enqueue_documents(
        self, documents, *, session_id, library_id, engine, model, scope="", retry_failed=False
    ):
        from api.knowledge_extraction import PROMPT_VERSION, scope_namespace

        PROMPT_VERSION = getattr(self, "prompt_version", PROMPT_VERSION)

        if engine not in {"openai", "deepseek", "ollama", "cli"} or not model.strip():
            raise ValueError("请选择有效的抽取模型")
        scheduled = reused = 0
        with advisory_lock(self.root / ".lock"):
            for document in documents:
                if document.get("parse_status") in {"pending", "failed"} or not document.get(
                    "searchable"
                ):
                    continue
                revision = document.get("source_revision", "")
                if not re.fullmatch(r"[a-f0-9]{64}", revision):
                    continue
                identity = (
                    "ingest_"
                    + hashlib.sha256(
                        json.dumps(
                            [
                                library_id,
                                document["document_id"],
                                revision,
                                PROMPT_VERSION,
                                scope_namespace(scope),
                                *(
                                    [engine, model]
                                    if getattr(self, "model_sensitive_jobs", False)
                                    else []
                                ),
                            ],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()
                )
                path = self._path(identity)
                if path.exists():
                    job = self._read(identity)
                    job["session_ids"] = sorted(
                        set(job.get("session_ids", [job["session_id"]])) | {session_id}
                    )
                    if retry_failed and job["status"] in {"failed", "partial"}:
                        if job["status"] == "partial":
                            job["cache_epoch"] = job.get("attempt", 1) + 1
                        job.update(
                            status="queued",
                            error="",
                            message="等待重新抽取",
                            engine=engine,
                            model=model,
                            attempt=job.get("attempt", 1) + 1,
                        )
                        scheduled += 1
                    else:
                        reused += 1
                    job["updated_at"] = _now()
                    _write(path, job)
                    continue
                timestamp = _now()
                origin = document.get("origin") or {}
                job = {
                    "id": identity,
                    "session_id": session_id,
                    "session_ids": [session_id],
                    "library_id": library_id,
                    "document_id": document["document_id"],
                    "source_revision": revision,
                    "title": origin.get("title") or document.get("name", "归档原文"),
                    "status": "queued",
                    "message": "等待从归档原文抽取实体和关系",
                    "error": "",
                    "engine": engine,
                    "model": model,
                    "scope": scope[:12000],
                    "scope_namespace": scope_namespace(scope),
                    "prompt_version": PROMPT_VERSION,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "entities": 0,
                    "relations": 0,
                    "model_calls": 0,
                    "attempt": 1,
                    "cache_epoch": 1,
                    "authorized": True,
                }
                _write(path, job)
                scheduled += 1
        return {"scheduled": scheduled, "reused": reused, "session_id": session_id}

    def schedule_session(
        self,
        session_id,
        *,
        engine,
        model,
        scope="",
        retry_failed=False,
        workspace_root=None,
        document_ids=None,
    ):
        from api.research_materials import research_materials

        materials = research_materials(self.data_root, session_id)
        if materials["library_deleted"]:
            raise ValueError("研究原文资料库已删除")
        if not session_id:
            latest = max(
                materials["sessions"], key=lambda item: item.get("updated_at", ""), default=None
            )
            if latest is None:
                return {"scheduled": 0, "reused": 0, "session_id": ""}
            session_id = latest["session_id"]
            materials = research_materials(self.data_root, session_id)
        if not any(item["session_id"] == session_id for item in materials["sessions"]):
            raise KeyError("该研究尚无已归档原文")
        if not scope:
            previous = next(
                (job for job in self._list() if session_id in job.get("session_ids", [])), None
            )
            if previous:
                scope = previous.get("scope", "")
            elif workspace_root is not None:
                from searchos.socm.workspace import WorkspaceManager

                scope = WorkspaceManager(workspace_root, session_id).load_state().intent
        documents = materials["documents"]
        if document_ids is not None:
            selected = set(document_ids)
            available = {item["document_id"] for item in documents}
            if not selected or not selected <= available:
                raise ValueError("只能选择本次研究已归档且仍可用的原文")
            documents = [item for item in documents if item["document_id"] in selected]
        return self.enqueue_documents(
            documents,
            session_id=session_id,
            library_id=materials["library_id"],
            engine=engine,
            model=model,
            scope=scope,
            retry_failed=retry_failed,
        )

    def retry(self, identity):
        with advisory_lock(self.root / ".lock"):
            job = self._read(identity)
            if job["status"] not in {"failed", "partial"}:
                raise ValueError("只有失败或部分完成的入库作业可以重试")
            if job["status"] == "partial":
                job["cache_epoch"] = job.get("attempt", 1) + 1
            job.update(
                status="queued",
                error="",
                message="等待重新抽取",
                updated_at=_now(),
                attempt=job.get("attempt", 1) + 1,
            )
            _write(self._path(identity), job)
        return job

    def resume(self):
        # No calls to research_materials here: completed historical tasks are not
        # scanned merely because the local app opens.
        with advisory_lock(self.root / ".lock"):
            for job in self._list():
                if job["status"] == "running" and not _alive(job.get("worker_pid")):
                    job.update(
                        status="failed",
                        error="Interrupted",
                        message="上次抽取被中断；可手动重试，已缓存的响应会复用",
                        updated_at=_now(),
                    )
                    _write(self._path(job["id"]), job)
        self.kick()

    def _claim(self):
        with advisory_lock(self.root / ".lock"):
            for job in self._list():
                if (
                    job["status"] == "running"
                    and _alive(job.get("worker_pid"))
                    and job.get("worker_id") != _WORKER_ID
                ):
                    return None
            for job in self._list():
                if (
                    job["status"] == "queued"
                    and job.get("authorized") is True
                    and self._can_execute(job)
                ):
                    job.update(
                        status="running",
                        message="正在读取原文、抽取并校验",
                        updated_at=_now(),
                        worker_id=_WORKER_ID,
                        worker_pid=os.getpid(),
                        model_calls=0,
                    )
                    _write(self._path(job["id"]), job)
                    return job
        return None

    def _can_execute(self, job):
        from api import field_extraction_policy

        return bool(
            self.extractor_factory
            or self.processor
            or field_extraction_policy.MODEL_EXTRACTION_ENABLED
        )

    def _execute(self, job):
        if not self._can_execute(job):
            from api.field_extraction_policy import PAUSED_MESSAGE

            raise ValueError(PAUSED_MESSAGE)
        from api.knowledge_extraction import extract_document

        factory = self.extractor_factory
        if factory is None:
            from api.knowledge_extraction_model import make_extractor

            factory = make_extractor
        extractor = factory(job["engine"], job["model"])
        actual_calls = 0
        cache = self.root / "responses" / job["id"] / str(job.get("cache_epoch", 1))

        def cached_extract(payload):
            nonlocal actual_calls
            key = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            path = cache / f"{key}.json"
            if path.exists():
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(value, dict):
                        return value
                except (OSError, ValueError):
                    pass
            actual_calls += 1
            # Persist attempted requests even when the provider later times out.
            self._finish(job["id"], model_calls=actual_calls)
            value = extractor(payload)
            if not isinstance(value, dict):
                raise ValueError("模型未返回结构化对象")
            _write(path, value)
            return value

        with advisory_lock(self.root / ".execution.lock"):
            result = (self.processor or extract_document)(
                self.data_root,
                job["library_id"],
                job["document_id"],
                job["source_revision"],
                extractor=cached_extract,
                database=self.database,
                scope=job["scope"],
                force=job.get("attempt", 1) > 1,
            )
        result["extraction_calls"] = result.get("model_calls", 0)
        result["model_calls"] = actual_calls
        return result

    async def _run(self):
        while not self.stopping:
            job = await asyncio.to_thread(self._claim)
            if job is None:
                return
            try:
                result = await asyncio.to_thread(self._execute, job)
                status = result.get("status", "failed")
                if status == "rejected":
                    status = "partial"
                if status not in {"completed", "partial", "skipped"}:
                    status = "failed"
                counts = result.get("counts") or {}
                if status == "completed" and not (
                    counts.get("entities") or counts.get("relations")
                ):
                    status = "skipped"
                message = {
                    "completed": "原文抽取与入库已完成",
                    "partial": "已保存通过校验的部分条目，仍有截断或未通过的内容",
                    "skipped": "当前原文没有可入库的实体关系，或来源已不可用",
                    "failed": "抽取未完成，请检查模型连接后重试",
                }[status]
                if status == "partial" and not (counts.get("entities") or counts.get("relations")):
                    message = "本次未形成可入库条目，输出没有通过原文校验"
                changes = {
                    "status": status,
                    "message": message,
                    "entities": int(counts.get("entities", 0)),
                    "relations": int(counts.get("relations", 0)),
                    "model_calls": int(result.get("model_calls", 0)),
                    "error": "",
                    "result": result,
                }
            except asyncio.CancelledError:
                self._finish(
                    job["id"],
                    status="failed",
                    error="Interrupted",
                    message="服务停止时抽取被中断；请手动重试",
                )
                raise
            except Exception as exc:
                source_error = isinstance(exc, (FileNotFoundError, KeyError)) or (
                    isinstance(exc, ValueError)
                    and any(
                        marker in str(exc).lower()
                        for marker in (
                            "revision",
                            "original",
                            "source_",
                            "failed_fetch",
                            "snippet",
                            "no_verified",
                            "no_substantive",
                        )
                    )
                )
                changes = {
                    "status": "skipped" if source_error else "failed",
                    "error": type(exc).__name__,
                    "message": "来源已删除、发生变化或不是有效正文；请核对归档来源"
                    if source_error
                    else "抽取未完成，请检查模型连接或结构化输出后重试",
                }
                logger.warning("Knowledge extraction job failed (%s)", type(exc).__name__)
            self._finish(job["id"], **changes)

    def _finish(self, identity, **changes):
        with advisory_lock(self.root / ".lock"):
            job = self._read(identity)
            job.update(**changes, updated_at=_now())
            _write(self._path(identity), job)

    def kick(self):
        if self.stopping:
            return
        if self.worker is not None and not self.worker.done():
            self._pending_kick = True
            return
        self._pending_kick = False
        self.worker = asyncio.create_task(self._run())
        self.worker.add_done_callback(self._worker_done)

    def _worker_done(self, task):
        if task.cancelled() or self.stopping:
            return
        if task.exception() is not None:
            logger.warning("Knowledge ingestion worker stopped unexpectedly")
            return
        # One additional kick closes the enqueue/empty-queue race. Do not spin
        # while another API process owns the execution lock or on a disk error.
        if self._pending_kick:
            self.kick()

    async def wait(self):
        if self.worker is not None:
            await self.worker

    async def stop(self):
        self.stopping = True
        if self.worker and not self.worker.done():
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass


_managers: dict[tuple[str, str], KnowledgeIngestionJobs] = {}


def manager(data_root, database=None):
    key = (str(Path(data_root).resolve()), str(Path(database or DEFAULT_DATABASE).resolve()))
    if key not in _managers or _managers[key].stopping:
        _managers[key] = KnowledgeIngestionJobs(data_root, database)
    return _managers[key]


async def schedule_for_session(session_id, workspace_root, data_root, *, engine, model, scope=""):
    # workspace_root is part of the hook contract; archived session associations
    # are read through their own ledger, never from a model-generated report.
    from api.research_ingestion import schedule_for_session as schedule_research

    return await schedule_research(
        session_id, workspace_root, data_root, engine=engine, model=model, scope=scope
    )


def resume_authorized_jobs(data_root):
    manager(data_root).resume()


async def stop_all_jobs():
    for jobs in list(_managers.values()):
        await jobs.stop()
