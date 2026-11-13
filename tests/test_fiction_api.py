"""Rotas do modo história: recusas com o código HTTP certo e estado isolado."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api_ext


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('TIKCLIP_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('TIKCLIP_DISABLE_AUTO_PUBLISH', raising=False)
    app = FastAPI()
    app.include_router(api_ext.router, prefix='/api/v1/ext')
    return TestClient(app), tmp_path


class TestBuildRoute:
    def test_clone_request_is_refused_with_400(self, client):
        api, _ = client
        resposta = api.post('/api/v1/ext/fiction/build',
                            json={'theme': 'uma estrada', 'clone_voice': 'amostra.wav'})
        assert resposta.status_code == 400
        assert 'Não clono a voz' in resposta.json()['detail']

    def test_empty_theme_is_refused(self, client):
        api, _ = client
        resposta = api.post('/api/v1/ext/fiction/build', json={'theme': '   '})
        assert resposta.status_code == 400
        assert 'tema' in resposta.json()['detail']

    def test_long_theme_is_refused(self, client):
        api, _ = client
        resposta = api.post('/api/v1/ext/fiction/build', json={'theme': 'a' * 301})
        assert resposta.status_code == 400

    def test_invalid_project_id_is_refused(self, client):
        api, _ = client
        resposta = api.post('/api/v1/ext/fiction/build',
                            json={'theme': 'estrada', 'project_id': '../fora'})
        assert resposta.status_code == 400
        assert 'inválido' in resposta.json()['detail']


class TestStateRoutes:
    def test_unknown_project_is_404(self, client):
        api, _ = client
        assert api.get('/api/v1/ext/fiction/state/nao-existe').status_code == 404

    def test_empty_listing(self, client):
        api, _ = client
        body = api.get('/api/v1/ext/fiction/projects').json()
        assert body == {'items': []}

    def test_player_is_404_without_timeline(self, client):
        api, tmp = client
        (tmp / 'projects' / 'p1').mkdir(parents=True)
        assert api.get('/api/v1/ext/fiction/player/p1').status_code == 404


class TestPublishRoutes:
    def test_env_lock_returns_409(self, client, monkeypatch):
        api, _ = client
        monkeypatch.setenv('TIKCLIP_DISABLE_AUTO_PUBLISH', '1')
        resposta = api.post('/api/v1/ext/fiction/publish', json={'project_id': 'p1'})
        assert resposta.status_code == 409
        assert 'travada por ambiente' in resposta.json()['detail']

    def test_nothing_pending_returns_409(self, client):
        api, tmp = client
        (tmp / 'projects' / 'p1').mkdir(parents=True)
        resposta = api.post('/api/v1/ext/fiction/publish', json={'project_id': 'p1'})
        assert resposta.status_code == 409
        assert 'Nada pendente' in resposta.json()['detail']

    def test_missing_project_id_returns_400(self, client):
        api, _ = client
        assert api.post('/api/v1/ext/fiction/publish', json={}).status_code == 400

    def test_publish_state_reports_the_lock(self, client, monkeypatch):
        api, _ = client
        monkeypatch.setenv('TIKCLIP_DISABLE_AUTO_PUBLISH', 'true')
        body = api.get('/api/v1/ext/fiction/publish/state').json()
        assert body['env_locked'] is True
        assert body['running'] is False

    def test_batch_state_carries_the_fiction_block(self, client):
        api, _ = client
        body = api.get('/api/v1/ext/publish/state').json()
        # O bloco novo entra ao lado do lote do TikTok, sem substituí-lo.
        assert 'fiction' in body
        assert body['fiction']['running'] is False
        assert 'total' in body and 'log' in body
