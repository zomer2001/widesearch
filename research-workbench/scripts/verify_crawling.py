"""Verify local extraction, JavaScript rendering and public-page fetching (no LLM)."""
from __future__ import annotations
import asyncio
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('CRAWL4_AI_BASE_DIRECTORY', str(ROOT / 'data/crawler-runtime'))
from searchos.tools.simple_browser.backend.base import BrowserService, build_backend

BODY = '这是一段用于验证正文提取的演示文本，抓取结果必须保留来源和真实页面内容。' * 30
DYNAMIC = '动态加载完成，浏览器读取到了 JavaScript 写入的正文。' * 20

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        if self.path == '/dynamic':
            html = '<html><head><title>动态抓取验证</title></head><body><main id="article">Loading</main><script>setTimeout(()=>document.getElementById("article").innerHTML=' + json.dumps("<h1>动态抓取验证</h1><p>" + DYNAMIC + "</p>") + ',150)</script></body></html>'
        else:
            html = f'<html><head><title>静态抓取验证</title></head><body><nav>登录 注册 帮助</nav><article><h1>静态抓取验证</h1><p>{BODY}</p></article></body></html>'
        data = html.encode()
        self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)

async def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    records = []
    try:
        for mode, url, marker in [
            ('trafilatura', base+'/static', BODY),
            ('crawl4ai', base+'/dynamic', DYNAMIC),
            ('auto', base+'/dynamic', DYNAMIC),
            ('trafilatura', 'https://www.python.org/about/', 'Python'),
            ('crawl4ai', 'https://quotes.toscrape.com/js/', 'Albert Einstein'),
        ]:
            started = time.monotonic()
            backend = build_backend(mode)
            try:
                result = await BrowserService(backend).fetch(url, timeout=60)
                record = {'requested': mode, 'url': result.url, 'ok': result.ok,
                          'characters': len(result.markdown), 'html_retained': bool(result.html),
                          'metadata': result.metadata, 'elapsed_s': round(time.monotonic()-started,2),
                          'marker_found': marker in result.markdown, 'error': result.error}
                print(json.dumps(record, ensure_ascii=False), flush=True)
                records.append(record)
            finally:
                await backend.close()
    finally:
        server.shutdown(); server.server_close()
        (ROOT/'data/crawling-verification.json').write_text(json.dumps(records, ensure_ascii=False, indent=2)+'\n')
    if not all(item['ok'] and item['marker_found'] and item['html_retained'] for item in records):
        raise SystemExit('网页采集实测未全部通过。')

if __name__ == '__main__': asyncio.run(main())
