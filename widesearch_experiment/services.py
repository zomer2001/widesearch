"""Common tools. This module cannot import the dataset, gold or scorer."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import textwrap
import time
from pathlib import Path
from urllib.parse import urlsplit

from .common import atomic_json, now, safe_error
from .model_runtime import response_completion, responses_options, sdk_client_options, uses_responses


class Journal:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.events = []
        self.started = time.monotonic()

    def emit(self, kind, **fields):
        with self.lock:
            event = {"seq": len(self.events) + 1, "time": now(),
                     "elapsed_seconds": round(time.monotonic() - self.started, 3), "kind": kind, **fields}
            self.events.append(event)
            with (self.directory / "events.jsonl").open("a") as f:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
                f.flush()
        return event

    def counts(self):
        research = [e for e in self.events if e["kind"] == "model_end" and e.get("stage") == "research"]
        calls = sum(e["kind"] == "model_start" and e.get("stage") == "research" for e in self.events)
        pages = [e for e in self.events if e["kind"] == "page_end" and e.get("success")]
        def total(key):
            return sum(e[key] for e in research) if len(research) == calls and all(e.get(key) is not None for e in research) else None
        return {"model_calls": calls, "input_tokens": total("input_tokens"), "output_tokens": total("output_tokens"),
                "search_calls": sum(e["kind"] == "search_start" for e in self.events),
                "pages_fetched": len(pages), "unique_pages_fetched": len({e["url"] for e in pages})}


class Services:
    def __init__(self, cfg, journal):
        self.cfg, self.journal = cfg, journal
        self.search_semaphore = asyncio.Semaphore(2)

    async def search(self, query, max_results=None):
        count = min(max_results or self.cfg["search_results"], self.cfg["search_results"])
        if self.cfg["search_provider"] == "deepseek":
            if isinstance(query, str) and query.strip() and len(query) > 128:
                # The Search API limits queries to 128 characters. Keep every
                # term, including long initial crawler tasks, without adding an
                # LLM planner. Each actual subquery is counted independently.
                parts = textwrap.wrap(query, width=128, break_on_hyphens=False)
                self.journal.emit("search_query_split", provider="deepseek", query=query, submitted_queries=parts)
                groups = [await self._deepseek_search(part, min(count, 50)) for part in parts]
                merged = {}
                for rank in range(count):
                    for group in groups:
                        if rank < len(group):
                            merged.setdefault(group[rank]["url"], group[rank])
                return list(merged.values())[:count]
            return await self._deepseek_search(query, min(count, 50))
        if self.cfg["search_provider"] != "openai_web_search":
            raise ValueError("Unsupported search provider")
        from openai import AsyncOpenAI
        event = self.journal.emit("search_start", query=query, requested_results=count)
        try:
            async with self.search_semaphore:
                async with AsyncOpenAI(timeout=self.cfg["search_timeout_seconds"], max_retries=0) as client:
                    r = await client.responses.create(
                        model=self.cfg["search_model"], input=query,
                        tools=[{"type": "web_search", "search_context_size": self.cfg["search_context_size"]}],
                        tool_choice="required", max_output_tokens=1000, max_tool_calls=1,
                        include=["web_search_call.action.sources"])
            payload = r.model_dump(mode="json")
            atomic_json(self.journal.directory / "search_receipts" / f"{event['seq']:05}.json", payload)
            calls = [x for x in payload["output"] if x.get("type") == "web_search_call" and x.get("status") == "completed"]
            if not calls:
                raise RuntimeError("Search tool was not completed")
            candidates = [s for c in calls for s in (c.get("action", {}).get("sources") or [])]
            for item in payload["output"]:
                if item.get("type") == "message":
                    candidates.extend(a for b in item.get("content", []) for a in (b.get("annotations") or [])
                                      if a.get("type") == "url_citation")
            urls = {}
            for x in candidates:
                url = x.get("url", "")
                p = urlsplit(url)
                if p.scheme in {"http", "https"} and p.hostname and not p.username and url not in urls:
                    urls[url] = {"url": url, "title": x.get("title") or url, "snippet": x.get("snippet", ""), "content": ""}
            results = list(urls.values())[:count]
            self.journal.emit("search_end", parent=event["seq"], query=query, results=results,
                              native_tool_calls=len(calls), usage=payload.get("usage"), served_model=payload.get("model"))
            return results
        except Exception as exc:
            self.journal.emit("search_error", parent=event["seq"], **safe_error(exc))
            raise

    async def _deepseek_request(self, tool, body, receipt_directory, seq):
        import httpx
        base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/").removesuffix("/v1")
        # This credential is deliberately separate from the OpenAI SDK aliases.
        headers = {"Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"]}
        async with httpx.AsyncClient(trust_env=False, timeout=self.cfg["search_timeout_seconds"]) as client:
            response = await client.post(base + f"/v1/tools/{tool}", json=body, headers=headers)
            response.raise_for_status()
            payload = response.json()
        atomic_json(self.journal.directory / receipt_directory / f"{seq:05}.json", payload)
        return payload

    async def _deepseek_search(self, query, count):
        # Do not silently discard task constraints to meet the API's length limit.
        if not isinstance(query, str) or not query.strip() or len(query) > 128:
            self.journal.emit("search_rejected", provider="deepseek", query=query, reason="query_length")
            raise ValueError("DeepSeek search requires 1–128 characters. Rewrite as a shorter search query.")
        event = self.journal.emit("search_start", provider="deepseek", query=query, requested_results=count)
        try:
            async with self.search_semaphore:
                payload = await self._deepseek_request("search", {"query": query, "count": count, "need_content": False},
                                                       "search_receipts", event["seq"])
            urls = {}
            for item in payload["results"]:
                url = item.get("url", "")
                parsed = urlsplit(url)
                if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
                    urls.setdefault(url, {"url": url, "title": item.get("title") or url,
                                          "snippet": item.get("summary") or item.get("snippet") or "", "content": ""})
            results = list(urls.values())[:count]
            self.journal.emit("search_end", parent=event["seq"], provider="deepseek", query=query,
                              results=results, api_requests=1, native_tool_calls=0, usage=None, served_model=None)
            return results
        except Exception as exc:
            self.journal.emit("search_error", parent=event["seq"], provider="deepseek", **safe_error(exc))
            raise

    async def deepseek_fetch(self, url):
        """Keep the title/status for SearchOS, and the raw API receipt for audit."""
        event = self.journal.emit("page_start", provider="deepseek", url=url)
        try:
            payload = await self._deepseek_request("fetch", {"url": url}, "fetch_receipts", event["seq"])
            content = payload["content"]
            ok = bool(content.strip())
            self.journal.emit("page_end", parent=event["seq"], provider="deepseek", url=url, success=ok,
                              status=200, api_requests=1)
            if ok:
                self.save_page(url, content)
            return {"title": payload.get("title", ""), "content": content, "status": 200,
                    "error": "" if ok else "empty_page"}
        except Exception as exc:
            error = safe_error(exc)
            self.journal.emit("page_end", parent=event["seq"], provider="deepseek", url=url, success=False,
                              status=error["status_code"], error=error)
            return {"title": "", "content": "", "status": error["status_code"] or 0, "error": error["type"]}

    async def fetch(self, url):
        """Shared page reader; native crawler baselines retain their own crawlers."""
        if self.cfg.get("fetch_provider") == "deepseek":
            return (await self.deepseek_fetch(url))["content"]
        import aiohttp
        import trafilatura
        self.journal.emit("page_start", url=url)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30), headers={"User-Agent": "Mozilla/5.0"}) as response:
                    raw = await response.content.read(4_000_000)
                    text = trafilatura.extract(raw, include_tables=True, include_links=True) or ""
                    ok = response.status == 200 and bool(text)
                    self.journal.emit("page_end", url=url, success=ok, status=response.status)
                    if ok:
                        self.save_page(url, text)
                    return text if ok else ""
        except Exception as exc:
            self.journal.emit("page_end", url=url, success=False, error=safe_error(exc))
            return ""

    def save_page(self, url, text):
        from .common import digest
        atomic_json(self.journal.directory / "pages" / f"{digest(url)[:20]}.json", {"url": url, "content": text})

    async def complete(self, prompt, *, stage="research", model=None):
        from openai import AsyncOpenAI
        event = self.journal.emit("model_start", stage=stage)
        async with AsyncOpenAI(timeout=self.cfg["model_timeout_seconds"], max_retries=0,
                               **sdk_client_options(self.cfg, asynchronous=True)) as client:
            if uses_responses(self.cfg):
                result = response_completion(await client.responses.create(
                    **responses_options(self.cfg, model), input=prompt))
            else:
                result = await client.chat.completions.create(
                    model=model or self.cfg["model"], temperature=self.cfg["temperature"], seed=self.cfg["seed"],
                    max_completion_tokens=self.cfg["max_output_tokens"], messages=[{"role": "user", "content": prompt}])
        usage = result.usage
        self.journal.emit("model_end", stage=stage, parent=event["seq"], served_model=result.model,
                          input_tokens=usage.prompt_tokens if usage else None, output_tokens=usage.completion_tokens if usage else None,
                          cached_input_tokens=getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None),
                          reasoning_tokens=getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None))
        response = result.choices[0].message.content or ""
        atomic_json(self.journal.directory / "model_responses" / f"{event['seq']:05}.json",
                    {"stage": stage, "response": response, "finish_reason": result.choices[0].finish_reason})
        return response


def langchain_callback(journal):
    from langchain_core.callbacks import BaseCallbackHandler

    class Meter(BaseCallbackHandler):
        def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
            journal.emit("model_start", stage="research", run_id=str(run_id))

        def on_llm_end(self, response, *, run_id, **kwargs):
            usage = (response.llm_output or {}).get("token_usage", {})
            if not usage and response.generations:
                msg = getattr(response.generations[0][0], "message", None)
                usage = getattr(msg, "usage_metadata", {}) or {}
            journal.emit("model_end", stage="research", run_id=str(run_id),
                         input_tokens=usage.get("prompt_tokens", usage.get("input_tokens")),
                         output_tokens=usage.get("completion_tokens", usage.get("output_tokens")),
                         cached_input_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens",
                             (usage.get("input_token_details") or {}).get("cache_read")),
                         reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens",
                             (usage.get("output_token_details") or {}).get("reasoning")))

        def on_llm_error(self, error, *, run_id, **kwargs):
            journal.emit("model_error", stage="research", run_id=str(run_id), error=safe_error(error))
    return Meter()
