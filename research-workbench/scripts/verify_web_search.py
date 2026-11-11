"""Verify the actual search/open tools without API keys or a model call."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('CRAWL4_AI_BASE_DIRECTORY', str(ROOT/'data/crawler-runtime'))

async def main():
    from searchos.tools.simple_browser.search.ddgs import DDGSProvider
    from searchos.tools.simple_browser.backend.automatic import AutomaticBackend
    from searchos.tools.simple_browser.backend.base import BrowserService
    from searchos.tools.simple_browser.state import set_browser_provider, reset_browser, _get_browser
    from searchos.tools.simple_browser.tools import search, open as open_page
    service = BrowserService(AutomaticBackend())
    BrowserService.set_instance(service)
    set_browser_provider(DDGSProvider())
    reset_browser()
    record = {'provider': 'ddgs', 'paid_api_key_used': False, 'model_called': False}
    try:
        await search.ainvoke({'query':'Python programming official website'})
        browser = _get_browser()
        urls = list(browser.search_content_cache)
        url = next((value for value in urls if urlsplit(value).hostname == 'www.python.org'), '')
        assert url, '搜索没有返回 Python 官网，不能计为通过'
        output = await open_page.ainvoke({'id_or_url':url})
        page = browser.get_page()
        record.update(search_results=len(urls), selected_url=url,
                      opened_characters=len(page.text), fetch=page.metadata.get('fetch'),
                      read_succeeded='Python' in page.text and '[FETCH_ERROR]' not in output)
        assert record['read_succeeded'], '原文读取未通过'
        print(json.dumps(record, ensure_ascii=False), flush=True)
    finally:
        (ROOT/'data/web-search-verification.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
        await service.close()
        BrowserService.reset()

if __name__ == '__main__': asyncio.run(main())
