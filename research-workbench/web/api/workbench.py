"""Private engine configuration and immutable per-run model selection.

Credentials remain in a mode-0600 file in this checkout. Public responses and
session metadata are built from explicit allowlists, never from this file.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

Engine = Literal["deepseek", "openai", "ollama", "cli"]
SearchProviderName = Literal["ddgs", "serper", "tavily", "openai_web_search"]
ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / ".workbench" / "private.json"
META_NAME = ".workbench-engine.json"
_ollama_windows: dict[tuple[str, str], dict] = {}


class Endpoint(BaseModel, extra="forbid"):
    model: str = Field(default="", max_length=200)
    api_base: str

    @field_validator("api_base")
    @classmethod
    def endpoint_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("服务地址必须为不带账号、查询参数的 HTTP(S) 地址")
        return value.rstrip("/")


class DeepSeek(Endpoint):
    model: str = "deepseek-v4-flash"
    api_base: str = "https://api.deepseek.com"
    api_key: str = Field(default="", repr=False, max_length=4096)


class OpenAIEndpoint(Endpoint):
    model: str = "gpt-4o"
    api_base: str = "https://api.openai.com/v1"
    api_key: str = Field(default="", repr=False, max_length=4096)


class Ollama(Endpoint):
    api_base: str = "http://127.0.0.1:11434/v1"


class CLI(BaseModel, extra="forbid"):
    model: str = Field(default="gpt-6-luna", min_length=1, max_length=200)


class Search(BaseModel, extra="forbid"):
    provider: SearchProviderName = "ddgs"
    serper_api_key: str = Field(default="", repr=False, max_length=4096)
    tavily_api_key: str = Field(default="", repr=False, max_length=4096)
    web_search_model: str = Field(default="gpt-4o", min_length=1, max_length=200)
    web_search_allowed_domains: list[str] = Field(default_factory=list, max_length=100)
    browser_backend: Literal["auto", "aiohttp", "crawl4ai", "trafilatura", "jina"] = "auto"

    @field_validator("web_search_model")
    @classmethod
    def search_model_name(cls, value: str) -> str:
        if not value.strip() or re.search(r"\s|[\x00-\x1f]", value.strip()):
            raise ValueError("搜索模型名称无效")
        return value.strip()

    @field_validator("web_search_allowed_domains")
    @classmethod
    def search_domains(cls, values: list[str]) -> list[str]:
        domains = []
        for value in values:
            domain = value.strip().lower()
            if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain):
                raise ValueError("限定网站必须是不含路径的域名")
            if domain not in domains:
                domains.append(domain)
        return domains


class Configuration(BaseModel, extra="forbid"):
    engine: Engine = "ollama"
    deepseek: DeepSeek = Field(default_factory=DeepSeek)
    openai: OpenAIEndpoint = Field(default_factory=OpenAIEndpoint)
    ollama: Ollama = Field(default_factory=Ollama)
    cli: CLI = Field(default_factory=CLI)
    search: Search = Field(default_factory=Search)


def load_config() -> Configuration:
    if not CONFIG_PATH.exists():
        return Configuration()
    try:
        return Configuration.model_validate_json(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(500, "本地配置无法读取，请检查 .workbench/private.json") from None


def save_config(config: Configuration) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix="private-", dir=CONFIG_PATH.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(config.model_dump_json(indent=2))
        os.replace(temporary, CONFIG_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def merged_config(patch: dict) -> Configuration:
    current = load_config().model_dump()
    # Read-only presence fields may be echoed back by a settings form.
    allowed = {"engine", "deepseek", "openai", "ollama", "cli", "search"}
    if set(patch) - allowed:
        raise HTTPException(422, "包含未知设置项")
    for key, value in patch.items():
        if key == "engine":
            current[key] = value
        elif isinstance(value, dict):
            for field, setting in value.items():
                if field.endswith("_configured") or field in {
                    "available",
                    "context_window",
                    "context_window_source",
                    "context_window_warning",
                }:
                    continue
                if field in {"api_key", "serper_api_key", "tavily_api_key"}:
                    if not isinstance(setting, str):
                        raise HTTPException(422, "密钥必须是文本")
                    if not setting.strip():
                        continue
                    setting = setting.strip()
                current[key][field] = setting
        else:
            raise HTTPException(422, "设置格式无效")
    try:
        return Configuration.model_validate(current)
    except ValueError:
        # Pydantic errors can include the invalid input, including a secret.
        raise HTTPException(422, "设置无效，请检查引擎、模型名称和服务地址") from None


async def ollama_models(config: Configuration) -> tuple[list[dict[str, str]], str]:
    base = config.ollama.api_base.removesuffix("/v1")
    try:
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            response = await client.get(f"{base}/api/tags")
            response.raise_for_status()
            data = response.json()
        models = [
            {"id": item["name"], "label": item["name"]}
            for item in data.get("models", [])
            if isinstance(item.get("name"), str)
        ]
        models.sort(key=lambda item: ("instruct" not in item["id"].lower(), item["id"]))
        return models, "" if models else "Ollama 中还没有已安装模型"
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return [], "无法连接 Ollama，请启动本机服务并检查地址"


async def ollama_context_window(api_base: str, model: str) -> dict:
    """Read this model's configured/effective window, never its architectural maximum."""
    base = api_base.removesuffix("/v1").rstrip("/")
    result = {
        "context_window": 4096,
        "context_window_source": "conservative_default",
        "context_window_warning": (
            "尚未取得该模型的上下文配置，暂按 4096 处理；"
            "建议使用明确配置上下文容量的模型"
        ),
    }

    def window(value):
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if not isinstance(value, bool) and 512 <= parsed <= 16_777_216 else None

    def configured(value, origin):
        return {
            "context_window": value,
            "context_window_source": origin,
            "context_window_warning": "",
        }

    async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
        try:
            response = await client.post(f"{base}/api/show", json={"model": model})
            response.raise_for_status()
            parameters = response.json().get("parameters", "")
            if isinstance(parameters, dict):
                value = window(parameters.get("num_ctx"))
            else:
                match = re.search(r"(?m)^\s*num_ctx\s+(\d+)\s*$", str(parameters))
                value = window(match.group(1)) if match else None
            if value:
                result = configured(value, "model_parameter")
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            pass
        if result["context_window_source"] == "conservative_default":
            try:
                response = await client.get(f"{base}/api/ps")
                response.raise_for_status()

                def canonical(name):
                    name = str(name)
                    return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"

                for running in response.json().get("models", []):
                    if canonical(running.get("name") or running.get("model")) != canonical(model):
                        continue
                    value = window(running.get("context_length"))
                    if value:
                        result = configured(value, "running_model")
                        break
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                pass
    _ollama_windows[(api_base, model)] = result.copy()
    return result


