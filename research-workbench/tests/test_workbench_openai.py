"""OpenAI uses independent credentials and task-scoped, secret-free metadata."""
import json
import stat
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))
from api import workbench


@pytest.fixture(autouse=True)
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(workbench, "CONFIG_PATH", tmp_path / "private.json")
    workbench.save_config(workbench.Configuration())


@pytest.mark.asyncio
async def test_openai_configuration_is_independent_and_public_view_never_exposes_key(monkeypatch):
    async def models(_):
        return [], ""
    monkeypatch.setattr(workbench, "ollama_models", models)
    saved = workbench.merged_config({
        "openai": {"api_key": "private-openai-value", "model": "gpt-4o"},
        "deepseek": {"api_key": "private-deepseek-value"}, "engine": "openai",
    })
    workbench.save_config(saved)
    public = await workbench.public_settings()
    assert public["openai"]["api_key_configured"] is True
    assert public["openai"]["model"] == "gpt-4o"
    assert public["engine"] == "openai"
    assert "private-openai-value" not in json.dumps(public)
    assert "private-deepseek-value" not in json.dumps(public)
    assert stat.S_IMODE(workbench.CONFIG_PATH.stat().st_mode) == 0o600
    kept = workbench.merged_config({"openai": {"api_key": " "}})
    assert kept.openai.api_key == "private-openai-value"
    assert kept.deepseek.api_key == "private-deepseek-value"


@pytest.mark.asyncio
async def test_missing_openai_key_does_not_fall_back_to_another_provider():
    saved = workbench.load_config()
    saved.deepseek.api_key = "other-provider-key"
    workbench.save_config(saved)
    with pytest.raises(HTTPException) as caught:
        await workbench.select_engine("openai")
    assert caught.value.status_code == 400
    assert "OpenAI" in caught.value.detail


@pytest.mark.asyncio
async def test_follow_up_restores_openai_model_and_endpoint_and_uses_only_openai_key(tmp_path):
    saved = workbench.load_config()
    saved.openai.api_key = "private-openai-value"
    saved.deepseek.api_key = "other-provider-key"
    workbench.save_config(saved)
    prior = {"engine": "openai", "engine_model": "gpt-4o-2024-08-06", "api_base": "https://api.openai.com/v1"}
    selection = await workbench.select_engine(None, prior)
    assert selection.model == prior["engine_model"]
    assert selection.api_key == "private-openai-value"
    model = workbench.build_model(selection, "orchestrator")
    assert model.model_name == prior["engine_model"]
    assert model.openai_api_base == prior["api_base"]
    assert model.openai_api_key.get_secret_value() == selection.api_key
    workbench.write_metadata(tmp_path, selection.metadata())
    assert workbench.read_metadata(tmp_path)["engine"] == "openai"
    assert "private-openai-value" not in (tmp_path / workbench.META_NAME).read_text()


def test_openai_key_is_redacted_from_errors():
    saved = workbench.load_config()
    saved.openai.api_key = "private-openai-value"
    workbench.save_config(saved)
    message = workbench.safe_error(RuntimeError("failed with private-openai-value"))
    assert "private-openai-value" not in message
