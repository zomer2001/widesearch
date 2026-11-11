"""Local persistent entity/relation knowledge API; no implicit model work."""

from __future__ import annotations

import asyncio
import csv
import io
import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator

from api.deps import LOCAL_DATA_ROOT, WORKSPACE_ROOT
from api.knowledge_database import DEFAULT_DATABASE, KnowledgeDatabase

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])
DATABASE_PATH = DEFAULT_DATABASE


def _database():
    return KnowledgeDatabase(LOCAL_DATA_ROOT, DATABASE_PATH)


def _error(exc):
    if isinstance(exc, KeyError):
        return HTTPException(404, str(exc).strip("'"))
    return HTTPException(422, str(exc))


EntityKind = Literal["product", "technology", "route", "reference"]
Predicate = Literal[
    "variant_of",
    "part_of",
    "replaces",
    "uses_technology",
    "has_route",
    "reference_for",
    "carries",
    "deploys",
    "integrates",
    "evolves_from",
]


class EntityCreate(BaseModel, extra="forbid"):
    kind: EntityKind
    name: str = Field(min_length=1, max_length=300)
    summary: str = Field(default="", max_length=20000)
    category: str = Field(default="", max_length=300)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError("实体名称不能为空")
        return value.strip()


class EntityPatch(BaseModel, extra="forbid"):
    name: str | None = Field(default=None, min_length=1, max_length=300)
    summary: str | None = Field(default=None, max_length=20000)
    category: str | None = Field(default=None, max_length=300)
    attributes: dict[str, Any] | None = None

    @field_validator("name", "summary", "category", "attributes")
    @classmethod
    def non_null_fields(cls, value):
        if value is None:
            raise ValueError("字段不能为 null")
        return value


class RelationCreate(BaseModel, extra="forbid"):
    source_id: str = Field(min_length=1, max_length=300)
    target_id: str = Field(min_length=1, max_length=300)
    predicate: Predicate


class FieldPatch(BaseModel, extra="forbid"):
    value: str | None = Field(max_length=12000)


class IngestionRequest(BaseModel, extra="forbid"):
    session_id: str = Field(default="", max_length=160)
    retry_failed: bool = False
    engine: Literal["openai", "deepseek", "ollama", "cli"] | None = None
    model: str | None = Field(default=None, min_length=1, max_length=200)
    scope: str = Field(default="", max_length=12000)
    document_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)


def _ingestion_jobs():
    from api.knowledge_ingestion_jobs import manager

    return manager(LOCAL_DATA_ROOT, DATABASE_PATH)


@router.get("/ingestion")
async def ingestion_status(session_id: str = Query(default="", max_length=160)):
    return await asyncio.to_thread(_ingestion_jobs().snapshot, session_id)


@router.post("/ingestion")
async def schedule_ingestion(request: IngestionRequest):
    from api import field_extraction_policy as policy

    if not policy.MODEL_EXTRACTION_ENABLED:
        raise HTTPException(503, policy.PAUSED_MESSAGE)
    from api import workbench

    # Selecting the configured provider is part of this explicit POST only.
    # GET and startup never load credentials to discover/extract old documents.
    config = workbench.load_config()
    engine = request.engine or config.engine
    model = request.model or getattr(config, engine).model
    if not model or not model.strip() or any(character.isspace() for character in model):
        raise HTTPException(422, "请先在设置中选择抽取模型")
    jobs = _ingestion_jobs()
    try:
        scheduled = await asyncio.to_thread(
            jobs.schedule_session,
            request.session_id,
            engine=engine,
            model=model,
            scope=request.scope,
            retry_failed=request.retry_failed,
            workspace_root=WORKSPACE_ROOT,
            document_ids=request.document_ids,
        )
    except (KeyError, ValueError) as exc:
        raise _error(exc) from exc
    jobs.kick()
    return {**await asyncio.to_thread(jobs.snapshot), **scheduled}


@router.post("/ingestion/{identity}/retry")
async def retry_ingestion(identity: str):
    from api import field_extraction_policy as policy

    if not policy.MODEL_EXTRACTION_ENABLED:
        raise HTTPException(503, policy.PAUSED_MESSAGE)
    jobs = _ingestion_jobs()
    try:
        await asyncio.to_thread(jobs.retry, identity)
    except FileNotFoundError as exc:
        raise HTTPException(404, "入库作业不存在") from exc
    except (KeyError, ValueError) as exc:
        raise _error(exc) from exc
    jobs.kick()
    return await asyncio.to_thread(jobs.snapshot)


@router.get("")
async def get_knowledge(
    query: str = Query(default="", max_length=300),
    kind: str = Query(default="", max_length=30),
):
    try:
        return await asyncio.to_thread(_database().snapshot, query, kind)
    except ValueError as exc:
        raise _error(exc) from exc


@router.post("/sync")
async def sync_knowledge():
    return await asyncio.to_thread(_database().sync)


@router.get("/export")
async def export_knowledge(format: Literal["json", "csv"] = "json"):
    data = await asyncio.to_thread(_database().snapshot)
    if format == "json":
        content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        return Response(
            content,
            media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="knowledge-database.json"',
            },
        )
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["实体ID", "类型", "名称", "类别", "说明", "来源状态", "属性", "证据ID"])
    for entity in data["entities"]:
        # Protect spreadsheet readers from formula interpretation of text cells.
        row = [
            entity["id"],
            entity["kind"],
            entity["name"],
            entity["category"],
            entity["summary"],
            entity["status"],
            json.dumps(entity["attributes"], ensure_ascii=False),
            ";".join(entity["evidence_ids"]),
        ]
        writer.writerow(
            [
                "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
                for value in row
            ]
        )
    return Response(
        "\ufeff" + stream.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="knowledge-entities.csv"',
        },
    )


@router.post("/entities", status_code=201)
async def create_entity(request: EntityCreate):
    try:
        return await asyncio.to_thread(_database().create_entity, request.model_dump())
    except ValueError as exc:
        raise _error(exc) from exc


@router.patch("/entities/{identity}")
async def update_entity(identity: str, request: EntityPatch):
    try:
        return await asyncio.to_thread(
            _database().update_entity, identity, request.model_dump(exclude_unset=True)
        )
    except (KeyError, ValueError) as exc:
        raise _error(exc) from exc


@router.delete("/entities/{identity}", status_code=204)
async def delete_entity(identity: str):
    try:
        await asyncio.to_thread(_database().delete_entity, identity)
    except KeyError as exc:
        raise _error(exc) from exc
    return Response(status_code=204)


@router.patch("/entities/{identity}/fields/{field}")
async def update_entity_field(identity: str, field: str, request: FieldPatch):
    try:
        return await asyncio.to_thread(_database().update_field, identity, field, request.value)
    except (KeyError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/relations", status_code=201)
async def create_relation(request: RelationCreate):
    try:
        return await asyncio.to_thread(_database().create_relation, request.model_dump())
    except (KeyError, ValueError) as exc:
        raise _error(exc) from exc


@router.delete("/relations/{identity}", status_code=204)
async def delete_relation(identity: str):
    try:
        await asyncio.to_thread(_database().delete_relation, identity)
    except KeyError as exc:
        raise _error(exc) from exc
    return Response(status_code=204)
