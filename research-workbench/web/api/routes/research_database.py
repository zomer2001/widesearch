"""Field database API: source selection, extraction receipts and evidence views."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api import field_extraction_policy as policy
from api.deps import LOCAL_DATA_ROOT
from api.knowledge_handoff.routes import router as handoff_router
from api.research_ingestion import manager
from api.research_mapping import VERSION, DocumentExtraction
from searchos.knowledge_store.database import DEFAULT_DATABASE, ResearchDatabase

router = APIRouter(prefix="/api/research-database", tags=["research-database"])
DATABASE_PATH = DEFAULT_DATABASE

# The v2 importer separates preview from explicit commit. Graph APIs are unchanged.
router.include_router(handoff_router)


def _store():
    return ResearchDatabase(DATABASE_PATH)


def _jobs():
    return manager(LOCAL_DATA_ROOT, DATABASE_PATH)


def available_documents(slug):
    from api.research_materials import research_materials

    data = research_materials(LOCAL_DATA_ROOT)
    documents = data["documents"]
    from api.topic_store import DEFAULT_TOPICS_DATABASE

    ids = set()
    path = Path(DEFAULT_TOPICS_DATABASE)
    if path.exists():
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            ids = {
                r[0]
                for r in db.execute(
                    "SELECT d.document_id FROM topic_documents d "
                    "JOIN topics t ON t.id=d.topic_id WHERE t.namespace=?",
                    (slug,),
                )
            }
    documents = [d for d in documents if d["document_id"] in ids]
    return {
        "library_id": data["library_id"],
        "documents": [
            {
                "document_id": d["document_id"],
                "name": (d.get("origin") or {}).get("title") or d["name"],
                "source_revision": d.get("source_revision", ""),
                "parse_status": d.get("parse_status", ""),
                "searchable": d.get("searchable", False),
                "url": (d.get("origin") or {}).get("final_url")
                or (d.get("origin") or {}).get("url", ""),
            }
            for d in documents
        ],
    }


class ExtractRequest(BaseModel, extra="forbid"):
    slug: str = Field(default="anduril", pattern=r"^[a-z0-9][a-z0-9:_-]{0,99}$")
    document_ids: list[str] = Field(min_length=1, max_length=100)
    engine: Literal["cli"] = "cli"
    model: Literal["gpt-6-luna"] = "gpt-6-luna"
    reasoning_effort: Literal["low"] = "low"
    retry_failed: bool = False


class StructuredRequest(BaseModel, extra="forbid"):
    slug: str = Field(default="anduril", pattern=r"^[a-z0-9][a-z0-9:_-]{0,99}$")
    document_id: str = Field(min_length=1, max_length=100)
    source_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    outputs: list[DocumentExtraction] = Field(min_length=1, max_length=500)


@router.get("/contract")
async def get_contract():
    def read():
        with _store().connect() as db:
            fields = [
                dict(r)
                for r in db.execute(
                    "SELECT f.id,f.label,f.subject_kind,f.value_type,f.repeat_group,m.element_id "
                    "FROM field_definitions f JOIN field_storage_mappings m ON m.field_id=f.id "
                    "ORDER BY f.id"
                )
            ]
        return {
            "schema_version": VERSION,
            "runtime": policy.runtime(),
            "output_schema": DocumentExtraction.model_json_schema(),
            "fields": fields,
        }

    return await asyncio.to_thread(read)


@router.post("/map")
async def map_structured(request: StructuredRequest):
    from api.research_extraction import ingest_structured

    available = await asyncio.to_thread(available_documents, request.slug)
    document = next(
        (d for d in available["documents"] if d["document_id"] == request.document_id), None
    )
    if document is None:
        raise HTTPException(422, "只能提交当前专题已归档原文的字段结果")
    if document["source_revision"] != request.source_revision:
        raise HTTPException(409, "归档原文版本已变化，请重新选择原文")
    try:
        receipt = await asyncio.to_thread(
            ingest_structured,
            LOCAL_DATA_ROOT,
            available["library_id"],
            request.document_id,
            request.source_revision,
            [o.model_dump() for o in request.outputs],
            database=DATABASE_PATH,
            slug=request.slug,
        )
        receipt["job_id"] = await asyncio.to_thread(
            _jobs().record_structured,
            document,
            receipt,
            library_id=available["library_id"],
            slug=request.slug,
        )
        return receipt
    except (ValueError, KeyError, FileNotFoundError) as exc:
        raise HTTPException(422, "字段格式、引句或原文版本未通过校验；本次未发布字段") from exc


@router.get("")
async def get_database(
    slug: str = "anduril",
    query: str = Query(default="", max_length=300),
    dimension: int = Query(default=0, ge=0, le=7),
    entity_id: str = "",
    limit: int = Query(default=150, ge=1, le=500),
):
    from api.research_database import snapshot

    try:
        return await asyncio.to_thread(snapshot, _store(), slug, query, dimension, entity_id, limit)
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(503, "字段数据库暂时不可用，请检查数据库文件") from exc


@router.get("/documents")
async def get_documents(slug: str = "anduril"):
    return await asyncio.to_thread(available_documents, slug)


@router.get("/assets")
async def get_assets(
    slug: str = "anduril",
    kind: Literal["product", "technology"] = "product",
    query: str = Query(default="", max_length=200),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=24, ge=1, le=100),
):
    from api.research_assets import asset_index

    return await asyncio.to_thread(asset_index, _store(), slug, kind, query, offset, limit)


@router.get("/asset-summary")
async def get_asset_summary():
    from api.research_assets import asset_summary

    return await asyncio.to_thread(asset_summary, _store())


@router.get("/assets/{entity_id}")
async def get_asset(entity_id: str, slug: str = "anduril"):
    from api.research_assets import asset_detail

    try:
        return await asyncio.to_thread(asset_detail, _store(), slug, entity_id)
    except KeyError as exc:
        raise HTTPException(404, "当前专题没有这个对象") from exc


@router.get("/assets/{entity_id}/graph")
async def get_asset_graph(
    entity_id: str,
    slug: str = "anduril",
    mode: Literal["overview", "lineage", "technology", "applications", "capabilities"] = "overview",
    depth: int = Query(default=1, ge=1, le=3),
    limit: int = Query(default=50, ge=2, le=500),
    scope: Literal["asset", "topic"] = "asset",
):
    from api.research_assets import asset_graph

    try:
        return await asyncio.to_thread(
            asset_graph, _store(), slug, entity_id, mode, depth, limit, scope
        )
    except KeyError as exc:
        raise HTTPException(404, "当前专题没有这个产品或技术") from exc


@router.get("/materials")
async def get_materials(slug: str = "anduril"):
    from api.research_assets import material_inventory

    available = await asyncio.to_thread(available_documents, slug)
    return await asyncio.to_thread(material_inventory, _store(), slug, available["documents"])


@router.get("/materials/{document_id}")
async def get_material(document_id: str, slug: str = "anduril"):
    from api.research_mapping import library_source

    available = await asyncio.to_thread(available_documents, slug)
    document = next((d for d in available["documents"] if d["document_id"] == document_id), None)
    if document is None:
        raise HTTPException(404, "原文不属于当前专题")
    try:
        source = await asyncio.to_thread(
            library_source,
            LOCAL_DATA_ROOT,
            available["library_id"],
            document_id,
            document["source_revision"],
        )
        return {**document, "text_content": source.text}
    except (ValueError, KeyError, FileNotFoundError) as exc:
        raise HTTPException(422, "当前原文不可用，请检查归档或解析状态") from exc


@router.get("/jobs")
async def get_jobs(slug: str = "anduril"):
    return await asyncio.to_thread(_jobs().snapshot, scope=slug)


@router.post("/extract")
async def extract(request: ExtractRequest):
    if not policy.MODEL_EXTRACTION_ENABLED:
        raise HTTPException(503, policy.PAUSED_MESSAGE)
    available = await asyncio.to_thread(available_documents, request.slug)
    selected = set(request.document_ids)
    documents = [d for d in available["documents"] if d["document_id"] in selected]
    if len(documents) != len(selected):
        raise HTTPException(422, "只能选择当前专题已归档的原文")
    jobs = _jobs()
    result = await asyncio.to_thread(
        jobs.enqueue_documents,
        documents,
        session_id="manual:" + request.slug,
        library_id=available["library_id"],
        engine=policy.EXTRACTION_ENGINE,
        model=policy.EXTRACTION_MODEL,
        scope="namespace: " + request.slug,
        retry_failed=request.retry_failed,
    )
    jobs.kick()
    return result


@router.post("/import-existing")
async def import_existing():
    jobs = _jobs()
    result = await asyncio.to_thread(jobs.schedule_existing)
    jobs.kick()
    return result


@router.post("/jobs/{job_id}/retry")
async def retry(job_id: str):
    try:
        await asyncio.to_thread(_jobs().retry, job_id)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(422, "该任务不存在或不能重试") from exc
    _jobs().kick()
    return {"queued": True}


@router.get("/jobs/{job_id}")
async def job_detail(job_id: str):
    try:
        job = await asyncio.to_thread(_jobs()._read, job_id)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, "处理任务不存在") from exc
    return {
        "id": job_id,
        "title": job.get("title"),
        "status": job["status"],
        "result": job.get("result"),
        "error": job.get("error", ""),
    }


@router.get("/sources/{revision_id}")
async def get_source(revision_id: str):
    from api.research_database import source_text

    try:
        return await asyncio.to_thread(source_text, _store(), revision_id)
    except KeyError as exc:
        raise HTTPException(404, "原文不存在") from exc
