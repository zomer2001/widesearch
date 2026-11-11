import asyncio
import os
import sys

from ..common import ROOT, atomic_json
from ..services import Services
from ..model_runtime import response_completion, responses_options, sdk_client_options, uses_responses


async def run(task, cfg, services, workdir):
    sys.path.insert(0, str(ROOT / "vendor/mindsearch"))
    import lagent.actions.web_browser as browser
    from lagent.llms import GPTAPI
    from openai import OpenAI
    from mindsearch.agent import init_agent, models
    import mindsearch.agent as factory
    from functools import partial
    from lagent.prompts import PluginParser, InterpreterParser

    # GPT may put JSON immediately after the marker. Whitespace-tolerant
    # delimiters keep the same official parser and tool JSON validation.
    factory.PluginParser = partial(PluginParser, begin="<|action_start|><|plugin|>", end="<|action_end|>")
    factory.InterpreterParser = partial(InterpreterParser, begin="<|action_start|><|interpreter|>", end="<|action_end|>")
    import mindsearch.agent.graph as graph_module
    import re
    import types
    # Official ExecutionAction uses a non-greedy ')' regex on Python source,
    # which truncates quoted node IDs containing parentheses. Only repair the
    # literal-name parser, leaving exec, graph scheduling and searches unchanged.
    def findall(pattern, string, *args, **kwargs):
        if pattern == r"graph.node\((.*?)\)":
            return re.findall(r'''graph\.node\(\s*("[^"]*"|'[^']*')\s*\)''', string)
        return re.findall(pattern, string, *args, **kwargs)
    graph_module.re = types.SimpleNamespace(**{name: getattr(re, name) for name in dir(re) if name != "findall"}, findall=findall)
    import mindsearch.agent.mindsearch_agent as mind_impl
    import json
    original_references = mind_impl._generate_references_from_graph

    def compatible_references(graph):
        try:
            return original_references(graph)
        except (IndexError, AssertionError, KeyError):
            # The upstream fixed memory[2] assumption fails for a node that
            # answers without a tool call. Keep its real response, with no
            # fabricated citation; collect URLs only from real tool returns.
            services.journal.emit("compatibility", fix="mindsearch_reference_memory_layout")
            offset, references, urls = 0, [], {}
            for name, node in graph.items():
                if name in {"root", "response"}:
                    continue
                source_map = {}
                for message in node.get("memory", {}).get("agent.memory", []):
                    if not message.get("sender", "").endswith("ActionExecutor"):
                        continue
                    try:
                        content = json.loads(message.get("content", ""))
                        if isinstance(content, dict) and all(str(k).isdigit() and isinstance(v, dict) and v.get("url") for k, v in content.items()):
                            source_map = {int(k): v for k, v in content.items()}
                            break
                    except (ValueError, TypeError):
                        pass
                response = node.get("response", {}).get("content", "")
                updated, mapped, added = mind_impl._update_ref(response, source_map, offset)
                references.append(f"## {node.get('content', '')}\n\n{updated}")
                urls.update(mapped)
                offset += added
            return "\n\n".join(references), urls
    mind_impl._generate_references_from_graph = compatible_references

    class CommonSearch:
        def __init__(self, topk=6, **kwargs):
            self.topk = topk

        def search(self, query):
            rows = asyncio.run(Services(cfg, services.journal).search(query, self.topk))
            return {i: {"url": r["url"], "title": r["title"], "summ": r["snippet"]} for i, r in enumerate(rows)}

    class CommonFetcher:
        def __init__(self, **kwargs):
            pass

        def fetch(self, url):
            text = asyncio.run(Services(cfg, services.journal).fetch(url))
            return bool(text), text

    class MeasuredGPT(GPTAPI):
        # The official GPTAPI prompt parser and stream protocol remain intact.
        # Only its HTTP transport is replaced to make usage and failures observable.
        def _stream_chat(self, messages, **gen_params):
            event = services.journal.emit("model_start", stage="research")
            usage = None
            text = ""
            try:
                with OpenAI(timeout=cfg["model_timeout_seconds"], max_retries=0, **sdk_client_options(cfg)) as client:
                    if uses_responses(cfg):
                        with client.responses.create(**responses_options(cfg), input=messages, stream=True) as stream:
                            for chunk in stream:
                                if chunk.type == "response.output_text.delta":
                                    text += chunk.delta
                                    yield chunk.delta
                                elif chunk.type == "response.completed":
                                    usage = response_completion(chunk.response).usage
                                elif chunk.type in {"response.failed", "response.incomplete", "error"}:
                                    raise RuntimeError(f"Model stream did not complete: {chunk.type}")
                        if usage is None:
                            raise RuntimeError("Model stream ended without a completed response")
                        return
                    with client.chat.completions.create(
                        model=cfg["model"], messages=messages, stream=True,
                        stream_options={"include_usage": True}, temperature=cfg["temperature"],
                        seed=cfg["seed"], max_completion_tokens=cfg["max_output_tokens"]) as stream:
                        for chunk in stream:
                            if chunk.usage:
                                usage = chunk.usage
                            if chunk.choices:
                                piece = chunk.choices[0].delta.content or ""
                                text += piece
                                yield piece
            finally:
                services.journal.emit("model_end", stage="research", parent=event["seq"],
                                      input_tokens=usage.prompt_tokens if usage else None,
                                      output_tokens=usage.completion_tokens if usage else None)
                atomic_json(workdir / "model_responses" / f"{event['seq']:05}.json", {"stage": "research", "response": text})

    browser.UnifiedExperimentSearch = CommonSearch
    browser.ContentFetcher = CommonFetcher
    models.experiment = {"type": MeasuredGPT, "model_type": cfg["model"], "key": os.environ["OPENAI_API_KEY"],
                         "api_base": os.environ["OPENAI_BASE_URL"].rstrip("/") + "/chat/completions",
                         "temperature": cfg["temperature"], "max_new_tokens": cfg["max_output_tokens"]}

    def execute():
        agent = init_agent(lang="cn" if task["language"] == "zh" else "en",
                           model_format="experiment", search_engine="UnifiedExperimentSearch", use_async=False)
        final = None
        with (workdir / "mindsearch_events.jsonl").open("w") as f:
            for message in agent(task["query"]):
                final = message
                f.write(message.model_dump_json() + "\n")
                f.flush()
        if final is None:
            raise RuntimeError("MindSearch returned no response")
        atomic_json(workdir / "workflow_result.json", final.model_dump())
        return {"response": final.content if isinstance(final.content, str) else str(final.content), "status": "success"}
    return await asyncio.to_thread(execute)
