"""Contract tests only: no dataset questions, external network or real keys."""
import asyncio
import json
import socket

import httpx
import pytest

from widesearch_experiment.common import config
from widesearch_baselines.launcher import DEFAULT_CONFIG, routed_runner, validate_config
from widesearch_baselines.open_deep_research import model_scope, partial_response, settings
from widesearch_baselines.mindsearch_protocol import completed_node_text, final_text, normalize_plugin


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Network access is forbidden in offline baseline checks")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.setenv(name, "offline-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://offline.invalid/v1")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")


def response(output):
    return {"id": "resp_offline", "object": "response", "created_at": 1,
            "model": "deepseek-flash", "status": "completed", "output": output,
            "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                      "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 1}}}


def text_output(text):
    return [{"type": "message", "id": "msg_offline", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": text, "annotations": []}]}]


def test_profile_and_all_upstream_roles_are_deepseek():
    cfg = config(DEFAULT_CONFIG)
    validate_config(cfg)
    value = settings(cfg)
    assert value["allow_clarification"] is False
    assert all(value[f"{role}_model"] == "openai:deepseek-flash"
               for role in ("research", "compression", "final_report", "summarization"))
    with pytest.raises(ValueError):
        validate_config({**cfg, "judge_model": "gpt-4o"})


def test_native_odr_structured_output_with_config():
    from pydantic import BaseModel

    class Result(BaseModel):
        value: str

    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.url.path == "/v1/responses"
        assert payload["model"] == "deepseek-flash"
        assert payload["max_output_tokens"] == 77
        assert payload["reasoning"] == {"effort": "low"}
        assert payload.get("store") is not False
        assert "reasoning.encrypted_content" not in payload.get("include", [])
        assert payload["text"]["format"]["type"] == "json_schema"
        return httpx.Response(200, json=response(text_output('{"value":"fixture"}')))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with model_scope(config(DEFAULT_CONFIG)) as create:
                model = create(configurable_fields=("model", "max_tokens", "api_key"), http_async_client=client)
                result = await (model.with_structured_output(Result).with_retry(stop_after_attempt=1)
                                .with_config({"model": "openai:deepseek-flash", "max_tokens": 77,
                                              "api_key": "offline-test"}).ainvoke("fixture"))
                assert result.value == "fixture"
    asyncio.run(exercise())
    assert len(requests) == 1


def test_odr_forced_tool_roundtrip_then_restores_low_reasoning():
    from langchain_core.messages import HumanMessage, ToolMessage
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["reasoning"]["effort"] == ("none" if len(requests) <= 2 else "low")
        if len(requests) == 1:
            output = [{"type": "function_call", "id": "fc_offline", "call_id": "call_fixture",
                       "name": "lookup", "arguments": "{}", "status": "completed"}]
        else:
            output = text_output("fixture")
        if len(requests) == 2:
            assert any(x.get("type") == "function_call_output" and x["call_id"] == "call_fixture"
                       for x in payload["input"])
        return httpx.Response(200, json=response(output))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with model_scope(config(DEFAULT_CONFIG)) as create:
                model = create(http_async_client=client)
            tool = {"type": "function", "function": {"name": "lookup", "description": "Fixture",
                    "parameters": {"type": "object", "properties": {}}}}
            prompt = HumanMessage(content="fixture")
            first = await model.bind_tools([tool], tool_choice="required").ainvoke([prompt])
            history = [prompt, first, ToolMessage(content="fixture", tool_call_id="call_fixture")]
            second = await model.ainvoke(history)
            assert second.content == "fixture"
            await model.ainvoke([*history, second, HumanMessage(content="fixture next turn")])
    asyncio.run(exercise())
    assert len(requests) == 3


def test_worker_routing_leaves_scorer_and_original_process_contract_unchanged(monkeypatch):
    from widesearch_experiment.runners import batch
    calls = []
    async def fake(args, *pos, **kwargs):
        calls.append(args)
        return 0, False
    monkeypatch.setattr(batch, "subprocess_run", fake)
    original_hashes = batch.code_hashes
    with routed_runner():
        hashes = batch.code_hashes()
        assert "../widesearch_baselines/mindsearch.py" in hashes
        assert all(hashes[k] == v for k, v in original_hashes().items())
        asyncio.run(batch.subprocess_run(["python", "-m", "widesearch_experiment.runners.worker"]))
        asyncio.run(batch.subprocess_run(["python", "-m", "widesearch_experiment.evaluation.official"]))
    assert calls[0][2] == "widesearch_baselines.worker"
    assert calls[1][2] == "widesearch_experiment.evaluation.official"
    assert batch.code_hashes is original_hashes and batch.subprocess_run is fake


def test_partial_recovery_excludes_plans_and_streaming_text():
    assert partial_response({"research_brief": "instructions", "messages": ["instructions"]}) == ""
    assert partial_response({"notes": ["saved evidence"]}) == "saved evidence"
    assert partial_response({"final_report": "final", "notes": ["older"]}) == "final"
    valid = {"content": "saved evidence", "stream_state": 0, "formatted": {"tool_type": None}}
    assert final_text(valid) == "saved evidence"
    assert final_text({**valid, "stream_state": 1}) == ""
    assert final_text({**valid, "content": '<|action_start|><|unknown|>{}<|action_end|>'}) == ""
    message = {"formatted": {"node": {"root": {"response": valid}, "a": {"response": valid},
               "b": {"response": {**valid, "stream_state": 1}}}}}
    assert completed_node_text(message) == "## a\n\nsaved evidence"


@pytest.mark.parametrize("raw", [
    '<|action_start|><|WebBrowser.search|>{"query":',
    '<|action_start|><|WebBrowser.delete|>{"url":"x"}<|action_end|>',
    '<|action_start|><|WebBrowser.search|>{"query":"x","extra":1}<|action_end|>',
    'ordinary prose mentioning WebBrowser.search',
])
def test_protocol_does_not_invent_or_guess_calls(raw):
    assert normalize_plugin(raw) == raw


def test_explicit_known_tool_is_only_reframed():
    raw = 'thinking<|action_start|><|WebBrowser.search|>{"query":["a","b"]}<|action_end|>'
    fixed = normalize_plugin(raw)
    args = json.loads(fixed.split('<|plugin|>')[1].split('<|action_end|>')[0])
    assert args == {"name": "WebBrowser.search", "parameters": {"query": ["a", "b"]}}
    assert normalize_plugin(fixed) == fixed
