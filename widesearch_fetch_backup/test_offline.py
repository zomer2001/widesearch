"""Offline reader contracts; fixtures never search or call a real model/API."""
import asyncio
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from widesearch_experiment.common import read_json
from widesearch_experiment.services import Journal, Services
from widesearch_fetch_backup import readers
from widesearch_fetch_backup.launcher import DEFAULT_CONFIG, backup_runner
from widesearch_fetch_backup.services import BackupServices


def page(body="fixture evidence", backend="trafilatura", **kwargs):
    return {"title": "fixture", "content": body, "status": 200, "error": "", "backend": backend, **kwargs}


class Readers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.journal = Journal(self.path)
        self.cfg = read_json(DEFAULT_CONFIG)
        self.service = BackupServices(self.cfg, self.journal)
        self.primary = {"title": "", "content": "", "status": 404, "error": "HTTPStatusError"}
        async def primary(service, url):
            start = service.journal.emit("page_start", provider="deepseek", url=url)
            service.journal.emit("page_end", parent=start["seq"], provider="deepseek", url=url,
                                 success=readers.usable(self.primary), status=self.primary["status"])
            return self.primary
        self.patch = patch.object(Services, "deepseek_fetch", primary)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        for target in ("socket.socket.connect", "socket.getaddrinfo"):
            denial = patch(target, side_effect=AssertionError("external network forbidden"))
            denial.start()
            self.addCleanup(denial.stop)

    async def test_primary_success_does_not_invoke_backups(self):
        self.primary = page(backend="deepseek")
        with patch('widesearch_fetch_backup.services.static_read', new_callable=AsyncMock) as direct:
            self.assertEqual(await self.service.fetch('https://example.test/a'), 'fixture evidence')
            direct.assert_not_awaited()

    async def test_static_success_is_saved_and_accounted(self):
        with patch('widesearch_fetch_backup.services.static_read', AsyncMock(return_value=page())), \
             patch('widesearch_fetch_backup.services.browser_read', new_callable=AsyncMock) as browser:
            result = await self.service.deepseek_fetch('https://example.test/a')
        self.assertEqual(result['backend'], 'trafilatura')
        browser.assert_not_awaited()
        self.assertEqual(self.journal.counts()['pages_fetched'], 1)
        self.assertEqual(len(list((self.path/'pages').glob('*.json'))), 1)
        receipts = list((self.path/'fallback_receipts').glob('*/receipt.json'))
        self.assertEqual(read_json(receipts[0])['backend'], 'trafilatura')
        self.assertEqual([e['provider'] for e in self.journal.events if e['kind']=='page_start'], ['deepseek','trafilatura'])

    async def test_empty_static_page_uses_browser(self):
        with patch('widesearch_fetch_backup.services.static_read', AsyncMock(return_value=page('Loading…'))), \
             patch('widesearch_fetch_backup.services.browser_read', AsyncMock(return_value=page('rendered evidence','crawl4ai'))) as browser:
            result = await self.service.deepseek_fetch('https://example.test/app#route')
        self.assertEqual(result['content'], 'rendered evidence')
        self.assertEqual(browser.await_args.args[0], 'https://example.test/app#route')
        self.assertEqual(self.journal.counts()['pages_fetched'], 1)

    async def test_confirmed_origin_404_does_not_launch_browser(self):
        with patch('widesearch_fetch_backup.services.static_read', AsyncMock(return_value=page('',status=404,error='HTTPStatusError'))), \
             patch('widesearch_fetch_backup.services.browser_read', new_callable=AsyncMock) as browser:
            result = await self.service.deepseek_fetch('https://example.test/missing')
        self.assertFalse(readers.usable(result))
        browser.assert_not_awaited()

    async def test_fallback_cache_is_shared_by_attempt_not_across_attempts(self):
        another = BackupServices(self.cfg, self.journal)
        fresh = BackupServices(self.cfg, Journal(self.path/'other_attempt'))
        with patch('widesearch_fetch_backup.services.static_read', AsyncMock(return_value=page())) as direct:
            await self.service.fetch('https://example.test/a')
            await another.fetch('https://example.test/a')
            await fresh.fetch('https://example.test/a')
        self.assertEqual(direct.await_count, 2)
        self.assertEqual(self.journal.counts()['pages_fetched'], 1)
        self.assertTrue(any(e['kind']=='fetch_backup_cache_hit' for e in self.journal.events))

    async def test_cancellation_propagates_and_releases_reader_slot(self):
        entered = asyncio.Event()
        async def slow(*args):
            entered.set()
            await asyncio.sleep(20)
        with patch('widesearch_fetch_backup.services.static_read', slow):
            task = asyncio.create_task(self.service.fetch('https://example.test/a'))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(self.service.backup['slots'].acquire(blocking=False))
        self.assertTrue(self.service.backup['slots'].acquire(blocking=False))
        self.service.backup['slots'].release()
        self.service.backup['slots'].release()

    async def test_backup_deadline_returns_failure_not_placeholder(self):
        cfg = {**self.cfg, 'fetch_backup_timeout_seconds': .02}
        service = BackupServices(cfg, self.journal)
        async def slow(*args):
            await asyncio.sleep(20)
        with patch('widesearch_fetch_backup.services.static_read', slow), \
             patch('widesearch_fetch_backup.services.browser_read', slow):
            result = await service.deepseek_fetch('https://example.test/a')
        self.assertFalse(readers.usable(result))
        self.assertEqual(result['content'], '')

    async def test_static_reader_extracts_real_html_fixture(self):
        source = '<html><head><title>Fixture article</title></head><body><article><h1>Fixture</h1>' + ''.join(
            '<p>Research fixture: the prototype supports a published interface and provides reproducible reference data. This paragraph is local test content only.</p>' for _ in range(6)) + '</article></body></html>'
        original = httpx.AsyncClient
        requests = []
        def handle(request):
            requests.append(request)
            self.assertNotIn('authorization',request.headers)
            return httpx.Response(200, text=source, headers={'content-type':'text/html; charset=utf-8'})
        with patch.object(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handle), **kw)):
            result = await readers.static_read('https://example.test/article', self.path/'static', 2)
        self.assertTrue(readers.usable(result))
        self.assertEqual(result['title'], 'Fixture article')
        self.assertIn('prototype supports',result['content'])
        self.assertTrue((self.path/'static/source.html').exists())
        self.assertEqual(len(requests),1)

    async def test_browser_bridge_uses_crawler_environment_without_api_keys(self):
        async def subprocess(args, logfile, env, timeout):
            self.assertTrue(args[0].endswith('/.venv/bin/python'))
            self.assertEqual(args[2],'widesearch_fetch_backup.browser')
            self.assertFalse(any(k.endswith('_API_KEY') for k in env))
            target = Path(args[args.index('--output')+1])
            target.write_text(json.dumps(page('rendered','crawl4ai')))
            return 0, False
        with patch('widesearch_fetch_backup.readers.subprocess_run',subprocess):
            result = await readers.browser_read('https://example.test/app',self.path/'browser',3)
        self.assertEqual(result['content'],'rendered')

    async def test_new_worker_routing_keeps_scorer_and_existing_runner_intact(self):
        from widesearch_experiment.runners import batch
        calls=[]
        async def fake(args,*pos,**kw):
            calls.append(args)
            return 0,False
        original_hashes=batch.code_hashes
        with patch.object(batch,'subprocess_run',fake):
            with backup_runner():
                self.assertIn('../widesearch_fetch_backup/services.py',batch.code_hashes())
                await batch.subprocess_run(['python','-m','widesearch_experiment.runners.worker'])
                await batch.subprocess_run(['python','-m','widesearch_experiment.evaluation.official'])
            self.assertIs(batch.subprocess_run,fake)
            self.assertIs(batch.code_hashes,original_hashes)
        self.assertEqual(calls[0][2],'widesearch_fetch_backup.worker')
        self.assertEqual(calls[1][2],'widesearch_experiment.evaluation.official')


if __name__=='__main__':
    unittest.main()
