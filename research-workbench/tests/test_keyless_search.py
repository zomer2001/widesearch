"""The default web search must work without paid credentials and keep evidence boundaries."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web'))
from api import workbench
from api.routes.workbench import SearchProbeRequest, probe_search
from searchos.tools.simple_browser.search.base import SearchResult
from searchos.tools.simple_browser.search.ddgs import DDGSProvider


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(workbench, 'CONFIG_PATH', tmp_path / 'private.json')
    workbench.save_config(workbench.Configuration())


def test_keyless_default_applies_to_the_actual_browser_provider(isolated_config):
    from searchos.tools.simple_browser.state import get_provider
    workbench.configure_search(required=True)
    assert get_provider().name == 'ddgs'


def test_paid_missing_key_clears_previous_provider_and_keeps_explicit_error(isolated_config):
    from fastapi import HTTPException
    from searchos.tools.simple_browser.state import get_provider
    workbench.configure_search(required=True)
    config = workbench.load_config()
    config.search.provider = 'serper'
    workbench.save_config(config)
    workbench.configure_search(required=False)
    assert get_provider() is None
    with pytest.raises(HTTPException) as caught:
        workbench.configure_search(required=True)
    assert caught.value.status_code == 400 and 'DDGS' in caught.value.detail


@pytest.mark.asyncio
async def test_keyless_result_is_a_snippet_not_full_evidence(monkeypatch):
    monkeypatch.setattr(DDGSProvider, '_search', staticmethod(lambda query, count: [
        {'title': 'Original', 'href': 'https://example.com/a', 'body': 'search hint'},
        {'title': 'duplicate', 'href': 'https://example.com/a', 'body': 'duplicate'},
        {'title': 'unsafe', 'href': 'javascript:alert(1)'},
        {'title': 'bad', 'href': 'http://[invalid'},
    ]))
    results = await DDGSProvider().search('example', 3)
    assert len(results) == 1
    assert results[0].url == 'https://example.com/a'
    assert results[0].snippet == 'search hint'
    assert results[0].content == ''


@pytest.mark.asyncio
async def test_probe_checks_selected_service_without_saving_or_model_call(isolated_config, monkeypatch):
    async def search(self, query, max_results=10):
        assert query == 'Python' and max_results == 3
        return [SearchResult(title='Python', url='https://www.python.org/', snippet='official')]
    monkeypatch.setattr(DDGSProvider, 'search', search)
    before = workbench.CONFIG_PATH.read_bytes()
    result = await probe_search(SearchProbeRequest(provider='ddgs', query='Python'))
    assert result['ok'] and result['provider'] == 'ddgs'
    assert result['results'][0]['url'] == 'https://www.python.org/'
    assert before == workbench.CONFIG_PATH.read_bytes()


@pytest.mark.asyncio
async def test_probe_failure_does_not_leak_upstream_data(isolated_config, monkeypatch):
    from fastapi import HTTPException
    async def search(*args, **kwargs):
        raise RuntimeError('private-api-token-must-not-appear')
    monkeypatch.setattr(DDGSProvider, 'search', search)
    with pytest.raises(HTTPException) as caught:
        await probe_search(SearchProbeRequest())
    assert caught.value.status_code == 503
    assert 'private-api-token' not in str(caught.value.detail)


def test_shared_factory_accepts_keyless_provider():
    from searchos.tools.simple_browser.search import build_search_provider
    assert build_search_provider('ddgs').name == 'ddgs'

@pytest.mark.asyncio
async def test_start_web_research_does_not_require_paid_key(isolated_config, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from api.routes import search as routes
    from searchos.harness import session
    from searchos.tools.simple_browser.state import get_provider
    async def selected(*args, **kwargs):
        return workbench.Selection('cli', 'test-model', '', '')
    class Harness:
        def __init__(self, **kwargs): pass
        async def run(self, query, **kwargs):
            assert get_provider().name == 'ddgs'
            assert kwargs['initial_state'].source_scope['mode'] == 'web'
            return SimpleNamespace(answer='model stub', execution_error='')
    monkeypatch.setattr(workbench, 'select_engine', selected)
    monkeypatch.setattr(workbench, 'build_models', lambda selection: {})
    monkeypatch.setattr(session, 'SearchSession', Harness)
    monkeypatch.setattr(routes, 'WORKSPACE_ROOT', str(tmp_path/'runs'))
    response = await routes.create_search(routes.SearchRequest(query='Find the Python website', engine='cli', max_time=10))
    assert response.status == 'running'
    await routes.sessions[response.session_id]['task']
    assert routes.sessions[response.session_id]['status'] == 'completed'
    routes.sessions.pop(response.session_id)
