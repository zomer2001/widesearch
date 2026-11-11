"""The local-only orchestrator keeps a small surface without changing tool execution."""

import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from searchos.agents.orchestrator import get_orchestrator_tools
from searchos.agents.orchestrator.prompt import build_orchestrator_prompt
from searchos.harness.middleware.context.socm_middleware import SOCMContextMiddleware
from searchos.harness.middleware.models import TaskContext
from searchos.socm import SearchState


def test_local_tools_keep_other_modes_and_use_typed_structured_mutations():
    original = {tool.name: tool for tool in get_orchestrator_tools(source_mode="hybrid")}
    descriptions = {name: tool.description for name, tool in original.items()}
    local = get_orchestrator_tools(source_mode="local")
    assert {tool.name for tool in local} == {
        "create_schema", "enqueue_tasks", "check_agents", "read_evidence",
        "list_frontier", "inspect_table", "stop_task",
    }
    for tool in local:
        shared = original[tool.name]
        assert tool is not shared
        if tool.name not in {"create_schema", "enqueue_tasks", "stop_task"}:
            assert tool.coroutine is shared.coroutine
            assert tool.args_schema is shared.args_schema
            assert convert_to_openai_tool(tool)["function"]["parameters"] == (
                convert_to_openai_tool(shared)["function"]["parameters"]
            )
        else:
            parameters = convert_to_openai_tool(tool)["function"]["parameters"]
            assert not any(name.endswith("_json") for name in parameters["properties"])
            argument = {"create_schema": "tables", "enqueue_tasks": "items", "stop_task": "task_ids"}[tool.name]
            assert parameters["properties"][argument]["type"] == "array"
    for mode in ("", "web", "hybrid"):
        assert {tool.name: tool.description for tool in get_orchestrator_tools(source_mode=mode)} == descriptions
    serialized = json.dumps([convert_to_openai_tool(tool) for tool in local], ensure_ascii=False)
    assert len(serialized) < 5500


