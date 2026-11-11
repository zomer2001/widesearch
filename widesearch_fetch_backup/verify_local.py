"""Optional real-browser check against a local fixture, with DeepSeek mocked."""
import asyncio
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from widesearch_experiment.common import read_json
from widesearch_experiment.services import Journal, Services
from .launcher import DEFAULT_CONFIG
from .services import BackupServices

ARTICLE = '<article><h1>Local browser fixture</h1>' + (
    '<p>This local fixture verifies browser rendering and article extraction. '
    'It contains no external resources and requires no model API.</p>' * 8) + '</article>'


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = ('<html><head><title>Local fixture</title></head><body><p>Please enable JavaScript</p>'
                '<script>setTimeout(()=>{document.body.innerHTML=' + json.dumps(ARTICLE)
                + '},30)</script></body></html>').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


async def failed_primary(self, url):
    return {'title': '', 'content': '', 'status': 404, 'error': 'fixture_primary_failure'}


async def verify(port):
    with tempfile.TemporaryDirectory(prefix='reader-fixture-') as temp:
        journal = Journal(Path(temp))
        service = BackupServices(read_json(DEFAULT_CONFIG), journal)
        with patch.object(Services, 'deepseek_fetch', failed_primary):
            result = await service.deepseek_fetch(f'http://127.0.0.1:{port}/dynamic')
        print(json.dumps({'backend': result.get('backend'), 'characters': len(result.get('content', '')),
                          'error': result.get('error'), 'events': [(e['kind'], e.get('provider')) for e in journal.events]}))
        assert result.get('backend') == 'crawl4ai' and 'local fixture verifies browser rendering' in result['content'].lower(), result


if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        asyncio.run(verify(server.server_port))
    finally:
        server.shutdown()
        server.server_close()
