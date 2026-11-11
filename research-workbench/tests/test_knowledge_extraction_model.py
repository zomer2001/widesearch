"""The extraction adapter retains provider choice and never retries bad output."""

from types import SimpleNamespace

import pytest
from api import knowledge_extraction_model as adapter
from api import workbench


@pytest.fixture
def clients(monkeypatch):
    import openai

    requests, configurations = [], []
    outcome = {"text": '{"entities":[],"relations":[],"gaps":[]}', "finish": "stop"}

    def create(**kwargs):
        requests.append(kwargs)
        if "error" in outcome:
            raise RuntimeError(outcome["error"])
        message = SimpleNamespace(content=outcome["text"], refusal=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason=outcome["finish"])]
        )

    def client(**kwargs):
        configurations.append(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(openai, "OpenAI", client)
    configuration = workbench.Configuration(
        openai={"api_key": "sk-fixture-credential"},
        deepseek={"api_key": "sk-fixture-deepseek"},
        ollama={"model": "local-fixture"},
    )
    monkeypatch.setattr(workbench, "load_config", lambda: configuration)
    return requests, configurations, outcome


@pytest.mark.parametrize("engine", ["openai", "deepseek", "ollama"])
def test_provider_is_retained_bounded_and_tool_free(engine, clients):
    requests, configurations, _ = clients
    result = adapter.make_extractor(engine, "chosen-model")({"segments": [], "scope": "Anduril"})
    assert result == {"entities": [], "relations": [], "gaps": []}
    assert len(requests) == len(configurations) == 1
    assert configurations[0]["max_retries"] == 0
    assert configurations[0]["timeout"] == 90
    request = requests[0]
    assert request["model"] == "chosen-model" and "tools" not in request
    assert request["response_format"]["type"] == (
        "json_schema" if engine == "openai" else "json_object"
    )


@pytest.mark.parametrize(
    "text,finish",
    [('{"entities":[]', "length"), ("not-json", "stop"), ('{"unexpected":"field"}', "stop")],
)
def test_invalid_or_truncated_output_is_not_published_or_automatically_retried(
    text, finish, clients
):
    requests, _, outcome = clients
    outcome.update(text=text, finish=finish)
    with pytest.raises(adapter.ExtractionModelError):
        adapter.make_extractor("openai", "gpt-4o")({"segments": []})
    assert len(requests) == 1


def test_provider_error_does_not_expose_credentials_or_fallback(clients):
    requests, configurations, outcome = clients
    outcome["error"] = "upstream headers Authorization: Bearer sk-fixture-credential"
    with pytest.raises(adapter.ExtractionModelError) as raised:
        adapter.make_extractor("openai", "gpt-4o")({"segments": []})
    assert "sk-fixture" not in str(raised.value)
    assert len(requests) == len(configurations) == 1


def test_invalid_engine_never_loads_private_settings(monkeypatch):
    monkeypatch.setattr(workbench, "load_config", lambda: pytest.fail("must not load credentials"))
    with pytest.raises(adapter.ExtractionModelError):
        adapter.make_extractor("unknown", "model")


def test_cli_extractor_explicitly_sets_low_reasoning_without_changing_chat_settings(monkeypatch):
    from langchain_core.messages import AIMessage

    client = SimpleNamespace(
        reasoning_effort="high",
        invoke=lambda *a, **k: AIMessage(content='{"entities":[],"relations":[],"gaps":[]}'),
    )
    monkeypatch.setattr(workbench, "load_config", workbench.Configuration)
    selected = []

    def build(selection, role):
        selected.append((selection.engine, selection.model, role))
        return client

    monkeypatch.setattr(workbench, "build_model", build)
    adapter.make_extractor("cli", "gpt-6-luna", reasoning_effort="low")({"segments": []})
    assert client.reasoning_effort == "low"
    assert selected == [("cli", "gpt-6-luna", "knowledge_extraction")]
