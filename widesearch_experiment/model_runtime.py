"""Model transport compatibility; research prompts and search remain unchanged."""
from __future__ import annotations


def uses_responses(cfg):
    return cfg.get("model_api") == "responses"


def sdk_client_options(cfg, *, asynchronous=False):
    """DeepSeek connects directly, independent of the Mac's OpenAI proxy."""
    if cfg.get("model_provider") != "deepseek":
        return {}
    import httpx
    client = httpx.AsyncClient if asynchronous else httpx.Client
    return {"http_client": client(trust_env=False)}


def responses_options(cfg, model=None):
    return {"model": model or cfg["model"],
            "reasoning": {"effort": cfg["reasoning_effort"]},
            "max_output_tokens": cfg["max_output_tokens"], "store": False}


def langchain_options(cfg):
    options = {}
    if cfg.get("model_provider") == "deepseek":
        options.update(http_client=sdk_client_options(cfg)["http_client"],
                       http_async_client=sdk_client_options(cfg, asynchronous=True)["http_client"])
    if uses_responses(cfg):
        options.update({"use_responses_api": True, "reasoning": {"effort": cfg["reasoning_effort"]},
                "temperature": None, "seed": None,
                "output_version": "v0"})
        if cfg.get("model_provider") != "deepseek":
            options["store"] = False
            options["include"] = ["reasoning.encrypted_content"]
        # DeepSeek is always stateless. Omit store=False here because LangChain
        # otherwise drops the plaintext reasoning required by DeepSeek on replay.
    else:
        options.update(temperature=cfg["temperature"], seed=cfg["seed"])
    return options


def content_text(content):
    if isinstance(content, str):
        return content
    return "".join(x if isinstance(x, str) else x.get("text", "")
                   for x in content or [] if isinstance(x, str) or x.get("type") in {"text", "output_text"})


def response_completion(response):
    """Preserve the completion contract expected by Crawl4AI and accounting."""
    from openai.types.chat import ChatCompletion
    if response.status != "completed":
        raise RuntimeError(f"Model response did not complete: {response.status}")
    usage = response.usage
    data = None
    if usage:
        data = {"prompt_tokens": usage.input_tokens, "completion_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
                "prompt_tokens_details": {"cached_tokens": getattr(usage.input_tokens_details, "cached_tokens", None)},
                "completion_tokens_details": {"reasoning_tokens": getattr(usage.output_tokens_details, "reasoning_tokens", None)}}
    return ChatCompletion.model_validate({"id": response.id, "created": int(response.created_at),
        "object": "chat.completion", "model": response.model, "usage": data,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": response.output_text}}]})


def searchos_model(cfg, **kwargs):
    from langchain_openai import ChatOpenAI
    options = {**langchain_options(cfg), **kwargs}
    if not uses_responses(cfg):
        return ChatOpenAI(model=cfg["model"], **options)

    class TextResponsesModel(ChatOpenAI):
        """SearchOS parsers require text; retain original blocks for tool round trips."""
        def _get_request_payload(self, input_, *, stop=None, **request_kwargs):
            messages = self._convert_input(input_).to_messages()
            restored = []
            for message in messages:
                blocks = message.additional_kwargs.get("experiment_response_content")
                restored.append(message.model_copy(update={"content": blocks}) if blocks is not None else message)
            payload = super()._get_request_payload(restored, stop=stop, **request_kwargs)
            if cfg.get("model_provider") == "deepseek":
                choice = payload.get("tool_choice")
                non_thinking_tool_turn = False
                for message in restored:
                    if message.type == "human":
                        non_thinking_tool_turn = False
                    elif message.type == "ai" and message.tool_calls:
                        reasoning = message.additional_kwargs.get("reasoning") or {}
                        if not reasoning.get("content"):
                            non_thinking_tool_turn = True
                # DeepSeek rejects forced tool choices in thinking mode. Keep
                # that contract and finish its tool turn without inventing the
                # reasoning text required when switching back to thinking mode.
                if non_thinking_tool_turn or (choice is not None and choice != "auto" and choice != "none"):
                    payload["reasoning"] = {"effort": "none"}
            return payload

        @staticmethod
        def _text_result(result):
            for generation in result.generations:
                message = generation.message
                if isinstance(message.content, list):
                    message.additional_kwargs["experiment_response_content"] = message.content
                    message.content = content_text(message.content)
            return result

        def _generate(self, *args, **request_kwargs):
            return self._text_result(super()._generate(*args, **request_kwargs))

        async def _agenerate(self, *args, **request_kwargs):
            return self._text_result(await super()._agenerate(*args, **request_kwargs))

    return TextResponsesModel(model=cfg["model"], disable_streaming=True, **options)
