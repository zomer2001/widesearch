"""Review-first handoff endpoints; all operations are local and model-free."""

import asyncio
import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .models import ResultEnvelope
from .service import HandoffService

router = APIRouter(prefix="/handoff")


def service():
    # The existing router owns the business source selection and configured paths.
    from api.routes import research_database as parent

    return HandoffService(parent.DATABASE_PATH, parent.LOCAL_DATA_ROOT, parent.available_documents)


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(default="anduril", pattern=r"^[a-z0-9][a-z0-9:_-]{0,99}$")
    result: ResultEnvelope
    entity_choices: dict[str, str] = Field(default_factory=dict, max_length=1000)


class CommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(default="anduril", pattern=r"^[a-z0-9][a-z0-9:_-]{0,99}$")
    plan_id: str = Field(pattern=r"^plan_[a-f0-9]{64}$")
    allow_partial: bool = False


async def local_call(function, *args, **kwargs):
    try:
        return await asyncio.to_thread(function, *args, **kwargs)
    except (ValueError, KeyError) as exc:
        message = str(exc)
        stale = any(
            word in message.lower() for word in ("版本", "过期", "变化", "stale", "changed")
        )
        raise HTTPException(409 if stale else 422, message) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, "原文或写入计划不存在") from exc
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(503, "本地资料或数据库暂时不可用；本次操作未完成") from exc


@router.get("/contract")
async def contract():
    from .contracts import build_contract

    result = await local_call(build_contract, service().store)
    return {**result, "result_schema": ResultEnvelope.model_json_schema(), "model_calls": 0}


@router.get("/status")
async def status(slug: str = "anduril"):
    return await local_call(service().status, slug)


@router.post("/preview")
async def preview(request: PreviewRequest):
    return await local_call(
        service().preview, request.slug, request.result, entity_choices=request.entity_choices
    )


@router.get("/plans/{plan_id}")
async def plan(plan_id: str, slug: str = "anduril"):
    return await local_call(service().get_plan, slug, plan_id)


@router.post("/commit")
async def commit(request: CommitRequest):
    return await local_call(
        service().commit, request.slug, request.plan_id, allow_partial=request.allow_partial
    )


@router.get("/documents/{document_id}")
async def document(document_id: str, slug: str = "anduril"):
    return await local_call(service().document, slug, document_id)


@router.post("/summaries")
async def summary(request: PreviewRequest):
    return await local_call(service().save_summary, request.slug, request.result)
