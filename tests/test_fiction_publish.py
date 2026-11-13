"""Publicação da história: trava por ambiente, registro por plataforma e não-republicação."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from backend.services.fiction import publish


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv('TIKCLIP_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('TIKCLIP_DISABLE_AUTO_PUBLISH', raising=False)
    with publish.LOCK:
        publish.STATE.update(running=False, stop=False, index=0, total=0, message='', log=[],
                             project_id=None, platforms=[], published=[], failed=[])
    publish._thread = None
    yield tmp_path
    with publish.LOCK:
        publish.STATE.update(running=False, stop=False)
    publish._thread = None


def ready_project(root: Path, pid: str = 'p1', variant_id: str = 'v1',
                  job_id: str = 'j1') -> dict:
    """Projeto com um corte já renderizado, exatamente como o build grava."""
    project = root / 'projects' / pid
    (project / 'output' / 'studio').mkdir(parents=True)
    (project / 'output' / 'studio' / f'{job_id}.mp4').write_bytes(b'\x00' * 4096)
    (project / 'metadata').mkdir(parents=True, exist_ok=True)
    state = {
        'schema_version': 2, 'drafts': [], 'events': [],
        'jobs': [{'job_id': job_id, 'status': 'completed', 'percent': 100, 'draft_id': 'd1',
                  'title': 'O carro na estrada', 'revision': 1, 'created_at': '2026-01-01',
                  'instance': 'outro-processo', 'result': {'duration': 33.4}}],
        'output_variants': [{
            'id': variant_id, 'draft_id': 'd1', 'draft_revision': 1, 'strategy_id': 'tiktok',
            'strategy_version': 1, 'branding': {'outro_enabled': False, 'outro_version': 'v1'},
            'status': 'completed', 'render_job_id': job_id, 'created_at': '2026-01-01',
            'post': {'title': 'O carro na estrada', 'description': 'uma história',
                     'tags': ['ficcao', 'terror']}}],
        'fiction': {'status': 'ready', 'job_id': job_id, 'variant_id': variant_id,
                    'title': 'O carro na estrada', 'duration': 33.4},
    }
    (project / 'metadata' / 'studio.json').write_text(
        json.dumps(state, ensure_ascii=False), encoding='utf-8')
    return {'project': pid, 'variant': variant_id, 'job': job_id}


def fake_publishers(seen: list, fail: set[str] | None = None) -> dict:
    fail = fail or set()

    def make(platform: str):
        def run(video, clip, account, progress):
            seen.append({'platform': platform, 'video': str(video), 'account': account,
                         'title': clip['title'], 'tags': clip.get('tags')})
            if progress:
                progress('enviando')
            if platform in fail:
                raise RuntimeError('o site recusou o envio')
            return {'status': 'completed', 'url': f'https://exemplo/{platform}/1',
                    'message': f'Publicado em {platform}'}
        return run

    return {platform: make(platform) for platform in ('tiktok', 'youtube')}


def wait_done(timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = publish.state()
        if not state['running']:
            return state
        time.sleep(0.05)
    raise AssertionError('a publicação não terminou a tempo')


class TestTrava:
    def test_env_lock_blocks_everything(self, data_dir, monkeypatch):
        ready_project(data_dir)
        monkeypatch.setenv('TIKCLIP_DISABLE_AUTO_PUBLISH', '1')
        assert publish.env_locked() is True
        with pytest.raises(publish.PublishError, match='travada por ambiente'):
            publish.start('p1')

    def test_no_ready_clip_is_refused(self, data_dir):
        with pytest.raises(publish.PublishError, match='Nada pendente'):
            publish.start('projeto-vazio')

    def test_invalid_platform_is_refused(self, data_dir):
        ready_project(data_dir)
        with pytest.raises(publish.PublishError, match='plataforma'):
            publish.start('p1', platforms=['orkut'])


class TestPublishing:
    def test_publishes_to_both_platforms_and_records_each(self, data_dir, monkeypatch):
        project = ready_project(data_dir)
        seen: list = []
        monkeypatch.setattr(publish, '_PUBLISHERS', fake_publishers(seen))

        publish.start(project['project'], platforms=['tiktok', 'youtube'],
                      tiktok_account='conta_tt', youtube_account='canal_yt', interval_sec=0)
        state = wait_done()

        assert [item['platform'] for item in seen] == ['tiktok', 'youtube']
        assert seen[0]['account'] == 'conta_tt' and seen[1]['account'] == 'canal_yt'
        assert seen[0]['title'] == 'O carro na estrada'
        assert len(state['published']) == 2
        assert state['failed'] == []
        assert publish.published_platforms('p1', project['variant']) == {'tiktok', 'youtube'}

        import api_ext
        statuses = [clip['status'] for clip in api_ext.project_clips('p1')]
        assert statuses == ['publicado']

    def test_published_clip_is_never_sent_twice(self, data_dir, monkeypatch):
        ready_project(data_dir)
        seen: list = []
        monkeypatch.setattr(publish, '_PUBLISHERS', fake_publishers(seen))
        publish.start('p1', platforms=['tiktok', 'youtube'], interval_sec=0)
        wait_done()
        assert len(seen) == 2

        with pytest.raises(publish.PublishError, match='já foram publicadas'):
            publish.start('p1', platforms=['tiktok', 'youtube'], interval_sec=0)
        assert len(seen) == 2

    def test_platform_failure_keeps_the_other_and_allows_retry(self, data_dir, monkeypatch):
        project = ready_project(data_dir)
        seen: list = []
        monkeypatch.setattr(publish, '_PUBLISHERS', fake_publishers(seen, fail={'tiktok'}))
        publish.start('p1', platforms=['tiktok', 'youtube'], interval_sec=0)
        state = wait_done()
        assert [item['platform'] for item in state['failed']] == ['tiktok']
        assert 'recusou' in state['failed'][0]['error']
        assert publish.published_platforms('p1', project['variant']) == {'youtube'}

        # Só o TikTok fica pendente: a nova tentativa não reenvia ao YouTube.
        seen.clear()
        monkeypatch.setattr(publish, '_PUBLISHERS', fake_publishers(seen))
        publish.start('p1', platforms=['tiktok', 'youtube'], interval_sec=0)
        wait_done()
        assert [item['platform'] for item in seen] == ['tiktok']
        assert publish.published_platforms('p1', project['variant']) == {'tiktok', 'youtube'}

    def test_stop_interrupts_the_queue(self, data_dir, monkeypatch):
        ready_project(data_dir)
        seen: list = []
        monkeypatch.setattr(publish, '_PUBLISHERS', fake_publishers(seen))
        assert publish.stop() == {'stopping': True}
        with publish.LOCK:
            publish.STATE.update(running=True, stop=True, published=[], failed=[], log=[])
        publish._loop('p1', ['tiktok'], None, None, 0)
        assert seen == []
        assert publish.state()['running'] is False

    def test_history_record_marks_the_platform(self, data_dir, monkeypatch):
        ready_project(data_dir)
        monkeypatch.setattr(publish, '_PUBLISHERS', fake_publishers([]))
        publish.start('p1', platforms=['tiktok'], interval_sec=0)
        wait_done()
        records = list((data_dir / 'projects' / 'p1' / 'output' / 'publish').glob('*.json'))
        assert len(records) == 1
        record = json.loads(records[0].read_text(encoding='utf-8'))
        assert record['provider'] == 'tiktok_local'
        assert record['platforms'] == ['tiktok']
        assert record['output_variant_id'] == 'v1'
        assert record['status'] == 'completed'
        assert record['results'][0]['success'] is True
