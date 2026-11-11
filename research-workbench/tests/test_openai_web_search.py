"""Offline Responses discovery, settings isolation and no-snippet evidence regressions."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))
from api import workbench
from api.routes.workbench import SearchProbeRequest, probe_search
from searchos.tools.simple_browser.search.openai_web_search import (
    OpenAIWebSearchProvider, normalize_source_url, parse_discovery_response,
)
from searchos.tools.simple_browser.search.base import SearchResult


def response_payload():
    return {"id": "resp_test", "status": "completed", "output": [
        {"type": "web_search_call", "status": "completed", "action": {
            "type": "search", "sources": [
                {"type": "url", "url": "https://www.anduril.com/omen", "title": "Omen"},
                {"type": "url", "url": "https://www.anduril.com/omen#details"},
                {"type": "url", "url": "javascript:alert(1)"},
                {"type": "url", "url": "https://name:password@bad.example/"},
            ]}},
        {"type": "message", "content": [{"type": "output_text",
            "text": "Generated answer https://www.anduril.com/hallucinated is not a source or snippet.",
            "annotations": [{"type": "url_citation", "url": "https://www.anduril.com/omen", "title": "Omen"},
                            {"type": "url_citation", "url": "https://www.anduril.com/news/omen", "title": "Announcement"}]}]},
    ]}


def test_parser_accepts_only_tool_sources_or_citations_and_never_generated_answer():
    results = parse_discovery_response(response_payload())
    assert [item.url for item in results] == ["https://www.anduril.com/omen", "https://www.anduril.com/news/omen"]
    assert all(item.content == "" and item.snippet == "" for item in results)
    assert all(not item.allow_snippet_fallback and item.discovery_type == "web_search_discovery" for item in results)
    assert len(parse_discovery_response(response_payload(), 1)) == 1


def test_tracking_variants_of_one_homepage_are_one_discovery_source():
    payload = response_payload()
    payload["output"] = [{"type": "web_search_call", "status": "completed", "action": {
        "sources": [{"url": "https://www.anduril.com/?utm_source=openai"},
                    {"url": "https://www.anduril.com/?trk=search&utm_source=openai&gclid=example"},
                    {"url": "https://www.anduril.com/lattice/command-and-control?utm_source=openai"}]}}]
    assert [item.url for item in parse_discovery_response(payload)] == [
        "https://www.anduril.com/", "https://www.anduril.com/lattice/command-and-control"]
    assert normalize_source_url("https://www.anduril.com/search?q=lattice&page=2&utm_medium=web&fbclid=123#result") == (
        "https://www.anduril.com/search?q=lattice&page=2")
    assert normalize_source_url("https://www.anduril.com/?id=&version=2") == "https://www.anduril.com/?id=&version=2"


@pytest.mark.parametrize("output", [[], [{"type": "message", "content": [{"text": "Claims I searched"}]}],
                                    [{"type": "web_search_call", "status": "failed"}]])
def test_response_without_completed_real_search_call_is_a_failure(output):
    with pytest.raises(RuntimeError, match="web_search_call"):
        parse_discovery_response({"output": output})


def fake_sdk(monkeypatch, payload, *, failure=None):
    calls = []
    client_arguments = []
    active = {"current": 0, "maximum": 0}

    class Client:
        def __init__(self, **kwargs):
            client_arguments.append(kwargs)
            self.responses = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def create(self, **kwargs):
            calls.append(kwargs)
            active["current"] += 1
            active["maximum"] = max(active["maximum"], active["current"])
            await asyncio.sleep(0)
            active["current"] -= 1
            if failure:
                raise failure
            return SimpleNamespace(model_dump=lambda **kwargs: payload)

    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", Client)
    return calls, client_arguments, active


@pytest.mark.asyncio
async def test_bounded_request_records_redacted_receipt_and_preserves_model(monkeypatch, tmp_path):
    payload = response_payload()
    payload["headers"] = {"Authorization": "hidden-secret"}
    payload["metadata"] = {"api_key": "hidden-secret", "note": "hidden-secret"}
    calls, clients, active = fake_sdk(monkeypatch, payload)
    provider = OpenAIWebSearchProvider("hidden-secret", model="gpt-4o", allowed_domains=["anduril.com"], receipt_dir=tmp_path)
    results = await provider.search("Lattice Mesh")
    assert len(results) == 2
    request = calls[0]
    assert request == {"model": "gpt-4o", "input": "Lattice Mesh", "tools": [{
        "type": "web_search", "search_context_size": "low", "filters": {"allowed_domains": ["anduril.com"]}}],
        "tool_choice": "required", "max_output_tokens": 1000, "max_tool_calls": 1,
        "include": ["web_search_call.action.sources"]}
    assert clients[0]["max_retries"] == 0 and clients[0]["timeout"] == 55
    receipt = next(tmp_path.glob("*.json")).read_text()
    assert "hidden-secret" not in receipt and "Authorization" not in receipt and '"headers"' not in receipt
    assert json.loads(receipt)["type"] == "web_search_discovery"
    await asyncio.gather(*(provider.search("same bounded query") for _ in range(3)))
    assert active["maximum"] == 1


@pytest.mark.asyncio
async def test_api_failure_is_not_retried_fallback_or_leaked(monkeypatch):
    calls, _, _ = fake_sdk(monkeypatch, {}, failure=RuntimeError("secret from upstream"))
    provider = OpenAIWebSearchProvider("saved-key", receipt_dir=None)
    with pytest.raises(RuntimeError, match="no provider fallback") as caught:
        await provider.search("public query")
    assert "secret from upstream" not in str(caught.value)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_domain_filter_rejects_offsite_citation_and_default_factory_resolves(monkeypatch):
    from searchos.tools.simple_browser.search import build_search_provider, resolve_search_provider_name
    payload = response_payload()
    payload["output"][0]["action"]["sources"].insert(0, {"url": "https://unrelated.example/article"})
    fake_sdk(monkeypatch, payload)
    provider = OpenAIWebSearchProvider("saved", allowed_domains=["anduril.com"], receipt_dir=None)
    assert all("anduril.com" in result.url for result in await provider.search("query"))
    assert len(await provider.search("query", max_results=1)) == 1
    assert resolve_search_provider_name("openai_web_search") == "openai_web_search"
    built = build_search_provider("openai_web_search", api_key="saved", web_search_model="chosen-model")
    assert built.name == "openai_web_search" and built.model == "chosen-model"


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(workbench, "CONFIG_PATH", tmp_path / "private.json")
    config = workbench.Configuration()
    config.openai.api_key = "saved-openai-key"
    config.openai.model = "analysis-model"
    workbench.save_config(config)
    from searchos.tools.simple_browser import state
    previous = state.get_provider()
    yield
    state.set_browser_provider(previous)


@pytest.mark.asyncio
async def test_public_search_config_reuses_key_and_search_model_is_independent(isolated_settings, monkeypatch):
    async def no_ollama(_config):
        return [], ""
    monkeypatch.setattr(workbench, "ollama_models", no_ollama)
    config = workbench.merged_config({"search": {"provider": "openai_web_search", "web_search_model": "chosen-search-model",
                                               "web_search_allowed_domains": ["Anduril.com"]}})
    workbench.save_config(config)
    public = await workbench.public_settings(config)
    assert public["search"]["openai_key_configured"] is True
    assert public["search"]["web_search_model"] == "chosen-search-model"
    assert "saved-openai-key" not in json.dumps(public)
    assert public["openai"]["model"] == "analysis-model"
    provider = workbench.search_provider(config.search, openai=config.openai)
    assert provider.model == "chosen-search-model" and provider._api_key == "saved-openai-key"
    assert provider.allowed_domains == ["anduril.com"]
    workbench.configure_search(required=True)
    from searchos.tools.simple_browser.state import get_provider
    assert get_provider().name == "openai_web_search"


@pytest.mark.asyncio
async def test_probe_uses_saved_openai_key_without_saving_draft(isolated_settings, monkeypatch):
    async def search(self, query, max_results=10):
        assert self._api_key == "saved-openai-key"  # request.api_key must not replace it.
        assert self.model == "draft-model" and self.allowed_domains == ["anduril.com"]
        return [SearchResult(title="Official", url="https://www.anduril.com/omen")]
    monkeypatch.setattr(OpenAIWebSearchProvider, "search", search)
    before = workbench.CONFIG_PATH.read_bytes()
    result = await probe_search(SearchProbeRequest(provider="openai_web_search", query="Omen", api_key="ignored-key",
                                                    web_search_model="draft-model", web_search_allowed_domains=["anduril.com"]))
    assert result["ok"] and result["provider"] == "openai_web_search"
    assert workbench.CONFIG_PATH.read_bytes() == before


def test_missing_openai_key_is_explicit_and_invalid_domains_are_rejected():
    with pytest.raises(HTTPException, match="OpenAI"):
        workbench.search_provider(workbench.Search(provider="openai_web_search"), openai=workbench.OpenAIEndpoint())
    assert workbench.search_provider(workbench.Search(provider="openai_web_search"), openai=workbench.OpenAIEndpoint(), required=False) is None
    for domain in ["https://anduril.com", "anduril.com/path", "*.anduril.com"]:
        with pytest.raises(ValueError):
            workbench.Search(web_search_allowed_domains=[domain])
    with pytest.raises(ValueError):
        workbench.Search(web_search_model="  ")


@pytest.mark.asyncio
async def test_search_engine_backend_never_replays_openai_discovery_as_full_page():
    from searchos.tools.simple_browser.backend.search_engine import (
        SearchEngineBackend, clear_search_results, register_search_results,
    )
    clear_search_results()
    result = parse_discovery_response(response_payload())[0]
    result.snippet = "discovery summary"
    register_search_results([result])
    fetched = await SearchEngineBackend().fetch(result.url)
    assert not fetched.ok and not fetched.markdown
    assert fetched.metadata["snippet_fallback_rejected"] is True
    clear_search_results()


@pytest.mark.asyncio
async def test_open_and_batch_failures_keep_fetch_error_without_persisting_snippet(monkeypatch):
    from searchos.tools.simple_browser import batch, state, tools
    from searchos.tools.simple_browser.render import PageContents
    previous = state.get_provider()
    state.set_browser_provider(OpenAIWebSearchProvider("unused", receipt_dir=None))
    state.reset_browser()
    url = "https://www.anduril.com/omen"
    state._get_browser().search_content_cache[url] = SearchResult(
        title="Discovery", url=url, snippet="Do not persist me", allow_snippet_fallback=False)
    calls = []

    async def failure(url):
        return PageContents(url=url, text="[FETCH_ERROR] original page unavailable", urls={})

    def persist(url, page):
        calls.append(page.text)
        return ""

    for module in [tools, batch]:
        monkeypatch.setattr(module, "_fetch_page", failure)
        monkeypatch.setattr(module, "_read_page_from_disk", lambda url: None)
        monkeypatch.setattr(module, "_persist_page", persist)
    try:
        answer = await tools.open.ainvoke({"id_or_url": url})
        assert "[FETCH_ERROR]" in str(answer) and "Do not persist me" not in str(answer)
        state._get_browser().add_page(PageContents(
            url=url, text="[NOTE] Live fetch failed; search-engine snippet, not the full page. Do not persist me", urls={}))
        pages = await batch._open_many([url])
        assert pages[0].text.startswith("[FETCH_ERROR]")
        assert all("Do not persist me" not in text for text in calls)
    finally:
        state.set_browser_provider(previous)
        state.reset_browser()
