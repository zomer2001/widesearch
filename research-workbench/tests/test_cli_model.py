"""Protocol, accounting, isolation, and cancellation checks for the CLI bridge."""

import asyncio
import json
import os
import sys

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from searchos.config.cli_model import (
    CodexCLIChatModel,
    CodexCLIError,
    _action_schema,
    _assert_no_native_tools,
    _parse_action,
    _serialize_messages,
)
from searchos.util.token_tracker import TokenTrackingCallback, start_tracking

TOOL = {
    "type": "function",
    "function": {
        "name": "lookup",
        "description": "Read a fixture fact.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    executable = tmp_path / "fake-codex"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys, time
if "debug" in sys.argv:
    native = [{"tools": [{"type": "function", "name": "exec"}]}]
    print(json.dumps(native if os.getenv("FAKE_NATIVE") else []))
    sys.exit(0)
payload=json.load(sys.stdin)
if os.getenv("FAKE_TRANSIENT"):
    print(json.dumps({"type":"error","message":"Reconnecting after transient network error"}))
if os.getenv("FAKE_PID"):
    open(os.environ["FAKE_PID"],"w").write(str(os.getpid()))
    time.sleep(30)
if os.getenv("FAKE_TOOL_EVENT"):
    print(json.dumps({"type":"item.started","item":{"type":"command_execution","command":"unsafe"}}),flush=True)
    time.sleep(30)
tools=payload["available_tools"]
has_result=any(m["role"] == "tool" for m in payload["messages"])
action={"content":"The fixture says seven." if has_result else "", "tool_calls":[]}
if tools and not has_result:
    args={"query":"fixture"}
    if os.getenv("FAKE_LEGACY_ARGUMENTS"):
        args=json.dumps(args)
    action["tool_calls"]=[{"name":"lookup","arguments":args}]
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":json.dumps(action)}}))
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":20,"cached_input_tokens":50}}))
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)

    def prepare(self, directory, schema):
        (directory / "auth.json").write_text("temporary-test-credential")
        return [], dict(os.environ)

    monkeypatch.setattr(CodexCLIChatModel, "_prepare", prepare)
    return executable


def test_transcript_preserves_tool_ids_and_roles():
    messages = [
        SystemMessage(content="Answer in Chinese."),
        HumanMessage(content="Find the fixture."),
        AIMessage(
            content="",
            tool_calls=[{"name": "lookup", "args": {"query": "fixture"}, "id": "call_1"}],
        ),
        ToolMessage(content="seven", tool_call_id="call_1"),
    ]
    payload = _serialize_messages(messages)
    assert [entry["role"] for entry in payload] == ["system", "user", "assistant", "tool"]
    assert payload[2]["tool_calls"][0]["id"] == payload[3]["tool_call_id"]
    assert payload[3]["content"] == "seven"


def test_images_fail_explicitly_for_existing_searchos_image_fallback():
    with pytest.raises(NotImplementedError, match="image input is not supported"):
        _serialize_messages(
            [
                HumanMessage(
                    content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}]
                )
            ]
        )


@pytest.mark.parametrize(
    "action",
    [
        {"content": "", "tool_calls": [{"name": "shell", "arguments": "{}"}]},
        {"content": "", "tool_calls": [{"name": "lookup", "arguments": "[]"}]},
        {"content": "", "tool_calls": [{"name": "lookup", "arguments": "{invalid}"}]},
        {"content": "", "tool_calls": [], "extra": "not allowed"},
    ],
)
def test_invalid_actions_cannot_reach_searchos_tools(action):
    with pytest.raises(CodexCLIError):
        _parse_action(json.dumps(action), [TOOL], None, model="gpt-6-luna")


def test_required_tool_and_no_tool_contracts_are_enforced():
    with pytest.raises(CodexCLIError, match="所要求"):
        _parse_action(
            '{"content":"answer","tool_calls":[]}', [TOOL], "required", model="gpt-6-luna"
        )
    with pytest.raises(CodexCLIError, match="禁止工具"):
        _parse_action(
            json.dumps({"content": "", "tool_calls": [{"name": "lookup", "arguments": "{}"}]}),
            [TOOL],
            "none",
            model="gpt-6-luna",
        )


