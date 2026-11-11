"""Orchestrator (paper §Agent Roles) — the Main Agent.

Assembles the orchestrator's tool bundle from the @tool surfaces in
``searchos.tools`` (schema editors + Frontier queue / scheduler). The spawn
lifecycle, scheduler, prompt, catalog, and post-mortem live in sibling modules.
"""

from __future__ import annotations

import json

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field


class _LocalTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1)
    target_table: str = ""
    target_cells: list[str] = Field(default_factory=list)
    blocked_by: list[str] = Field(default_factory=list)
    priority: float = Field(default=0.5, ge=0, le=1)


class _LocalTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_id: str = Field(min_length=1)
    attributes: list[str] = Field(min_length=1)
    primary_key: list[str] = Field(default_factory=list)
    table_label: str = ""
    seed_entities: list[str | list[str]] = Field(default_factory=list)


def _assert_local_execution() -> None:
    from searchos.agents.runtime import _ctx
    from searchos.harness.p1_runtime import assert_current

    assert_current(_ctx.workspace)


@tool("enqueue_tasks")
async def _enqueue_local_tasks(items: list[_LocalTask]) -> str:
    """Dispatch typed local-reading requests through the existing guarded scheduler."""
    from searchos.tools.tasks import enqueue_tasks

    _assert_local_execution()
    payload = [{**item.model_dump(), "agent_type": "local_search_agent",
                "result_mode": "findings"} for item in items]
    return await enqueue_tasks.coroutine(items_json=json.dumps(payload, ensure_ascii=False))


@tool("create_schema")
async def _create_local_schema(tables: list[_LocalTable]) -> str:
    """Create typed local comparison tables using the original schema validator."""
    from searchos.tools.schema import create_schema

    _assert_local_execution()
    return await create_schema.coroutine(
        tables_json=json.dumps([table.model_dump() for table in tables], ensure_ascii=False),
    )


@tool("stop_task")
async def _stop_local_tasks(task_ids: list[str], reason: str = "") -> str:
    """Stop identified tasks through the original scope and execution guards."""
    from searchos.tools.tasks import stop_task

    _assert_local_execution()
    return await stop_task.coroutine(task_ids_json=json.dumps(task_ids, ensure_ascii=False), reason=reason)


_LOCAL_TOOL_DESCRIPTIONS = {
    "create_schema": (
        "按需要建立新的比较表；已有表无需重建。tables 直接传对象数组，"
        "每项必需 table_id、attributes（列名数组），可选 primary_key（主键列名数组）、"
        "table_label、seed_entities。"
        "单主键的 seed_entities 为名称数组，多主键为按主键顺序的值数组。"
        "只把用户指定或原文支持的对象作为种子，不填写猜测的属性值。"
        "证据提取负责填表。数组和对象直接作为工具参数传入，不转成字符串。"
    ),
    "enqueue_tasks": (
        "仅为尚未覆盖的资料阅读需求新增任务；已有 pending/running 任务先用 check_agents。"
        "items 直接传对象数组，每项必需 task（完整阅读问题）；"
        "可填 priority（0到1）、target_table（已存在的表ID）、target_cells（entity.attribute数组）、"
        "blocked_by（真正前置任务ID数组）。执行器固定为本地阅读，输出固定为原文事实，"
        "无需提供 agent_type 或 result_mode。"
        "task 中给出资料目录路径、需要核对的事实/日期和完成条件。"
        "返回 queued/deduped/rejected/errors；新增任务会自动按并发限制调度。"
    ),
    "check_agents": (
        "推动已有待执行任务、等待运行中的任务，并收集结果。通常省略 timeout 使用默认等待；"
        "不要短间隔反复轮询。grounded_findings 是已提交的原文事实，"
        "agent_report 仅为执行说明。尚有运行任务时继续处理其结果，不宣称资料不存在。"
    ),
    "read_evidence": (
        "读取当前可用证据及原文摘录、来源和 citation。可按 task_id、cell、entity、attribute "
        "或 evidence_ids（逗号分隔ID）筛选；组合筛选取交集。通常保持 include_inactive=False。"
        "最终回答使用返回的 evidence:ID 引用；不存在的记录不可作为事实。"
    ),
    "list_frontier": (
        "查看已有任务及状态，避免重复分配。status 可选 pending/running/completed/blocked/cancelled；"
        "kind 可选 search/write/explore；空值表示全部，limit 默认50。执行完成不等于事实已获支持。"
    ),
    "inspect_table": (
        "查看已有比较表和事实缺口。table_id 为空表示默认表，status 默认all，with_values 默认True。"
        "表格值仍须用 read_evidence 核对出处；本工具不修改表格。"
    ),
    "stop_task": (
        "停止已重复、过时或超出用户范围的任务。task_ids 直接传任务ID字符串数组，"
        "reason 简述原因；使用任务ID而不是执行器名称。"
    ),
}


def get_orchestrator_tools(*, source_mode: str = "") -> list:
    """Orchestrator tools: schema editors + Frontier queue + scheduler.

    Skill / browser tools belong to sub-agents only — exposing them here lets
    a confused orchestrator hop across them at budget exhaustion.
    """
    from searchos.tools.python_execution import python
    from searchos.tools.schema import (
        add_columns,
        add_entities,
        create_schema,
        edit_entities,
        inspect_table,
        remove_entities,
    )
    from searchos.tools.socm_read import list_frontier, read_evidence
    from searchos.tools.tasks import check_agents, enqueue_tasks, stop_task

    if source_mode == "local":
        # Typed wrappers serialize once inside Python, then use the original
        # scope/version validators. Shared Web/Hybrid tool objects stay intact.
        return [tool.model_copy(update={"description": _LOCAL_TOOL_DESCRIPTIONS[tool.name]})
                for tool in (_create_local_schema, _enqueue_local_tasks, check_agents, read_evidence,
                             list_frontier, inspect_table, _stop_local_tasks)]

    # Extraction owns extraction, evidence binding and cell projection.
    return [
        create_schema,
        add_columns,
        add_entities,
        remove_entities,
        edit_entities,
        inspect_table,
        python,
        enqueue_tasks,
        stop_task,
        check_agents,
        read_evidence,
        list_frontier,
    ]
