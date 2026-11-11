import asyncio
import json

import httpx
import pytest

from widesearch_experiment import common
from widesearch_experiment.common import config, credential_env
from widesearch_experiment.model_runtime import searchos_model
from widesearch_experiment.services import Journal, Services


def test_saved_deepseek_key_wins_over_openai_key(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "APP", tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-only")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    private = tmp_path / ".workbench/private.json"
    private.parent.mkdir()
    private.write_text(json.dumps({"deepseek": {"api_key": "saved-deepseek"}}))
    env = credential_env(config())
    assert env["OPENAI_API_KEY"] == env["DEEPSEEK_API_KEY"] == "saved-deepseek"
    assert env["OPENAI_BASE_URL"] == "https://api.deepseek.com"
    assert credential_env({})["OPENAI_API_KEY"] == "openai-only"
    private.write_text("{}")
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        credential_env(config())


def test_search_and_fetch_keep_real_receipts_separate_from_summaries(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    requests = []

    def handle(request):
        assert request.headers["authorization"] == "Bearer deepseek-test"
        payload = json.loads(request.content)
        requests.append(request.url.path)
        if request.url.path == "/v1/tools/search":
            assert payload == {"query": "test", "count": 6, "need_content": False}
            return httpx.Response(200, json={"results": [
                {"url": "https://example.com/", "title": "Example", "snippet": "short",
                 "summary": "Search summary", "content": "Not independently fetched"},
                {"url": "https://example.com/", "title": "duplicate"},
                {"url": "https://user:secret@example.com/"}, {"url": "file:///private"}]})
        assert payload == {"url": "https://example.com/"}
        return httpx.Response(200, json={"title": "Source page", "content": "# Real page\nSource text"})

    original = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        return original(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    journal = Journal(tmp_path)
    services = Services(config(), journal)
    hits = asyncio.run(services.search("test"))
    assert hits == [{"url": "https://example.com/", "title": "Example", "snippet": "Search summary", "content": ""}]
    assert asyncio.run(services.fetch(hits[0]["url"])) == "# Real page\nSource text"
    assert requests == ["/v1/tools/search", "/v1/tools/fetch"]
    assert journal.counts()["search_calls"] == journal.counts()["pages_fetched"] == 1
    assert len(list((tmp_path / "search_receipts").glob("*.json"))) == 1
    assert len(list((tmp_path / "fetch_receipts").glob("*.json"))) == 1
    assert len(list((tmp_path / "pages").glob("*.json"))) == 1


def test_empty_queries_are_not_counted_as_api_calls(tmp_path):
    journal = Journal(tmp_path)
    services = Services(config(), journal)
    with pytest.raises(ValueError, match="shorter search query"):
        asyncio.run(services.search(" "))
    assert journal.counts()["search_calls"] == 0
    assert journal.events[0]["kind"] == "search_rejected"


def test_long_queries_keep_all_terms_and_count_each_network_request(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test")
    queries = []

    def handle(request):
        query = json.loads(request.content)["query"]
        queries.append(query)
        assert 1 <= len(query) <= 128
        return httpx.Response(200, json={"results": [{"url": f"https://example.com/{len(queries)}"}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    journal = Journal(tmp_path)
    services = Services(config(), journal)
    query = " ".join(f"term{i}" for i in range(60))
    hits = asyncio.run(services.search(query, 2))
    assert " ".join(queries) == query
    assert len(queries) > 1 and journal.counts()["search_calls"] == len(queries)
    assert journal.events[0]["query"] == query
    assert [h["url"] for h in hits] == ["https://example.com/1", "https://example.com/2"]


def test_insufficient_balance_is_visible_and_not_empty_success(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test")
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(402, json={"error": "Insufficient balance"})), **kwargs))
    journal = Journal(tmp_path)
    services = Services(config(), journal)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(services.search("test"))
    assert journal.events[-1]["status_code"] == 402
    assert asyncio.run(services.fetch("https://example.com/")) == ""
    assert journal.events[-1]["success"] is False
    assert journal.events[-1]["status"] == 402
    assert journal.counts()["pages_fetched"] == 0


def test_required_tool_keeps_required_contract_with_compatible_reasoning():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    model = searchos_model(config(), api_key="deepseek-test", base_url="https://api.deepseek.com")
    try:
        assert model._get_request_payload("hello")["reasoning"] == {"effort": "low"}
        assert "include" not in model._get_request_payload("hello")
        for choice in ("required", {"type": "function", "name": "lookup"}):
            payload = model._get_request_payload("hello", tool_choice=choice)
            assert payload["tool_choice"] == choice
            assert payload["reasoning"] == {"effort": "none"}
        assert model._get_request_payload("hello", tool_choice="auto")["reasoning"] == {"effort": "low"}
        turn = [HumanMessage(content="lookup"),
                AIMessage(content="", tool_calls=[{"id": "call_1", "name": "lookup", "args": {}}]),
                ToolMessage(content="7", tool_call_id="call_1")]
        assert model._get_request_payload(turn)["reasoning"] == {"effort": "none"}
        # A fresh user turn restores the configured low thinking level.
        assert model._get_request_payload(turn + [HumanMessage(content="continue")])["reasoning"] == {"effort": "low"}
        turn[1].additional_kwargs["reasoning"] = {"type": "reasoning", "summary": [],
            "content": [{"type": "reasoning_text", "text": "Use lookup."}]}
        turn[1].additional_kwargs["experiment_response_content"] = []
        payload = model._get_request_payload(turn)
        assert payload["reasoning"] == {"effort": "low"}
        assert any(x.get("type") == "reasoning" and x.get("content") for x in payload["input"])
    finally:
        model.http_client.close()
        asyncio.run(model.http_async_client.aclose())
