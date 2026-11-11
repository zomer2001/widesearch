"""Local page readers. No model calls, search expansion or question rewriting."""
import asyncio
import html
import os
import re
from pathlib import Path

from widesearch_experiment.common import ROOT, WORKSPACE, atomic_json, read_json, safe_error
from widesearch_experiment.runners.batch import subprocess_run


def usable(page):
    body = page.get("content", "").strip()
    if page.get("error") or not body:
        return False
    return not (len(body) < 200 and re.search(
        r"enable javascript|javascript (?:is )?(?:required|disabled)|^loading[. …]*$|^加载中[.。 …]*$",
        body, re.I))


async def static_read(url, directory, timeout):
    import httpx
    import trafilatura
    result = {"title": "", "content": "", "status": 0, "error": "", "backend": "trafilatura"}
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=True, timeout=timeout,
                                     headers={"User-Agent": "Mozilla/5.0 (compatible; ResearchReader/1.0)"}) as client:
            async with client.stream("GET", url) as response:
                result.update(status=response.status_code, resolved_url=str(response.url))
                response.raise_for_status()
                media_type = response.headers.get("content-type", "").split(";")[0].lower()
                if media_type and not (media_type.startswith("text/") or media_type == "application/xhtml+xml"):
                    return {**result, "error": "unsupported_content_type"}
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 4_000_000:
                        return {**result, "error": "page_too_large"}
                response.encoding = response.encoding or "utf-8"
                source = bytes(raw).decode(response.encoding, errors="replace")
        directory.mkdir(parents=True, exist_ok=True)
        if media_type in {"text/plain", "text/markdown"}:
            result["content"] = source.strip()
            (directory / "source.txt").write_text(source)
        else:
            (directory / "source.html").write_text(source)
            result["content"] = await asyncio.to_thread(
                trafilatura.extract, source, url=result["resolved_url"], output_format="markdown",
                include_tables=True, include_links=True, include_comments=False) or ""
            title = re.search(r"<title[^>]*>(.*?)</title>", source, re.I | re.S)
            result["title"] = html.unescape(re.sub(r"\s+", " ", title[1])).strip() if title else ""
        if not usable(result):
            result["error"] = "no_readable_content"
    except Exception as exc:
        error = safe_error(exc)
        result.update(error=error["type"], status=error["status_code"] or result["status"])
    return result


async def browser_read(url, directory, timeout):
    """Use the existing crawler environment, including from the Lagent worker.

    MindSearch pins an older environment without Crawl4AI. A bounded child
    process avoids changing those dependencies or sharing browsers across loops.
    """
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / "result.json"
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env["PYTHONPATH"] = str(WORKSPACE)
    env["CRAWL4_AI_BASE_DIRECTORY"] = str(directory / "cache")
    code, timed_out = await subprocess_run([
        str(ROOT / ".venv/bin/python"), "-m", "widesearch_fetch_backup.browser",
        "--url", url, "--output", str(output), "--timeout", str(timeout)],
        directory / "browser.log", env, timeout)
    if output.exists():
        return read_json(output)
    return {"title": "", "content": "", "status": 0, "backend": "crawl4ai",
            "error": "browser_timeout" if timed_out else f"browser_exit_{code}"}
