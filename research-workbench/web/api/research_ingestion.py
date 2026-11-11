"""Durable research extraction jobs. GETs never schedule documents or model calls."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from api.knowledge_ingestion_jobs import STATUSES, KnowledgeIngestionJobs, _now, _write
from api.research_mapping import HANDOFF, VERSION, key
from searchos.knowledge_store.database import DEFAULT_DATABASE, ResearchDatabase


class ResearchIngestionJobs(KnowledgeIngestionJobs):
    prompt_version = VERSION
    model_sensitive_jobs = True

    def __init__(self, data_root, database=DEFAULT_DATABASE, **kwargs):
        kwargs.setdefault("root", Path(database).parent / "research_ingestion")
        super().__init__(data_root, database, **kwargs)

    def snapshot(self, session_id="", scope=""):
        from api.field_extraction_policy import runtime
        from api.knowledge_extraction import scope_namespace
        from api.knowledge_ingestion_jobs import _PUBLIC_FIELDS

        raw = [
            job
            for job in self._list()
            if (not session_id or session_id in job.get("session_ids", [job["session_id"]]))
            and (not scope or scope_namespace(job.get("scope", "")) == scope)
        ]
        result = {
            "enabled": runtime()["model_enabled"],
            "runtime": runtime(),
            "total_jobs": len(raw),
            "jobs": [],
            "counts": {status: sum(j["status"] == status for j in raw) for status in STATUSES},
        }
        for saved in reversed(raw[-200:]):
            job = {field: saved[field] for field in _PUBLIC_FIELDS if field in saved}
            receipt = saved.get("result") or {}
            job.update(
                fields=receipt.get("counts", {}).get("fields", 0),
                unmapped_count=len(receipt.get("unmapped", [])),
                mode=saved.get("mode", "model"),
                chunks_done=saved.get("chunks_done", 0),
                chunks_total=saved.get("chunks_total", 0),
                scope=saved.get("scope", ""),
            )
            result["jobs"].append(job)
        result["scope_hint"] = "归档原文按字段抽取入库；已有抽取结果可以离线导入。"
        return result

    def schedule_existing(self):
        manifest = json.loads((HANDOFF / "manifest.json").read_text())
        scheduled = reused = 0
        for source in manifest["sources"]:
            sid = source["source_id"]
            if source["status"] != "ready" or not (HANDOFF / "outputs" / f"{sid}.json").is_file():
                continue
            payload_hash = key(json.loads((HANDOFF / "outputs" / f"{sid}.json").read_text()))
            extra = HANDOFF / "outputs-unmapped" / f"{sid}.json"
            extra_hash = key(json.loads(extra.read_text())) if extra.exists() else ""
            identity = "ingest_" + key("existing", sid, payload_hash, extra_hash, VERSION)
            if self._path(identity).exists():
                reused += 1
                continue
            timestamp = _now()
            _write(
                self._path(identity),
                dict(
                    id=identity,
                    session_id="existing-anduril",
                    session_ids=["existing-anduril"],
                    library_id=source.get("library_id", ""),
                    document_id=source.get("document_id", ""),
                    source_id=sid,
                    title=source["title"],
                    status="queued",
                    message="等待导入已有字段结果",
                    engine="offline",
                    model="已有抽取结果",
                    scope="namespace: anduril",
                    mode="existing",
                    created_at=timestamp,
                    updated_at=timestamp,
                    entities=0,
                    relations=0,
                    model_calls=0,
                    attempt=1,
                    cache_epoch=1,
                    authorized=True,
                ),
            )
            scheduled += 1
        return {"scheduled": scheduled, "reused": reused}

    def retry(self, identity):
        job = self._read(identity)
        if not self._can_execute(job):
            from api.field_extraction_policy import PAUSED_MESSAGE

            raise ValueError(PAUSED_MESSAGE)
        if job["status"] not in {"failed", "partial"}:
            raise ValueError("只有失败或部分完成的任务可以重试")
        job.update(
            status="queued",
            error="",
            message="等待重试；已完成分块使用缓存",
            attempt=job.get("attempt", 1) + 1,
            updated_at=_now(),
        )
        _write(self._path(identity), job)

    def record_structured(self, document, receipt, *, library_id, slug):
        identity = "ingest_" + key("structured", receipt["run_id"])
        previous = self._read(identity) if self._path(identity).exists() else {}
        _write(
            self._path(identity),
            {
                "id": identity,
                "session_id": "manual:" + slug,
                "session_ids": ["manual:" + slug],
                "library_id": library_id,
                "document_id": document["document_id"],
                "source_revision": document["source_revision"],
                "title": document["name"],
                "status": receipt["status"],
                "message": "结构化字段结果已入库；未调用模型",
                "engine": "offline",
                "model": "结构化字段结果",
                "mode": "structured",
                "scope": "namespace: " + slug,
                "created_at": previous.get("created_at", _now()),
                "updated_at": _now(),
                "entities": receipt["counts"].get("entities", 0),
                "relations": receipt["counts"].get("relations", 0),
                "model_calls": 0,
                "result": receipt,
                "attempt": 1,
            },
        )
        return identity

    def _can_execute(self, job):
        return job.get("mode") == "existing" or super()._can_execute(job)

    def _execute(self, job):
        from api import field_extraction_policy as policy
        from api import research_extraction as extraction

        if not self._can_execute(job):
            raise ValueError(policy.PAUSED_MESSAGE)
        store = ResearchDatabase(self.database)
        store.initialize()
        if job.get("mode") == "existing":
            result = extraction.import_existing(job["source_id"], database=self.database)
            _write(self.root / "receipts" / (job["id"] + ".json"), result)
            return result
        from api.knowledge_extraction_model import make_extractor

        factory = self.extractor_factory
        if not factory and (job["engine"], job["model"]) != (
            policy.EXTRACTION_ENGINE,
            policy.EXTRACTION_MODEL,
        ):
            raise ValueError("该任务使用旧模型配置；请按 6-luna 低思考重新创建任务。")
        extractor = (
            factory(job["engine"], job["model"])
            if factory
            else make_extractor(
                job["engine"],
                job["model"],
                system_prompt=extraction.PROMPT,
                output_schema=extraction.DocumentExtraction.model_json_schema(),
                decoder=extraction.decode,
                reasoning_effort=policy.EXTRACTION_REASONING,
            )
        )
        actual_calls = 0
        cache = self.root / "responses" / job["id"]

        def cached(payload):
            nonlocal actual_calls
            path = cache / (key(payload) + ".json")
            if path.exists():
                saved = json.loads(path.read_text())
                try:
                    extraction.validate_window(payload, saved)
                    return saved
                except ValueError:
                    pass
            actual_calls += 1
            self._finish(job["id"], model_calls=actual_calls)
            result = extractor(payload)
            _write(path, result)
            return result

        def progress(done, total):
            self._finish(
                job["id"],
                chunks_done=done,
                chunks_total=total,
                message=f"已读取 {done}/{total} 个正文分块，正在映射字段",
            )

        result = extraction.extract_document(
            self.data_root,
            job["library_id"],
            job["document_id"],
            job["source_revision"],
            extractor=cached,
            database=self.database,
            scope=job["scope"],
            on_progress=progress,
            artifact_root=self.root / "artifacts" / job["id"],
            model_version=job["engine"] + ":" + job["model"],
        )
        result["model_calls"] = actual_calls
        if self.database.resolve() == Path(DEFAULT_DATABASE).resolve():
            from api.knowledge_database import DEFAULT_DATABASE as LEGACY_DATABASE
            from api.research_mapping import library_source

            source = library_source(
                self.data_root, job["library_id"], job["document_id"], job["source_revision"]
            )
            try:
                result["legacy_projection"] = extraction.project_legacy(
                    self.data_root, source, result, store, LEGACY_DATABASE
                )
            except Exception as exc:
                # The structured store remains authoritative if the old view cannot refresh.
                result["legacy_projection_error"] = type(exc).__name__
        _write(self.root / "receipts" / (job["id"] + ".json"), result)
        return result


_managers = {}


def manager(data_root, database=DEFAULT_DATABASE):
    identity = (str(Path(data_root).resolve()), str(Path(database).resolve()))
    if identity not in _managers or _managers[identity].stopping:
        _managers[identity] = ResearchIngestionJobs(data_root, database)
    return _managers[identity]


async def schedule_for_session(session_id, workspace_root, data_root, *, engine, model, scope=""):
    from api import field_extraction_policy as policy

    if not policy.MODEL_EXTRACTION_ENABLED:
        return {"scheduled": 0, "reused": 0, "paused": True, "message": policy.PAUSED_MESSAGE}
    jobs = manager(data_root)
    result = await asyncio.to_thread(
        jobs.schedule_session,
        session_id,
        engine=policy.EXTRACTION_ENGINE,
        model=policy.EXTRACTION_MODEL,
        scope=scope,
        workspace_root=workspace_root,
    )
    jobs.kick()
    return result


async def stop_all_jobs():
    for jobs in list(_managers.values()):
        await jobs.stop()
