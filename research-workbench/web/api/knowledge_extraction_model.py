"""Bounded, tool-free model calls for already-authorized archive extraction.

No settings or clients are loaded by a database read or by importing this module.
The selected provider is retained; failed requests are never sent to another one.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable


class ExtractionModelError(ValueError):
    """Messages are deliberately safe to persist in a public job record."""


def _decode(text: str) -> dict:
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text, flags=re.S | re.I)
    if fenced:
        text = fenced.group(1)
    try:
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError
        from api.knowledge_extraction import Extraction

        return Extraction.model_validate(value).model_dump()
    except (TypeError, ValueError):
        raise ExtractionModelError("模型返回的实体关系格式未通过校验，未作为知识发布。") from None


def make_extractor(
    engine: str,
    model: str,
    *,
    system_prompt=None,
    output_schema=None,
    decoder=None,
    reasoning_effort=None,
) -> Callable[[dict], dict]:
    if engine not in {"openai", "deepseek", "ollama", "cli"}:
        raise ExtractionModelError("自动入库的模型类型无效。")
    if not isinstance(model, str) or not model.strip():
        raise ExtractionModelError("自动入库尚未选择模型。")
    from api import workbench
    from api.knowledge_extraction import EXTRACTION_SCHEMA, SYSTEM_PROMPT

    config = workbench.load_config()
    endpoint = getattr(config, engine)
    custom = system_prompt is not None
    schema = output_schema if custom else EXTRACTION_SCHEMA
    decode = decoder or _decode
    messages_schema = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    prompt = (
        (system_prompt if custom else SYSTEM_PROMPT)
        + "\n只输出一个符合下列 JSON Schema 的 JSON 对象，不输出报告、Markdown 或解释。\n"
        + messages_schema
    )
    if engine == "cli":
        selection = workbench.Selection("cli", model.strip(), "")
        client = workbench.build_model(selection, "knowledge_extraction")
        if reasoning_effort is not None:
            client.reasoning_effort = reasoning_effort

        def extract(payload: dict) -> dict:
            from langchain_core.messages import HumanMessage, SystemMessage

            try:
                response = client.invoke(
                    [
                        SystemMessage(content=prompt),
                        HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
                    ],
                    tool_choice="none",
                )
                if response.tool_calls or not isinstance(response.content, str):
                    raise ExtractionModelError("抽取模型返回了工具请求或无效内容。")
                return decode(response.content)
            except ExtractionModelError:
                raise
            except Exception:
                raise ExtractionModelError("CLI 模型抽取未完成；可在自动入库中重试。") from None

        return extract

    from openai import OpenAI

    api_key = endpoint.api_key if engine in {"openai", "deepseek"} else "ollama"
    if not api_key:
        raise ExtractionModelError("所选抽取模型尚未配置密钥。")
    client = OpenAI(api_key=api_key, base_url=endpoint.api_base, timeout=90, max_retries=0)
    output_format = (
        {
            "type": "json_schema",
            "json_schema": {
                "name": "archive_entity_relations",
                "strict": True,
                "schema": EXTRACTION_SCHEMA,
            },
        }
        if engine == "openai" and not custom
        else {"type": "json_object"}
    )

    def extract(payload: dict) -> dict:
        try:
            response = client.chat.completions.create(
                model=model.strip(),
                temperature=0,
                max_tokens=2048 if engine == "ollama" else 6000,
                response_format=output_format,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
            )
            choice = response.choices[0]
            if choice.finish_reason != "stop" or choice.message.refusal:
                raise ExtractionModelError("模型输出被截断或拒绝，本段未入库。")
            return decode(choice.message.content or "")
        except ExtractionModelError:
            raise
        except Exception:
            raise ExtractionModelError(
                "所选模型抽取请求失败；原文已保留，可在自动入库中重试。"
            ) from None

    return extract