async def public_settings(config: Configuration | None = None) -> dict:
    config = config or load_config()
    installed, error = await ollama_models(config)
    model = config.ollama.model or (installed[0]["id"] if installed else "")
    installed_ids = {item["id"] for item in installed}
    cli_available = shutil.which("codex") is not None
    ollama_available = bool(model and model in installed_ids)
    ds_available = bool(config.deepseek.api_key)
    openai_available = bool(config.openai.api_key)
    window = await ollama_context_window(config.ollama.api_base, model) if ollama_available else {}
    return {
        "engine": config.engine,
        "deepseek": {
            "model": config.deepseek.model,
            "api_base": config.deepseek.api_base,
            "api_key_configured": ds_available,
        },
        "openai": {
            "model": config.openai.model,
            "api_base": config.openai.api_base,
            "api_key_configured": openai_available,
        },
        "ollama": {"model": model, "api_base": config.ollama.api_base, **window},
        "cli": {"model": config.cli.model, "available": cli_available},
        "search": {
            "provider": config.search.provider,
            "serper_key_configured": bool(config.search.serper_api_key),
            "tavily_key_configured": bool(config.search.tavily_api_key),
            "openai_key_configured": openai_available,
            "web_search_model": config.search.web_search_model,
            "web_search_allowed_domains": config.search.web_search_allowed_domains,
            "browser_backend": config.search.browser_backend,
        },
        "engines": [
            {
                "id": "openai",
                "label": "OpenAI API",
                "available": openai_available,
                "message": "已配置密钥，点击检查验证连接"
                if openai_available
                else "请先填写 OpenAI API 密钥",
            },
            {
                "id": "deepseek",
                "label": "DeepSeek API",
                "available": ds_available,
                "message": "已配置密钥，点击检查验证连接"
                if ds_available
                else "请先填写 DeepSeek API 密钥",
            },
            {
                "id": "ollama",
                "label": "Ollama",
                "available": ollama_available,
                "message": (
                    window.get("context_window_warning")
                    or f"已发现本地模型，上下文容量 {window.get('context_window', 4096)}"
                )
                if ollama_available
                else error or "所选模型尚未安装",
            },
            {
                "id": "cli",
                "label": "CLI · 6-luna",
                "available": cli_available,
                "message": "已发现 CLI，点击检查验证登录与模型权限"
                if cli_available
                else "未找到 Codex CLI",
            },
        ],
        "ollama_models": installed,
    }


