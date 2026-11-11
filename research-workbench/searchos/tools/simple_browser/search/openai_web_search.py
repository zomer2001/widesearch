"""Responses web_search discovery; returned text is never page evidence."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from searchos.tools.simple_browser.search.base import SearchProvider, SearchResult

DEFAULT_RECEIPT_DIR = Path(__file__).resolve().parents[4] / "data/web_search_receipts"


def normalize_source_url(value) -> str:
    if not isinstance(value, str) or re.search(r"\s|[<>]|\.\.\.", value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
        _ = parsed.port  # Reject malformed ports as well as malformed hosts.
        query = parse_qsl(parsed.query, keep_blank_values=True)
        tracking = {"trk", "fbclid", "gclid", "dclid", "msclkid", "yclid", "igshid", "mc_cid", "mc_eid", "_ga", "_gl"}
        retained = [(key, item) for key, item in query
                    if not key.lower().startswith("utm_") and key.lower() not in tracking]
        cleaned_query = urlencode(retained) if len(retained) != len(query) else parsed.query
        # Fragments identify a passage, not a separate discovery URL.
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", cleaned_query, ""))
    except ValueError:
        return ""


def _safe_payload(value, secret: str = ""):
    if isinstance(value, dict):
        return {key: _safe_payload(item, secret) for key, item in value.items()
                if not re.search(r"api[_-]?key|authorization|headers?|secret|password|access_token", key, re.I)}
    if isinstance(value, list):
        return [_safe_payload(item, secret) for item in value]
    if isinstance(value, str):
        value = value.replace(secret, "[REDACTED]") if secret else value
        return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", value)
    return value


def parse_discovery_response(payload: dict, max_results: int = 10) -> list[SearchResult]:
    """Use only actual tool sources and URL-citation annotations, never prose URLs."""
    output = payload.get("output", [])
    calls = [item for item in output if isinstance(item, dict) and item.get("type") == "web_search_call"]
    if not any(item.get("status") == "completed" for item in calls):
        raise RuntimeError("OpenAI did not complete a web_search_call; no discovery sources accepted")
    candidates = []
    for call in calls:
        if call.get("status") != "completed":
            continue
        action = call.get("action", {})
        if isinstance(action, dict):
            candidates.extend(source for source in action.get("sources", []) if isinstance(source, dict))
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for block in item.get("content", []):
            if not isinstance(block, dict):
                continue
            candidates.extend(annotation for annotation in block.get("annotations", [])
                              if isinstance(annotation, dict) and annotation.get("type") == "url_citation")
    merged: dict[str, SearchResult] = {}
    for source in candidates:
        url = normalize_source_url(source.get("url"))
        if not url:
            continue
        title = source.get("title") if isinstance(source.get("title"), str) else ""
        # sources may carry a provider snippet; generated answer text is never copied.
        snippet = source.get("snippet") if isinstance(source.get("snippet"), str) else ""
        if url in merged:
            previous = merged[url]
            if title and (not previous.title or previous.title == url):
                previous.title = title
            previous.snippet = previous.snippet or snippet[:400]
            continue
        merged[url] = SearchResult(
            title=title or url, url=url, snippet=snippet[:400], content="",
            allow_snippet_fallback=False, discovery_type="web_search_discovery",
        )
    return list(merged.values())[:max(0, max_results)]


class OpenAIWebSearchProvider(SearchProvider):
    """One bounded Responses call; no retries and no fallback search service."""

    allows_snippet_fallback = False

    def __init__(
        self, api_key: str = "", api_base: str = "https://api.openai.com/v1",
        model: str = "gpt-4o", *, allowed_domains: list[str] | None = None,
        receipt_dir: Path | None = DEFAULT_RECEIPT_DIR, timeout: float = 55,
        max_concurrency: int = 1,
    ) -> None:
        self._api_key = api_key
        self._api_base = api_base
        self.model = model
        self.allowed_domains = list(dict.fromkeys(domain.strip().lower() for domain in (allowed_domains or [])))
        self.receipt_dir = Path(receipt_dir) if receipt_dir is not None else None
        self.timeout = min(60.0, max(1.0, timeout))
        self._semaphore = asyncio.Semaphore(min(2, max(1, max_concurrency)))

    @property
    def name(self) -> str:
        return "openai_web_search"

    def _receipt(self, query: str, payload: dict) -> None:
        if self.receipt_dir is None:
            return
        self.receipt_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC)
        path = self.receipt_dir / f"{timestamp:%Y%m%dT%H%M%S}_{uuid.uuid4().hex}.json"
        data = _safe_payload({
            "type": "web_search_discovery", "provider": self.name, "model": self.model,
            "queried_at": timestamp.isoformat(), "query": query,
            "allowed_domains": self.allowed_domains, "search_context_size": "low",
            "response": payload,
        }, self._api_key)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        if not self._api_key:
            raise RuntimeError("OpenAI web_search requires the saved OpenAI API key")
        if max_results <= 0 or not query.strip():
            return []
        from openai import AsyncOpenAI

        tool = {"type": "web_search", "search_context_size": "low"}
        if self.allowed_domains:
            tool["filters"] = {"allowed_domains": self.allowed_domains}
        try:
            async with self._semaphore:
                # Covers SDK/connect/read timeouts. Never retry a possibly billed request.
                async with asyncio.timeout(self.timeout):
                    async with AsyncOpenAI(api_key=self._api_key, base_url=self._api_base,
                                           timeout=self.timeout, max_retries=0) as client:
                        response = await client.responses.create(
                            model=self.model, input=query, tools=[tool], tool_choice="required",
                            max_output_tokens=1000, max_tool_calls=1,
                            include=["web_search_call.action.sources"],
                        )
        except TimeoutError:
            raise TimeoutError("OpenAI web_search timed out; no retry or provider fallback") from None
        except Exception as exc:
            # SDK exception bodies can contain request information; browser batch
            # tools log exception strings, so do not propagate those bodies.
            raise RuntimeError(f"OpenAI web_search failed ({type(exc).__name__}); no provider fallback") from None
        payload = response.model_dump(mode="json")
        self._receipt(query, payload)
        results = parse_discovery_response(payload, 1000)
        if self.allowed_domains:
            results = [result for result in results if any(
                urlsplit(result.url).hostname == domain or
                (urlsplit(result.url).hostname or "").endswith("." + domain)
                for domain in self.allowed_domains)]
        return results[:max_results]
