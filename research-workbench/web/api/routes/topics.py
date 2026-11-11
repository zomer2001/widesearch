"""Topic workspace API. Reads never schedule model work."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from api.deps import LOCAL_DATA_ROOT
from api.knowledge_database import DEFAULT_DATABASE
from api.topic_store import DEFAULT_TOPICS_DATABASE, TopicStore

router = APIRouter(prefix="/api/topics", tags=["topics"])
DATABASE_PATH = DEFAULT_TOPICS_DATABASE
KNOWLEDGE_DATABASE_PATH = DEFAULT_DATABASE


def _store():
    return TopicStore(LOCAL_DATA_ROOT, DATABASE_PATH, KNOWLEDGE_DATABASE_PATH)


def _error(exc):
    return HTTPException(404 if isinstance(exc, KeyError) else 422, str(exc).strip("'"))


class TopicCreate(BaseModel, extra="forbid"):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=2000)


class TopicMerge(BaseModel, extra="forbid"):
    canonical_id: str = Field(min_length=1, max_length=300)
    alias_id: str = Field(min_length=1, max_length=300)


class TopicClassification(BaseModel, extra="forbid"):
    cluster_ids: list[str] | None = Field(max_length=30)


class ResearchRequest(BaseModel, extra="forbid"):
    query: str = Field(min_length=1, max_length=12000)
    topic_id: str | None = Field(default=None, max_length=100)
    engine: Literal["openai", "deepseek", "ollama", "cli"] | None = None
    max_tasks: int = Field(default=4, ge=1, le=8)
    max_seconds: int = Field(default=1200, ge=60, le=7200)
    local_only: bool = False
    modules: list[Literal["adaptive_collection"]] = Field(default_factory=list)


@router.get("")
async def list_topics():
    topics = await asyncio.to_thread(_store().list_topics)
    return {"topics": topics, "default_topic_id": topics[0]["id"] if topics else None}


@router.post("")
async def create_topic(request: TopicCreate):
    try:
        return await asyncio.to_thread(_store().create_topic, request.title, request.description)
    except (KeyError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/research")
async def research(request: ResearchRequest):
    from api.topic_research import manager

    try:
        return await manager().start(**request.model_dump())
    except (KeyError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/runs/{run_id}/stop")
async def stop_run(run_id: str):
    from api.topic_research import manager

    try:
        return await manager().stop(run_id)
    except (KeyError, ValueError) as exc:
        raise _error(exc) from exc


@router.get("/{topic_id}")
async def topic_snapshot(topic_id: str):
    try:
        return await asyncio.to_thread(_store().snapshot, topic_id)
    except (KeyError, ValueError) as exc:
        raise _error(exc) from exc


@router.delete("/{topic_id}/entities/{entity_id}", status_code=204)
async def remove_topic_entity(topic_id: str, entity_id: str):
    try:
        await asyncio.to_thread(_store().remove_entity, topic_id, entity_id)
    except (KeyError, ValueError) as exc:
        raise _error(exc) from exc
    return Response(status_code=204)


@router.post("/{topic_id}/merges")
async def merge_topic_entities(topic_id: str, request: TopicMerge):
    from api.topic_resolution import merge_entities

    try:
        return await asyncio.to_thread(
            merge_entities, _store(), topic_id, request.canonical_id, request.alias_id
        )
    except (KeyError, ValueError) as exc:
        raise _error(exc) from exc


@router.patch("/{topic_id}/entities/{entity_id}/classification")
async def classify_topic_entity(topic_id: str, entity_id: str, request: TopicClassification):
    try:
        return await asyncio.to_thread(
            _store().update_classification, topic_id, entity_id, request.cluster_ids
        )
    except (KeyError, ValueError) as exc:
        raise _error(exc) from exc


@router.delete("/{topic_id}/merges/{alias_id}")
async def undo_topic_merge(topic_id: str, alias_id: str):
    from api.topic_resolution import undo_merge

    try:
        return await asyncio.to_thread(undo_merge, _store(), topic_id, alias_id)
    except (KeyError, ValueError) as exc:
        raise _error(exc) from exc
