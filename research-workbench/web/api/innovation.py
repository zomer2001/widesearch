"""Versioned, immutable innovation selection for one research workspace."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from searchos.config.run_config import normalize_module_config

ModuleId = Literal["evidence_revision", "context_memory", "adaptive_collection"]
MANIFEST_NAME = ".workbench-modules.json"
MODULES = (
    {
        "id": "adaptive_collection",
        "label": "自适应采集",
        "description": "依据目标缺口、资料关联和新增信息调整搜索与阅读顺序。",
        "implementation_version": "1",
        "default_enabled": False,
    },
    {
        "id": "evidence_revision",
        "label": "证据适用性与修订",
        "description": "核验事实口径与有效期，追踪证据修正对结论和任务的影响。",
        "implementation_version": "1",
        "default_enabled": False,
    },
    {
        "id": "context_memory",
        "label": "目标与采集经验",
        "description": "保留任务目标和可验证的检索经验，并在需要时压缩上下文。",
        "implementation_version": "1",
        "default_enabled": False,
    },
)


class ModuleConfig(BaseModel, extra="forbid"):
    schema_version: Literal[1] = 1
    enabled: list[ModuleId] = Field(default_factory=list)
    implementation_version: Literal["1"] = "1"


def normalize(config: dict | ModuleConfig | None = None) -> dict:
    try:
        value = (
            config
            if isinstance(config, ModuleConfig)
            else ModuleConfig.model_validate(config or {})
        )
    except (ValueError, TypeError):
        raise HTTPException(
            409,
            "任务的创新模块版本或清单无法识别，请保留该任务并新建研究",
        ) from None
    return normalize_module_config(value.model_dump())


def read_manifest(workspace: Path, state_config: dict | None = None) -> dict:
    path = workspace / MANIFEST_NAME
    if not path.exists():
        return normalize(state_config)
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(409, "任务的创新模块清单无法读取，请保留该任务并新建研究") from None
    config = normalize(saved)
    if state_config is not None and normalize(state_config) != config:
        raise HTTPException(409, "任务状态与创新模块清单不一致，请新建研究")
    return config


def resolve_modules(
    requested: list[ModuleId] | None,
    *,
    workspace: Path | None = None,
    state_config: dict | None = None,
) -> dict:
    prior = read_manifest(workspace, state_config) if workspace is not None else None
    selected = normalize({"enabled": requested}) if requested is not None else prior or normalize()
    if prior is not None and prior != selected:
        raise HTTPException(409, "当前任务已固定创新模块；更改模块组合请新建研究")
    return selected


def write_manifest(workspace: Path, config: dict) -> dict:
    """Publish once atomically; never silently replace another run's selection."""
    config = normalize(config)
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / MANIFEST_NAME
    if path.exists():
        if read_manifest(workspace) != config:
            raise HTTPException(409, "当前任务已固定创新模块；更改模块组合请新建研究")
        return config
    fd, temporary = tempfile.mkstemp(prefix=".modules-", dir=workspace)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(config, output, ensure_ascii=False, indent=2)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if read_manifest(workspace) != config:
                raise HTTPException(409, "当前任务已固定创新模块；更改模块组合请新建研究") from None
    finally:
        os.unlink(temporary)
    return config