def test_local_prompt_is_bounded_and_keeps_source_and_pending_task_contract():
    sources = ("Current library: fixture; scope: selected documents.\nReadable documents: 2.\n"
               "- 2024-负责人公告.md (document_id=doc_2024)\n"
               "- 2025-负责人公告.md (document_id=doc_2025)")
    prompt = build_orchestrator_prompt(
        "unused-full-agent-catalog", "unused-skills", source_mode="local",
        orch_toolset="unused-duplicate-tool-schemas", local_sources=sources,
        current_time="2026-10-29", follow_up=True,
    )
    assert 2000 <= len(prompt) <= 3000
    assert sources in prompt
    assert "2026-10-29" in prompt
    assert "已有 pending 任务" in prompt
    assert "优先调用 check_agents" in prompt
    assert "目录无关键词匹配不等于原文不存在" in prompt
    assert "items 直接传对象数组" in prompt
    assert "不填写 agent_type 或 result_mode" in prompt
    assert "不要再包装成 JSON 字符串" in prompt
    assert "缺失日期保持未知" in prompt
    assert "evidence:事实ID" in prompt
    assert "已有任务的续问" in prompt
    assert not any(token in prompt for token in (
        "unused-", "explore_agent", "python", "edit_entities", "add_entities", "add_columns",
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["local", "web", "hybrid"])
async def test_actual_socm_injection_matches_local_tool_availability(mode):
    state = SearchState(source_scope={"mode": mode}, research_mode="findings")
    component = SOCMContextMiddleware(
        SimpleNamespace(load_state=lambda: state), task=TaskContext(agent_id="orchestrator"),
        view="overview",
    )

    @dataclass
    class Request:
        messages: list
        system_message: object = None

    captured = []

    async def model(request):
        captured.append(request.messages[-1])
        return AIMessage(content="Ready")

    await component.awrap_model_call(Request([HumanMessage(content="Compare the notices")]), model)
    message = captured[0]
    assert message.additional_kwargs["sf_synthetic"] is True
    if mode == "local":
        assert "pending/running" in message.content
        assert "read_evidence" in message.content
        assert "python" not in message.content
        assert "edit_entities" not in message.content
    else:
        assert "python and edit_entities" in message.content


@pytest.mark.asyncio
async def test_typed_wrappers_delegate_exactly_once_and_preserve_quotes(monkeypatch):
    from unittest.mock import AsyncMock

    from searchos.tools import schema, tasks

    create = AsyncMock(return_value="created")
    enqueue = AsyncMock(return_value="queued")
    stop = AsyncMock(return_value="stopped")
    monkeypatch.setattr(schema.create_schema, "coroutine", create)
    monkeypatch.setattr(tasks.enqueue_tasks, "coroutine", enqueue)
    monkeypatch.setattr(tasks.stop_task, "coroutine", stop)
    local = {tool.name: tool for tool in get_orchestrator_tools(source_mode="local")}
    table = {"table_id": "appointments", "attributes": ["Name", "Leader"],
             "primary_key": ["Name"], "seed_entities": ['Institute "A"']}
    assert await local["create_schema"].ainvoke({"tables": [table]}) == "created"
    create.assert_awaited_once()
    encoded_table, = json.loads(create.call_args.kwargs["tables_json"])
    assert encoded_table["seed_entities"] == ['Institute "A"']
    assert encoded_table["primary_key"] == ["Name"]

    question = 'Compare "Alice" and Bob; preserve the literal closing brace }.\nRead 2025.'
    assert await local["enqueue_tasks"].ainvoke({"items": [{"task": question}]}) == "queued"
    enqueue.assert_awaited_once()
    item, = json.loads(enqueue.call_args.kwargs["items_json"])
    assert item["task"] == question
    assert item["agent_type"] == "local_search_agent"
    assert item["result_mode"] == "findings"
    assert item["priority"] == 0.5 and item["target_cells"] == []
    assert await local["stop_task"].ainvoke({"task_ids": ['task-"quoted"'], "reason": "Duplicate"}) == "stopped"
    stop.assert_awaited_once()
    assert json.loads(stop.call_args.kwargs["task_ids_json"]) == ['task-"quoted"']
    assert stop.call_args.kwargs["reason"] == "Duplicate"


@pytest.mark.asyncio
async def test_typed_local_tools_reach_original_schema_scope_and_version_guards(monkeypatch, tmp_path):
    from searchos.agents import runtime
    from searchos.config.run_config import EffectiveRunConfig, activate_run_config
    from searchos.harness.p1_runtime import begin_run, execution_context
    from searchos.socm.revisions import StaleExecutionError
    from searchos.socm.workspace import WorkspaceManager
    from searchos.tools import tasks
    from searchos.tools.search_state import _source_scope_var

    workspace = WorkspaceManager(tmp_path, "typed-local")
    workspace.create()
    state = SearchState(research_mode="findings", module_config={
        "schema_version": 1, "implementation_version": "1", "enabled": ["evidence_revision"],
    }, source_scope={
        "mode": "local", "documents": [{"document_id": "selected"}],
    })
    workspace.save_state(state)
    runtime.set_orchestrator_context(workspace=workspace, model=object())

    async def tick():
        return {}

    monkeypatch.setattr(tasks, "_scheduler", lambda: SimpleNamespace(tick=tick, allow_tasks=lambda _: None))
    local = {tool.name: tool for tool in get_orchestrator_tools(source_mode="local")}
    config = EffectiveRunConfig.from_settings().with_modules({"enabled": ["evidence_revision"]})
    source_token = _source_scope_var.set(state.source_scope)
    try:
        with activate_run_config(config):
            ticket = begin_run(workspace, "__answer__", "typed-run")
            with execution_context(ticket):
                result = await local["create_schema"].ainvoke({"tables": [{
                    "table_id": "leaders", "attributes": ["Name", "Leader"], "primary_key": ["Name"],
                }]})
                assert "Error:" not in result
                assert "leaders" in workspace.load_state().coverage_map.tables
                invalid = await local["create_schema"].ainvoke({"tables": [{
                    "table_id": "invalid", "attributes": ["Name"], "primary_key": ["unknown"],
                }]})
                assert "Error:" in invalid
                assert "invalid" not in workspace.load_state().coverage_map.tables
                outcome = json.loads(await local["enqueue_tasks"].ainvoke({"items": [{
                    "task": "Read the original appointment notice", "target_table": "leaders",
                }]}))
                assert len(outcome["queued"]) == 1
                task = workspace.load_state().frontier.questions[0]
                assert task.agent_type == "local_search_agent" and task.result_mode == "findings"
                await local["stop_task"].ainvoke({"task_ids": outcome["queued"], "reason": "No longer needed"})
                assert workspace.load_state().frontier.questions[0].status.value == "cancelled"

                # A retired execution cannot mutate tasks through the adapter.
                def retire(current):
                    current.p1.executions[ticket].invalidated = True
                    return current

                workspace.atomic_update_state(retire)
                with pytest.raises(StaleExecutionError):
                    await local["enqueue_tasks"].ainvoke({"items": [{"task": "Stale attempt"}]})

        # A Local adapter is no privilege grant when used outside a local scope.
        _source_scope_var.set({"mode": "web"})
        rejected = json.loads(await local["enqueue_tasks"].ainvoke({"items": [{"task": "Outside scope"}]}))
        assert not rejected["queued"]
        assert "local_search_agent disabled" in rejected["rejected"][0]["reason"]
    finally:
        _source_scope_var.reset(source_token)


@pytest.mark.asyncio
@pytest.mark.parametrize("argument", [{"items_json": "[]"}, {"items": "[]"},
                                      {"items": [{"task": "Read", "agent_type": "search_agent"}]}])
async def test_typed_local_dispatch_rejects_double_json_and_role_override(argument):
    from pydantic import ValidationError

    dispatch = next(tool for tool in get_orchestrator_tools(source_mode="local") if tool.name == "enqueue_tasks")
    with pytest.raises(ValidationError):
        await dispatch.ainvoke(argument)