def test_tool_isolation_checks_structure_not_mentions():
    _assert_no_native_tools(json.dumps([{"role": "system", "content": "No tools available"}]))
    with pytest.raises(CodexCLIError, match="原生工具"):
        _assert_no_native_tools(
            json.dumps([{"type": "additional_tools", "tools": [{"name": "exec"}]}])
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_real_subprocess_tool_roundtrip_usage_and_temp_cleanup(
    tmp_path,
    fake_cli,
    monkeypatch,
    legacy,
):
    if legacy:
        monkeypatch.setenv("FAKE_LEGACY_ARGUMENTS", "1")
    usage = start_tracking()
    model = CodexCLIChatModel(
        cli_path=str(fake_cli),
        work_root=tmp_path,
        callbacks=[TokenTrackingCallback(role="sub_agent")],
    )
    bound = model.bind_tools([TOOL])
    request = HumanMessage(content="Read the fixture using lookup.")
    first = await bound.ainvoke([request])
    assert first.tool_calls[0]["name"] == "lookup"
    assert first.tool_calls[0]["args"] == {"query": "fixture"}
    second = await bound.ainvoke(
        [request, first, ToolMessage(content="seven", tool_call_id=first.tool_calls[0]["id"])]
    )
    assert second.content == "The fixture says seven."
    assert not second.tool_calls
    assert usage.llm_calls == 2
    assert usage.prompt_tokens == 200
    assert usage.completion_tokens == 40
    assert usage.cached_prompt_tokens == 100
    assert second.response_metadata["network_inference"] is True
    assert not list((tmp_path / ".runtime" / "codex").iterdir())


def test_real_orchestrator_tool_schema_has_typed_arguments_and_strict_optional_fields():
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from searchos.agents.orchestrator import get_orchestrator_tools

    tools = [convert_to_openai_tool(tool) for tool in get_orchestrator_tools(source_mode="local")]
    schema = _action_schema(tools)
    branches = schema["properties"]["tool_calls"]["items"]["anyOf"]
    assert len(branches) == len(tools)
    indexed = {branch["properties"]["name"]["enum"][0]: branch for branch in branches}
    for tool in tools:
        parameters = indexed[tool["function"]["name"]]["properties"]["arguments"]
        original = tool["function"]["parameters"]
        assert parameters["type"] == "object"
        assert parameters["additionalProperties"] is False
        assert set(parameters["required"]) == set(parameters["properties"])
        for name in original["properties"]:
            if name.endswith("_json"):
                shape = parameters["properties"][name]
                assert shape.get("type") == "string" or {"type": "string"} in shape["anyOf"]
    enqueue = indexed["enqueue_tasks"]["properties"]["arguments"]
    assert enqueue["type"] == "object"
    assert enqueue["required"] == ["items"]
    assert enqueue["properties"]["items"]["type"] == "array"
    task = enqueue["properties"]["items"]["items"]
    assert task["type"] == "object" and task["additionalProperties"] is False
    assert task["properties"]["task"]["type"] == "string"
    assert set(task["required"]) == set(task["properties"])
    create = indexed["create_schema"]["properties"]["arguments"]
    assert create["properties"]["tables"]["type"] == "array"
    assert create["properties"]["tables"]["items"]["type"] == "object"
    stop = indexed["stop_task"]["properties"]["arguments"]
    assert stop["properties"]["task_ids"] == {"type": "array", "items": {"type": "string"}}
    assert '"$ref"' not in json.dumps(schema)
    check = indexed["check_agents"]["properties"]["arguments"]
    assert check["additionalProperties"] is False
    assert check["required"] == ["timeout"]
    assert check["properties"]["timeout"]["anyOf"] == [{"type": "number"}, {"type": "null"}]


def test_real_local_typed_wrappers_accept_restored_nested_defaults():
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from searchos.agents.orchestrator import get_orchestrator_tools

    tools = {tool.name: tool for tool in get_orchestrator_tools(source_mode="local")}
    definitions = [convert_to_openai_tool(tool) for tool in tools.values()]
    action = {
        "content": "",
        "tool_calls": [
            {
                "name": "enqueue_tasks",
                "arguments": {
                    "items": [
                        {
                            "task": '核对资料中的 "任期" 与日期',
                            "target_table": None,
                            "target_cells": None,
                            "blocked_by": None,
                            "priority": None,
                        }
                    ]
                },
            },
            {
                "name": "create_schema",
                "arguments": {
                    "tables": [
                        {
                            "table_id": "leaders",
                            "attributes": ["name", "term"],
                            "primary_key": None,
                            "table_label": None,
                            "seed_entities": None,
                        }
                    ]
                },
            },
            {"name": "stop_task", "arguments": {"task_ids": ["task-1"], "reason": None}},
        ],
    }
    message = _parse_action(json.dumps(action), definitions, None, model="gpt-6-luna")
    args = {call["name"]: call["args"] for call in message.tool_calls}
    assert args["enqueue_tasks"] == {"items": [{"task": '核对资料中的 "任期" 与日期'}]}
    task = tools["enqueue_tasks"].args_schema.model_validate(args["enqueue_tasks"]).items[0]
    assert task.priority == 0.5 and task.blocked_by == [] and task.target_table == ""
    table = tools["create_schema"].args_schema.model_validate(args["create_schema"]).tables[0]
    assert table.attributes == ["name", "term"] and table.primary_key == []
    assert table.seed_entities == [] and table.table_label == ""
    assert args["stop_task"] == {"task_ids": ["task-1"]}


def test_typed_arguments_preserve_quotes_and_restore_optional_defaults():
    tool = {
        "type": "function",
        "function": {
            "name": "lookup",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                    "view": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
                },
                "required": ["query"],
            },
        },
    }
    message = _parse_action(
        json.dumps(
            {
                "content": "",
                "tool_calls": [
                    {
                        "name": "lookup",
                        "arguments": {
                            "query": '中文 "quoted" \\ content',
                            "limit": None,
                            "view": None,
                        },
                    }
                ],
            }
        ),
        [tool],
        None,
        model="gpt-6-luna",
    )
    assert message.tool_calls[0]["args"] == {"query": '中文 "quoted" \\ content', "view": None}


