"""Real native graph + mocked transports; no benchmark questions, keys or network."""
import asyncio
import json
import socket
import sys
from contextlib import contextmanager
from unittest.mock import AsyncMock

import httpx
import pytest

from widesearch_experiment.common import config, read_json
from widesearch_experiment.services import Journal, Services
from widesearch_odr_lite import SYSTEM
from widesearch_odr_lite import adapter
from widesearch_odr_lite.launcher import DEFAULT_CONFIG, lite_runner, validate_config
from widesearch_odr_lite.retrieval import Retrieval, excerpts


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("External network is forbidden")
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.setenv(name, "offline-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://offline.invalid/v1")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")


def response(output):
    return {"id": "resp_fixture", "object": "response", "created_at": 1,
            "model": "deepseek-flash", "status": "completed", "output": output,
            "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                      "input_token_details": {}, "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 0}}}


def message(text):
    return [{"type": "message", "id": "msg_fixture", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": text, "annotations": []}]}]


def call(name, arguments, identifier):
    return {"type": "function_call", "id": "fc_" + identifier, "call_id": identifier,
            "name": name, "arguments": json.dumps(arguments), "status": "completed"}


def test_profile_rejects_hidden_extra_calls():
    cfg = config(DEFAULT_CONFIG)
    validate_config(cfg)
    for change in ({"conversion": "deterministic_then_llm"}, {"execution_attempts": 2},
                   {"odr_research_model_limit": 8}, {"judge_attempts": 2}):
        with pytest.raises(ValueError):
            validate_config({**cfg, **change})


def test_excerpt_selects_late_relevant_text_verbatim():
    text = "Unrelated introductory text.\n\n" * 100 + "Alpha sonar launch year: 2001.\n\nOther data."
    picked = excerpts(text, "Alpha sonar launch", limit=45)
    assert any("2001" in p["text"] for p in picked)
    assert sum(len(p["text"]) for p in picked) <= 45
    assert all(p["text"] == text[p["start"]:p["end"]] for p in picked)


def test_cache_budgets_failures_and_saved_evidence(tmp_path):
    async def exercise():
        cfg = {**config(DEFAULT_CONFIG), "odr_search_limit": 2, "max_pages": 1}
        service = Services(cfg, Journal(tmp_path))
        service.search = AsyncMock(return_value=[
            {"url": "https://fixture.test/a", "snippet": "Fallback snippet Alpha year 2001"},
            {"url": "https://fixture.test/b", "snippet": "Only a search snippet, Beta year 2002"}])
        service.fetch = AsyncMock(return_value="")
        store = Retrieval(cfg, service, tmp_path)
        values = await asyncio.gather(store.search(["Alpha"]), store.search(["Alpha"]))
        assert service.search.await_count == 1
        assert service.fetch.await_count == 1  # Includes failed fetch, no repeated paid retry.
        assert all("search_snippet" in value for value in values)
        await store.search(["x" * 400])
        assert service.search.await_count == 2
        assert all(len(args.args[0]) <= 128 for args in service.search.await_args_list)
        assert store.stats()["limits_hit"] == ["pages", "search"]
        assert len(store.final_context("year")) <= cfg["odr_final_context_chars"]
        saved = read_json(tmp_path / "evidence_fragments.json")
        assert len(saved) == 2
        assert all(s["kind"] == "search_snippet" for row in saved for s in row["selections"])
        await store.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("limit,mode", [(7, "ok"), (4, "ok"), (7, "supervisor_error"),
                                       (7, "final_error"), (7, "malformed_table")])
def test_native_graph_has_bounded_calls_and_preserves_evidence(tmp_path, monkeypatch, limit, mode):
    requests, final_inputs = [], []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.url.path == "/v1/responses"
        assert payload["max_output_tokens"] == 4096
        if isinstance(payload["input"], str):
            final_inputs.append(payload["input"])
            if mode == "final_error":
                raise httpx.ReadError("fixture failure", request=request)
            output = message("Unstructured fixture answer" if mode == "malformed_table" else
                             "| Product | Year |\n|---|---|\n| Alpha | 2001 |")
        elif payload.get("text", {}).get("format", {}).get("type") == "json_schema":
            output = message(json.dumps({"research_brief": "Find Alpha and Beta products, return Product and Year."}))
        else:
            names = {tool.get("name") for tool in payload.get("tools", [])}
            has_observation = any(item.get("type") == "function_call_output" for item in payload["input"])
            if "ConductResearch" in names:
                if has_observation:
                    if mode == "supervisor_error":
                        raise httpx.ReadError("fixture failure", request=request)
                    output = [call("ResearchComplete", {}, "supervisor_done")]
                else:
                    output = [call("ConductResearch", {"research_topic": topic}, "delegate_" + topic)
                              for topic in ("Alpha", "Beta")]
            elif "tavily_search" in names:
                output = ([call("ResearchComplete", {}, "research_done")] if has_observation else
                          [call("tavily_search", {"queries": ["fixture shared"]}, "search_fixture")])
            else:
                raise AssertionError("Unexpected model stage: per-page summary/compression must be absent")
        return httpx.Response(200, json=response(output))

    async def exercise():
        cfg = {**config(DEFAULT_CONFIG), "odr_research_model_limit": limit}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            import widesearch_baselines.open_deep_research as base
            import widesearch_experiment.services as common_services
            original_model = base.searchos_model

            def local_model(settings, **kwargs):
                return original_model(settings, **{**kwargs, "http_async_client": client})

            monkeypatch.setattr(base, "searchos_model", local_model)
            monkeypatch.setattr(common_services, "sdk_client_options", lambda *a, **k: {"http_client": client})
            service = Services(cfg, Journal(tmp_path))
            service.search = AsyncMock(return_value=[{"url": "https://fixture.test/a", "title": "Fixture", "snippet": "Alpha"}])

            async def fetch(url):
                text = "Alpha product launch year: 2001. Beta launch year: 2002."
                service.save_page(url, text)
                return text
            service.fetch = AsyncMock(side_effect=fetch)
            task = {"instance_id": "fixture", "language": "en", "query": "Return Product and Year for Alpha and Beta."}
            from widesearch_experiment.runners import worker
            monkeypatch.setattr(worker, "Services", lambda *args: service)
            monkeypatch.setitem(sys.modules, f"widesearch_experiment.adapters.{SYSTEM}_adapter", adapter)
            monkeypatch.chdir(tmp_path)
            await worker.execute({"task": task, "config": cfg, "system": SYSTEM,
                                  "trial": 1, "attempt": 1, "config_fingerprint": "offline-fixture"}, tmp_path)
            result = read_json(tmp_path / "result.json")
            assert len(requests) <= limit + 1
            assert len(final_inputs) == 1
            assert "https://fixture.test/a" in final_inputs[0] and "2001" in final_inputs[0]
            assert service.fetch.await_count == service.search.await_count == 1
            assert len(list((tmp_path / "pages").glob("*.json"))) == 1
            assert read_json(tmp_path / "budget.json")["total_model_requests"] == len(requests)
            assert service.journal.counts()["model_calls"] == len(requests)
            if mode == "ok":
                assert result["status"] == "success"
                assert "| Alpha | 2001 |" in result["response"]
                assert len(requests) == limit + 1
                if limit == 4:
                    assert result["stop_reason"] == "model_budget"
                    assert result["budget"]["blocked_model_requests"] > 0
                assert (tmp_path / "prediction/prediction.csv").read_text().splitlines() == ["Product,Year", "Alpha,2001"]
            elif mode == "supervisor_error":
                assert result["status"] == "failed" and "2001" in result["response"]
            elif mode == "malformed_table":
                assert result["status"] == "parse_error"
                assert not (tmp_path / "prediction/prediction.md").read_text()
                assert result["conversion"]["mode"] == "deterministic"
            else:
                assert result["status"] == "failed" and result["response"] == ""
                assert result["budget"]["final_model_requests"] == 1
            assert (tmp_path / "evidence_fragments.json").exists()
    asyncio.run(exercise())


def test_user_cancellation_does_not_start_final_model(tmp_path, monkeypatch):
    entered = asyncio.Event()

    class PausedGraph:
        async def astream(self, *args, **kwargs):
            entered.set()
            await asyncio.sleep(60)
            yield {}

    @contextmanager
    def no_model(cfg):
        yield lambda **kwargs: None

    monkeypatch.setattr(adapter, "model_scope", no_model)
    monkeypatch.setattr(adapter, "build_graph", lambda *args: (PausedGraph(), None))

    async def exercise():
        cfg = config(DEFAULT_CONFIG)
        service = Services(cfg, Journal(tmp_path))
        service.complete = AsyncMock()
        task = asyncio.create_task(adapter.run({"query": "Fixture"}, cfg, service, tmp_path))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        service.complete.assert_not_awaited()
        assert read_json(tmp_path / "budget.json")["total_model_requests"] == 0
    asyncio.run(exercise())


def test_workflow_timeout_still_exports_saved_evidence(tmp_path, monkeypatch):
    stores = []

    def store(*args):
        value = Retrieval(*args)
        stores.append(value)
        return value

    class PausedGraph:
        async def astream(self, *args, **kwargs):
            await stores[0].search(["fixture"])
            await asyncio.sleep(60)
            yield {}

    @contextmanager
    def no_model(cfg):
        yield lambda **kwargs: None

    monkeypatch.setattr(adapter, "Retrieval", store)
    monkeypatch.setattr(adapter, "model_scope", no_model)
    monkeypatch.setattr(adapter, "build_graph", lambda *args: (PausedGraph(), None))

    async def exercise():
        cfg = {**config(DEFAULT_CONFIG), "odr_workflow_timeout_seconds": .05}
        service = Services(cfg, Journal(tmp_path))
        service.search = AsyncMock(return_value=[{"url": "https://fixture.test/a", "snippet": "Alpha 2001"}])
        service.fetch = AsyncMock(return_value="Alpha launch year: 2001")
        service.complete = AsyncMock(return_value="| Product | Year |\n|---|---|\n| Alpha | 2001 |")
        result = await adapter.run({"query": "Product and Year"}, cfg, service, tmp_path)
        assert result["status"] == "timeout" and "2001" in result["response"]
        assert service.complete.await_count == 1
        assert read_json(tmp_path / "budget.json")["final_model_requests"] == 1
    asyncio.run(exercise())


def test_worker_route_and_opt_in_official_judging(tmp_path, monkeypatch):
    from widesearch_experiment.runners import batch
    seen = []

    async def fake(args, *pos, **kwargs):
        seen.append(args)
        return 0, False

    monkeypatch.setattr(batch, "subprocess_run", fake)
    original_hashes = batch.code_hashes
    cfg = config(DEFAULT_CONFIG)
    args = ["python", "-m", "widesearch_experiment.evaluation.official", "--instance", "fixture",
            "--output", str(tmp_path / "score.json")]
    with lite_runner(cfg):
        hashes = batch.code_hashes()
        assert "../widesearch_odr_lite/adapter.py" in hashes
        assert "../widesearch_fetch_backup/services.py" in hashes
        asyncio.run(batch.subprocess_run(["python", "-m", "widesearch_experiment.runners.worker"]))
        asyncio.run(batch.subprocess_run(args))
    assert seen[0][2] == "widesearch_odr_lite.worker"
    assert len(seen) == 1 and read_json(tmp_path / "score.json")["status"] == "skipped"
    with lite_runner({**cfg, "odr_evaluate": True}):
        asyncio.run(batch.subprocess_run(args))
    assert seen[-1] == args
    assert batch.code_hashes is original_hashes and batch.subprocess_run is fake