def read_metadata(workspace: Path) -> dict:
    from api.innovation import read_manifest

    modules = {"module_config": read_manifest(workspace)}
    try:
        data = json.loads((workspace / META_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return modules
    if not isinstance(data, dict) or data.get("engine") not in {"deepseek", "openai", "ollama", "cli"}:
        return modules
    return {
        **modules,
        **{
            key: data[key]
            for key in (
                "engine",
                "engine_model",
                "api_base",
                "status",
                "error",
                "execution_error",
                "context_window",
                "context_window_source",
                "context_window_warning",
            )
            if key in data
        },
    }


def write_metadata(workspace: Path, metadata: dict) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / META_NAME
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


@dataclass(frozen=True)
class Selection:
    engine: Engine
    model: str
    api_base: str
    api_key: str = field(default="", repr=False)
    context_window: int = 0
    context_window_source: str = ""
    context_window_warning: str = ""

    def metadata(self) -> dict:
        metadata = {"engine": self.engine, "engine_model": self.model, "api_base": self.api_base}
        if self.context_window:
            metadata.update(
                context_window=self.context_window,
                context_window_source=self.context_window_source,
                context_window_warning=self.context_window_warning,
            )
        return metadata


async def select_engine(engine: Engine | None = None, previous: dict | None = None) -> Selection:
    config = load_config()
    previous = previous or {}
    chosen = engine or previous.get("engine") or config.engine
    # A same-engine follow-up keeps the model, endpoint and effective window,
    # including clients that explicitly echo the engine of the prior turn.
    retain_previous = previous.get("engine") == chosen
    settings = getattr(config, chosen)
    model = previous.get("engine_model") if retain_previous else settings.model
    base = previous.get("api_base", "") if retain_previous else getattr(settings, "api_base", "")
    if chosen == "ollama" and not model:
        installed, error = await ollama_models(config)
        if not installed:
            raise HTTPException(400, error)
        model = installed[0]["id"]
    if not model:
        raise HTTPException(400, "请先选择模型")
    if chosen == "deepseek" and not config.deepseek.api_key:
        raise HTTPException(400, "请先在设置中填写 DeepSeek API 密钥")
    if chosen == "openai" and not config.openai.api_key:
        raise HTTPException(400, "请先在设置中填写 OpenAI API 密钥")
    if chosen == "cli" and shutil.which("codex") is None:
        raise HTTPException(400, "未找到 Codex CLI，请先安装并登录")
    window = {}
    if chosen == "ollama":
        if (
            retain_previous
            and isinstance(previous.get("context_window"), int)
            and previous["context_window"] >= 512
        ):
            window = {
                key: previous.get(key, "")
                for key in (
                    "context_window",
                    "context_window_source",
                    "context_window_warning",
                )
            }
        else:
            window = await ollama_context_window(base, model)
    return Selection(
        chosen, model, base, settings.api_key if chosen in {"deepseek", "openai"} else "", **window
    )


def build_model(selection: Selection, role: str):
    from searchos.util.token_tracker import TokenTrackingCallback

    callbacks = [TokenTrackingCallback(role=role)]
    if selection.engine == "cli":
        from searchos.config.cli_model import CodexCLIChatModel

        return CodexCLIChatModel(
            model=selection.model,
            callbacks=callbacks,
            work_root=str(ROOT),
            timeout_s=180,
            # Conservative bridge byte ceiling, independent of P2's token threshold.
            max_input_tokens=200000,
        )
    from langchain_openai import ChatOpenAI

    context_window = (selection.context_window or 4096) if selection.engine == "ollama" else 128000
    return ChatOpenAI(
        model=selection.model,
        base_url=selection.api_base,
        api_key=selection.api_key or "ollama",
        temperature=0,
        max_tokens=2048 if selection.engine == "ollama" else 8192,
        timeout=180,
        max_retries=1,
        callbacks=callbacks,
        stream_usage=True,
        # Buffer OpenAI responses: the current network path can truncate SSE
        # after HTTP 200. The app still reports tool progress and saves the full
        # message; no partial generation is mistaken for a completed answer.
        disable_streaming=selection.engine == "openai",
        profile={
            "max_input_tokens": context_window,
            "max_output_tokens": 2048 if selection.engine == "ollama" else 8192,
            "tool_calling": True,
            "image_inputs": False,
        },
    )


def build_models(selection: Selection) -> dict:
    from searchos.config.model_purposes import ROLE_NAMES

    return {role: build_model(selection, role) for role in ROLE_NAMES}


def configure_search(*, required: bool = False) -> None:
    from searchos.config.settings import settings
    from searchos.tools.simple_browser.state import set_browser_provider

    config = load_config()
    search = config.search
    settings.browser_backend = search.browser_backend
    settings.serper_api_key = search.serper_api_key
    settings.tavily_api_key = search.tavily_api_key
    set_browser_provider(search_provider(search, required=required, openai=config.openai))


def search_provider(search: Search, *, required: bool = True, openai: OpenAIEndpoint | None = None):
    if search.provider == "openai_web_search":
        endpoint = openai if openai is not None else load_config().openai
        if not endpoint.api_key:
            if required:
                raise HTTPException(400, "OpenAI web_search 需要已保存的 OpenAI 密钥，请到 OpenAI API 设置中配置")
            return None
        from searchos.tools.simple_browser.search.openai_web_search import OpenAIWebSearchProvider
        return OpenAIWebSearchProvider(
            api_key=endpoint.api_key, api_base=endpoint.api_base,
            model=search.web_search_model, allowed_domains=search.web_search_allowed_domains,
        )
    if search.provider == "ddgs":
        from searchos.tools.simple_browser.search.ddgs import DDGSProvider
        return DDGSProvider()
    key = search.serper_api_key if search.provider == "serper" else search.tavily_api_key
    if not key:
        if required:
            raise HTTPException(400, "当前搜索服务缺少密钥：请在设置中选择“免密钥搜索（DDGS）”，或填写 Serper/Tavily 密钥；本地资料无需搜索密钥")
        return None
    if search.provider == "serper":
        from searchos.tools.simple_browser.search.serper import SerperProvider
        return SerperProvider(api_key=key)
    from searchos.tools.simple_browser.search.tavily import TavilyProvider
    return TavilyProvider(api_key=key)


def safe_error(exc: Exception) -> str:
    message = str(exc)
    config = load_config()
    for secret in (
        config.deepseek.api_key,
        config.openai.api_key,
        config.search.serper_api_key,
        config.search.tavily_api_key,
    ):
        if secret:
            message = message.replace(secret, "[已隐藏]")
    message = re.sub(r"sk-[\w-]+", "[已隐藏]", message)
    return message[:500] or type(exc).__name__


async def initialize() -> None:
    from api.deps import WORKSPACE_ROOT
    from searchos.config.model_context import set_default_model_factory
    from searchos.config.settings import settings

    for path in Path(WORKSPACE_ROOT).glob(f"*/{META_NAME}"):
        metadata = read_metadata(path.parent)
        if metadata.get("status") == "running":
            write_metadata(
                path.parent,
                {**metadata, "status": "error", "error": "服务已重启，请重新运行本任务"},
            )

    config = load_config()
    if not config.ollama.model:
        installed, _ = await ollama_models(config)
        if installed:
            config.ollama.model = installed[0]["id"]
            save_config(config)
    # A private single-machine demo starts with bounded, serial workers.
    settings.max_parallel_agents = 1
    settings.extraction_flush_concurrency = 1
    settings.enable_skills = False
    settings.enable_skill_router = False
    settings.enable_access_skill_generation = False
    settings.default_max_time_s = 600
    configure_search()

    def default_model(role: str):
        current = load_config()
        endpoint = getattr(current, current.engine)
        if current.engine == "deepseek" and not current.deepseek.api_key:
            raise RuntimeError("请先填写 DeepSeek API 密钥")
        if current.engine == "openai" and not current.openai.api_key:
            raise RuntimeError("请先填写 OpenAI API 密钥")
        if not endpoint.model:
            raise RuntimeError("请先选择已安装的 Ollama 模型")
        return build_model(
            Selection(
                current.engine,
                endpoint.model,
                getattr(endpoint, "api_base", ""),
                endpoint.api_key if current.engine in {"deepseek", "openai"} else "",
                **(
                    _ollama_windows.get((endpoint.api_base, endpoint.model), {})
                    if current.engine == "ollama"
                    else {}
                ),
            ),
            role,
        )

    set_default_model_factory(default_model)


async def probe(engine: Engine) -> dict:
    try:
        selection = await select_engine(engine)
        model = build_model(selection, "orchestrator")
        response = await asyncio.wait_for(model.ainvoke("Reply with exactly OK."), timeout=180)
        if not getattr(response, "content", ""):
            return {"ok": False, "message": "模型未返回文字，请检查模型与接口设置"}
        return {
            "ok": True,
            "message": f"{selection.model} 连接成功；研究工具链将在任务中验证"
            + (f"。{selection.context_window_warning}" if selection.context_window_warning else ""),
            **selection.metadata(),
        }
    except HTTPException as exc:
        return {"ok": False, "message": str(exc.detail)}
    except Exception as exc:
        return {"ok": False, "message": safe_error(exc)}