def test_legacy_arguments_with_trailing_json_are_rejected_without_salvaging():
    with pytest.raises(CodexCLIError, match="有效 JSON"):
        _parse_action(
            json.dumps(
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "name": "lookup",
                            "arguments": '{"query":"one"} {"query":"two"}',
                        }
                    ],
                }
            ),
            [TOOL],
            None,
            model="gpt-6-luna",
        )


def test_json_string_tool_fields_are_preserved_verbatim_inside_typed_arguments():
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from searchos.tools.tasks import enqueue_tasks

    tool = convert_to_openai_tool(enqueue_tasks)
    text = json.dumps(
        [{"task": 'Compare "Alpha" and 中文', "result_mode": "findings"}], ensure_ascii=False
    )
    message = _parse_action(
        json.dumps(
            {
                "content": "",
                "tool_calls": [
                    {
                        "name": "enqueue_tasks",
                        "arguments": {"items_json": text},
                    }
                ],
            }
        ),
        [tool],
        None,
        model="gpt-6-luna",
    )
    assert message.tool_calls[0]["args"]["items_json"] == text


def test_nested_references_and_nullable_objects_keep_optional_default_semantics():
    tool = {
        "type": "function",
        "function": {
            "name": "lookup",
            "parameters": {
                "type": "object",
                "properties": {
                    "nested": {
                        "anyOf": [
                            {"$ref": "#/$defs/Nested"},
                            {"type": "null"},
                        ]
                    }
                },
                "$defs": {
                    "Nested": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "limit": {"type": "integer", "default": 10},
                        },
                        "required": ["query"],
                    }
                },
            },
        },
    }
    schema = _action_schema([tool])
    assert '"$ref"' not in json.dumps(schema)
    message = _parse_action(
        json.dumps(
            {
                "content": "",
                "tool_calls": [
                    {
                        "name": "lookup",
                        "arguments": {"nested": {"query": "fixture", "limit": None}},
                    }
                ],
            }
        ),
        [tool],
        None,
        model="gpt-6-luna",
    )
    assert message.tool_calls[0]["args"] == {"nested": {"query": "fixture"}}


def test_empty_tool_schema_only_allows_no_calls():
    calls = _action_schema([])["properties"]["tool_calls"]
    assert calls["maxItems"] == 0
    assert "anyOf" not in calls["items"]


@pytest.mark.asyncio
async def test_native_tools_block_before_inference(tmp_path, fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_NATIVE", "1")
    pid_file = tmp_path / "pid"
    monkeypatch.setenv("FAKE_PID", str(pid_file))
    model = CodexCLIChatModel(cli_path=str(fake_cli), work_root=tmp_path)
    with pytest.raises(CodexCLIError, match="原生工具"):
        await model.ainvoke("Return seven.")
    assert not pid_file.exists()


@pytest.mark.asyncio
async def test_completed_turn_survives_transient_cli_error_notice(tmp_path, fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_TRANSIENT", "1")
    model = CodexCLIChatModel(cli_path=str(fake_cli), work_root=tmp_path)
    result = await model.ainvoke("Return seven.")
    assert result.usage_metadata["total_tokens"] == 120


@pytest.mark.asyncio
async def test_unexpected_native_execution_event_fails_closed(tmp_path, fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_TOOL_EVENT", "1")
    model = CodexCLIChatModel(cli_path=str(fake_cli), work_root=tmp_path, timeout_s=3)
    with pytest.raises(CodexCLIError, match="原生工具事件"):
        await model.ainvoke("Return seven.")
    assert not list((tmp_path / ".runtime" / "codex").iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_reap_process_and_remove_auth(
    tmp_path, fake_cli, monkeypatch, cancel
):
    pid_file = tmp_path / "pid"
    monkeypatch.setenv("FAKE_PID", str(pid_file))
    model = CodexCLIChatModel(
        cli_path=str(fake_cli), work_root=tmp_path, timeout_s=0.5 if not cancel else 20
    )
    task = asyncio.create_task(model.ainvoke("Return seven."))
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_file.exists()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(CodexCLIError, match="已终止"):
            await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    assert not list((tmp_path / ".runtime" / "codex").iterdir())
