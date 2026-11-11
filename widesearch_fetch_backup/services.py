"""DeepSeek first, then local static extraction and browser rendering."""
import asyncio
import threading
import time
from urllib.parse import urlsplit

from widesearch_experiment.common import atomic_json, safe_error
from widesearch_experiment.services import Services as OriginalServices
from .readers import browser_read, static_read, usable


class BackupServices(OriginalServices):
    def __init__(self, cfg, journal):
        super().__init__(cfg, journal)
        # MindSearch creates fresh Services in multiple threads/event loops.
        # Its Journal is shared by the attempt; keep the bounded reader slots
        # and cache there instead of attaching asyncio locks to another loop.
        with journal.lock:
            if not hasattr(journal, "_fetch_backup"):
                journal._fetch_backup = {"slots": threading.BoundedSemaphore(2), "cache": {}}
            self.backup = journal._fetch_backup

    async def deepseek_fetch(self, url):
        primary = await super().deepseek_fetch(url)
        if usable(primary) or self.cfg.get("fetch_backup_revision") != "fetch-backup-v1":
            return primary
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return {**primary, "content": "", "error": "unsupported_url"}

        with self.journal.lock:
            saved = self.backup["cache"].get(url)
        if saved and (usable(saved[1]) or time.monotonic() - saved[0] < 60):
            self.journal.emit("fetch_backup_cache_hit", url=url, backend=saved[1].get("backend"),
                              success=usable(saved[1]))
            return dict(saved[1])

        budget = float(self.cfg.get("fetch_backup_timeout_seconds", 35))
        started = time.monotonic()
        acquired = False
        last = {**primary, "content": "", "backend": "deepseek"}
        try:
            async with asyncio.timeout(budget):
                while not self.backup["slots"].acquire(blocking=False):
                    await asyncio.sleep(0.05)
                acquired = True
                # A request may have finished while this one waited for a slot.
                with self.journal.lock:
                    saved = self.backup["cache"].get(url)
                if saved and (usable(saved[1]) or time.monotonic() - saved[0] < 60):
                    self.journal.emit("fetch_backup_cache_hit", url=url, success=usable(saved[1]),
                                      backend=saved[1].get("backend"))
                    return dict(saved[1])
                for backend, reader in (("trafilatura", static_read), ("crawl4ai", browser_read)):
                    remaining = budget - (time.monotonic() - started)
                    if remaining <= 0:
                        break
                    # Leave room for browser startup if plain HTML has no body.
                    timeout = min(12, remaining) if backend == "trafilatura" else remaining
                    event = self.journal.emit("page_start", provider=backend, url=url, fallback=True,
                                              primary_error=primary.get("error"), primary_status=primary.get("status"))
                    directory = self.journal.directory / "fallback_receipts" / f"{event['seq']:06}-{backend}"
                    directory.mkdir(parents=True, exist_ok=True)
                    try:
                        async with asyncio.timeout(timeout):
                            last = await reader(url, directory, timeout)
                        if not usable(last):
                            last = {**last, "content": "", "error": last.get("error") or "no_readable_content"}
                    except asyncio.CancelledError:
                        self.journal.emit("page_end", parent=event["seq"], provider=backend, url=url,
                                          fallback=True, success=False, status=0, error={"type": "cancelled"})
                        raise
                    except Exception as exc:
                        last = {"title": "", "content": "", "status": 0, "error": safe_error(exc)["type"], "backend": backend}
                    self.journal.emit("page_end", parent=event["seq"], provider=backend, url=url, fallback=True,
                                      success=usable(last), status=last.get("status"), error=last.get("error"))
                    atomic_json(directory / "receipt.json", {"requested_url": url, **last})
                    if usable(last):
                        self.save_page(url, last["content"])
                        break
                    # A direct origin 404/410 or a non-web document gains nothing
                    # from running the same URL in a browser.
                    if last.get("status") in {404, 410} or last.get("error") in {"unsupported_content_type", "page_too_large"}:
                        break
        except TimeoutError:
            last = {"title": "", "content": "", "status": 0, "error": "fallback_timeout", "backend": last.get("backend")}
        finally:
            if acquired:
                self.backup["slots"].release()
        with self.journal.lock:
            self.backup["cache"][url] = (time.monotonic(), dict(last))
        return last
