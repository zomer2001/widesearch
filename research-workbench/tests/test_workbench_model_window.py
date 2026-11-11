"""Ollama budgets derive from the selected model's actual configured window."""

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

from api import workbench


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(workbench, "CONFIG_PATH", tmp_path / "private.json")
    config = workbench.Configuration()
    config.ollama.model = "research-qwen3-32k:latest"
    workbench.save_config(config)
    workbench._ollama_windows.clear()
    yield
    workbench._ollama_windows.clear()


def mock_http(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )


@pytest.mark.asyncio
async def test_show_parameter_overrides_architecture_and_other_loaded_models(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request.url.path)
        assert json.loads(request.content)["model"] == "research-qwen3-32k:latest"
        return httpx.Response(
            200,
            json={
                "parameters": "temperature 0\nnum_ctx                        32768\n",
                "model_info": {"qwen3.context_length": 262144},
            },
        )

    mock_http(monkeypatch, handle)
    result = await workbench.ollama_context_window(
        "http://127.0.0.1:11434/v1",
        "research-qwen3-32k:latest",
    )
    assert result["context_window"] == 32768
    assert result["context_window_source"] == "model_parameter"
    assert result["context_window_warning"] == ""
    assert calls == ["/api/show"]


@pytest.mark.asyncio
async def test_ps_matches_exact_model_and_returns_its_running_window(monkeypatch):
    def handle(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"model_info": {"qwen3.context_length": 262144}})
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "unrelated:latest", "context_length": 131072},
                    {"name": "selected:latest", "context_length": 8192},
                ]
            },
        )

    mock_http(monkeypatch, handle)
    result = await workbench.ollama_context_window("http://127.0.0.1:11434/v1", "selected")
    assert result["context_window"] == 8192
    assert result["context_window_source"] == "running_model"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 503])
async def test_unknown_window_is_conservative_and_has_visible_warning(monkeypatch, status):
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            status,
            json={
                "parameters": "num_ctx -1",
                "model_info": {"qwen3.context_length": 262144},
                "models": [{"name": "other", "context_length": 32768}],
            },
        ),
    )
    result = await workbench.ollama_context_window("http://127.0.0.1:11434/v1", "unknown")
    assert result["context_window"] == 4096
    assert result["context_window_source"] == "conservative_default"
    assert "4096" in result["context_window_warning"]


@pytest.mark.asyncio
async def test_follow_up_freezes_window_model_and_endpoint(monkeypatch, tmp_path):
    calls = []

    async def window(base, model):
        calls.append((base, model))
        return {
            "context_window": 32768,
            "context_window_source": "model_parameter",
            "context_window_warning": "",
        }

    monkeypatch.setattr(workbench, "ollama_context_window", window)
    first = await workbench.select_engine("ollama")
    workbench.write_metadata(tmp_path, first.metadata())
    restored = workbench.read_metadata(tmp_path)
    config = workbench.load_config()
    config.ollama.model = "different-model:latest"
    workbench.save_config(config)
    for engine in (None, "ollama"):
        next_turn = await workbench.select_engine(engine, restored)
        assert next_turn.model == first.model
        assert next_turn.context_window == 32768
        assert next_turn.context_window_source == "model_parameter"
    assert calls == [(first.api_base, first.model)]
    model = workbench.build_model(next_turn, "sub_agent")
    assert model.profile["max_input_tokens"] == 32768


@pytest.mark.asyncio
async def test_settings_surface_actual_window_and_allow_echoing_readonly_fields(monkeypatch):
    async def installed(_config):
        return [{"id": "research-qwen3-32k:latest", "label": "research-qwen3-32k:latest"}], ""

    monkeypatch.setattr(workbench, "ollama_models", installed)
    mock_http(monkeypatch, lambda _: httpx.Response(200, json={"parameters": "num_ctx 32768"}))
    settings = await workbench.public_settings()
    assert settings["ollama"]["context_window"] == 32768
    assert (
        "32768" in next(item for item in settings["engines"] if item["id"] == "ollama")["message"]
    )
    saved = workbench.merged_config({"ollama": settings["ollama"]})
    assert saved.ollama.model == "research-qwen3-32k:latest"
