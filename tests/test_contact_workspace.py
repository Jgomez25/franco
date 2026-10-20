"""Search must cover the full contact book, with safe filters and stable paging."""
import sqlite3

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from franco import dashboard
from franco.state import StateManager


@pytest_asyncio.fixture
async def contacts(tmp_path, monkeypatch):
    path = tmp_path / 'contacts.db'
    await StateManager(db_path=str(path)).init_db()
    monkeypatch.setattr(dashboard, 'DB_PATH', path)
    with sqlite3.connect(path) as db:
        db.executemany(
            'INSERT INTO prospects (id, first_name, last_name, company, title, industry, email, email_verified, score, status) VALUES (?,?,?,?,?,?,?,?,?,?)',
            [(str(i), 'Ada' if i == 220 else 'Person', str(i), 'Acme', 'VP Sales',
              'Software' if i % 2 else 'Retail', f'p{i}@example.test', i % 2, i % 101,
              'new' if i % 2 else 'contacted') for i in range(230)],
        )
    return TestClient(dashboard.app, base_url='http://localhost')


@pytest.mark.asyncio
async def test_search_beyond_old_limit_and_pagination(contacts):
    result = contacts.get('/api/contacts/search?q=Ada').json()
    assert result['total'] == 1 and result['items'][0]['id'] == '220'
    first = contacts.get('/api/contacts/search?sort=score').json()
    second = contacts.get('/api/contacts/search?sort=score&page=2').json()
    assert first['total'] == 230
    assert len(first['items']) == 25
    assert set(p['id'] for p in first['items']).isdisjoint(p['id'] for p in second['items'])
    assert first['items'][0]['score'] == 100
    assert contacts.get('/api/contacts/search?page=999').json()['page'] == 10


@pytest.mark.asyncio
async def test_combined_filters_and_literal_queries(contacts):
    result = contacts.get('/api/contacts/search?verified=true&min_score=80&industry=Software&status=new').json()
    assert result['total'] > 0
    assert all(p['email_verified'] and p['score'] >= 80 and p['industry'] == 'Software' for p in result['items'])
    for q in ('%', '_', "' OR 1=1 --"):
        assert contacts.get('/api/contacts/search', params={'q': q}).json()['total'] == 0
    for params in ({'sort':'DROP TABLE prospects'}, {'page':0}, {'page_size':1000}, {'min_score':-1}):
        assert contacts.get('/api/contacts/search', params=params).status_code == 422


def test_empty_install_and_static_assets(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, 'DB_PATH', tmp_path / 'missing.db')
    client = TestClient(dashboard.app, base_url='http://localhost')
    assert client.get('/api/contacts/search').json()['total'] == 0
    assert client.get('/static/contacts.js').status_code == 200
    assert client.get('/blueprint').status_code == 200
    assert client.get('/static/blueprint.js').status_code == 200
    assert client.get('/static/blueprint.css').status_code == 200
    assert client.get('/api/contacts/search', headers={'host':'evil.test'}).status_code == 403
