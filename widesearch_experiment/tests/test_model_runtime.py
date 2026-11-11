import asyncio
import json

import httpx
import pytest

from widesearch_experiment.common import config
from widesearch_experiment.model_runtime import response_completion, searchos_model
from widesearch_experiment.services import Journal, Services


def response(output, *, status="completed"):
    return {"id": "resp_test", "object": "response", "created_at": 1,
            "model": "gpt-6-luna", "status": status, "output": output,
            "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
            "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30,
                      "input_tokens_details": {"cached_tokens": 4, "cache_write_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 3}}}


def text_output(text="7"):
    return [{"type": "message", "id": "msg_test", "role": "assistant", "status": "completed",
             "phase": "final_answer", "content": [{"type": "output_text", "text": text, "annotations": []}]}]


def test_responses_tool_roundtrip_preserves_reasoning_and_text_contract():
    from langchain_core.messages import HumanMessage, ToolMessage
    requests = []

    def handle(request):
        assert request.url.path == "/v1/responses"
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["model"] == "gpt-6-luna"
        assert payload["reasoning"]["effort"] == "low"
        assert "temperature" not in payload and "seed" not in payload
        if len(requests) == 1:
            output = [{"type": "reasoning", "id": "rs_test", "summary": [], "encrypted_content": "opaque"},
                      {"type": "function_call", "id": "fc_test", "call_id": "call_test",
                       "name": "lookup", "arguments": "{}", "status": "completed"}]
        else:
            assert any(x.get("type") == "function_call_output" and x["call_id"] == "call_test" for x in payload["input"])
            assert any(x.get("type") == "reasoning" and x.get("encrypted_content") == "opaque" for x in payload["input"])
            output = text_output()
        return httpx.Response(200, json=response(output))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            model = searchos_model(config("widesearch_experiment/configs/unified_luna.json"), api_key="offline-test", base_url="https://test.invalid/v1",
                                   http_async_client=client, max_retries=0)
            prompt = HumanMessage(content="Read lookup.")
            tool = {"type": "function", "function": {"name": "lookup", "description": "Read a value",
                    "parameters": {"type": "object", "properties": {}}}}
            first = await model.bind_tools([tool], tool_choice="required").ainvoke([prompt])
            assert isinstance(first.content, str)
            result = await model.ainvoke([prompt, first, ToolMessage(content="7", tool_call_id="call_test")])
            assert result.content == "7"
            assert result.usage_metadata["output_token_details"]["reasoning"] == 3
            # Preserve the response's phase when a text reply is sent back too.
            payload = model._get_request_payload([prompt, result, HumanMessage(content="Continue")])
            assert any(x.get("phase") == "final_answer" for x in payload["input"])
    asyncio.run(run())
    assert len(requests) == 2


def test_legacy_gpt4o_still_uses_chat_completions():
    cfg = config("widesearch_experiment/configs/unified_gpt4o.json")
    model = searchos_model(cfg, api_key="offline-test")
    payload = model._get_request_payload("Hello")
    assert payload["model"] == "gpt-4o-2024-08-06"
    assert "messages" in payload and "input" not in payload
    assert "reasoning" not in payload and payload["temperature"] == 0


def test_incomplete_response_is_not_accepted_as_a_table():
    from openai.types.responses import Response
    with pytest.raises(RuntimeError, match="incomplete"):
        response_completion(Response.model_validate(response(text_output(), status="incomplete")))


def test_judge_json_framing_does_not_repair_or_change_decisions():
    from widesearch_experiment.evaluation.official import judge_payload
    raw = '{"idx_1": 0, "idx_2": 1}'
    parsed, framed = judge_payload(raw)
    assert parsed == {"idx_1": 0, "idx_2": 1}
    assert framed == '```json\n' + raw + '\n```'
    assert judge_payload(framed) == (parsed, framed)
    for invalid in ('{"idx_1":', 'Here is {"idx_1": 1}', '[1, 0]'):
        assert judge_payload(invalid) == (None, invalid)


def test_luna_completion_keeps_original_search_configuration(tmp_path, monkeypatch):
    import openai
    cfg = config("widesearch_experiment/configs/unified_luna.json")
    assert cfg["search_model"] == "gpt-4o-2024-08-06"
    assert cfg["judge_model"] == "gpt-6-luna" and cfg["judge_reasoning_effort"] == "low"
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if "tools" in payload:
            assert payload["model"] == cfg["search_model"]
            assert payload["tools"][0]["type"] == "web_search"
            assert "reasoning" not in payload
            output = [{"id": "ws_test", "type": "web_search_call", "status": "completed",
                       "action": {"type": "search", "query": "test", "sources": [
                           {"type": "url", "url": "https://example.com", "title": "Example"}]}}]
        else:
            assert payload["reasoning"] == {"effort": "low"}
            output = text_output("|name|\n|---|\n|Example|")
        return httpx.Response(200, json=response(output))

    original = openai.AsyncOpenAI
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kwargs: original(api_key="offline-test",
        base_url="https://test.invalid/v1", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)), **kwargs))
    journal = Journal(tmp_path)
    services = Services(cfg, journal)
    assert "Example" in asyncio.run(services.complete("Extract a table."))
    assert asyncio.run(services.search("test"))[0]["url"] == "https://example.com"
    assert journal.counts()["input_tokens"] == 20
    end = next(e for e in journal.events if e["kind"] == "model_end")
    assert end["cached_input_tokens"] == 4 and end["reasoning_tokens"] == 3
