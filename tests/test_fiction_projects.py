"""Build do modo história: registro no projeto, falha explícita e limpeza."""
from __future__ import annotations

import json

import pytest

from backend.services.fiction import projects, timeline as tl
from tests.fiction_helpers import fake_call, fake_generate, fake_synth, script_payload

IMAGE_CONFIG = {'enabled': True, 'configured': True, 'provider': 'openai',
                'model': 'gpt-image-1', 'base_url': '', 'api_key': ''}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Isola o diretório de dados: nenhum projeto real é tocado."""
    monkeypatch.setenv('TIKCLIP_DATA_DIR', str(tmp_path))
    return tmp_path


def fake_render(seen: list | None = None):
    """Dublê de `render.render`: cria o MP4 e devolve o descritivo."""
    calls = seen if seen is not None else []

    def run(timeline, project, output, *, capture=None, progress=None):
        from pathlib import Path
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b'\x00' * 2048)
        calls.append({'duration': timeline.duration, 'frames': round(timeline.duration * 30)})
        if progress:
            progress(50)
            progress(100)
        return {'path': str(output), 'duration': timeline.duration, 'width': 1080,
                'height': 1920, 'fps': 30, 'frames': round(timeline.duration * 30),
                'bytes': 2048}

    run.calls = calls
    return run


def build(project_id='p1', **overrides):
    kwargs = dict(theme='Uma estrada à noite', script_call=fake_call(script_payload()),
                  generate_image=fake_generate(), image_config=IMAGE_CONFIG,
                  synth=fake_synth(2.6), render_fn=fake_render())
    kwargs.update(overrides)
    return projects.build(project_id, **kwargs)


class TestBuild:
    def test_records_a_ready_video_in_the_project(self, data_dir):
        item = build()
        assert item['status'] == 'ready'
        assert item['percent'] == 100
        assert item['title'] == 'O carro na estrada'
        assert item['has_video'] is True
        assert 30 <= item['duration'] <= 45
        assert item['scenes'] == 6 and item['cues'] > 0
        assert item['timing'] == ['words']

    def test_writes_a_plain_timeline_file(self, data_dir):
        build()
        path = data_dir / 'projects' / 'p1' / 'metadata' / 'fiction' / 'timeline.json'
        raw = json.loads(path.read_text(encoding='utf-8'))
        assert raw['version'] == 1
        assert len(raw['scenes']) == 6
        assert raw['scenes'][0]['image'] == 'images/scene-0.jpg'
        assert raw['scenes'][0]['lines'][0]['audio'].startswith('audio/')
        assert raw['cues'] and raw['audio'] == 'audio/track.wav'
        loaded = tl.load(data_dir / 'projects' / 'p1')
        assert loaded.duration == pytest.approx(item_duration(raw))

    def test_job_and_variant_are_completed(self, data_dir):
        item = build()
        state = json.loads((data_dir / 'projects' / 'p1' / 'metadata' / 'studio.json')
                           .read_text(encoding='utf-8'))
        job = next(row for row in state['jobs'] if row['job_id'] == item['job_id'])
        variant = next(row for row in state['output_variants'] if row['id'] == item['variant_id'])
        assert job['status'] == 'completed'
        assert job['result']['duration'] >= 30
        assert variant['status'] == 'completed'
        assert variant['post']['title'] == 'O carro na estrada'
        assert variant['render_job_id'] == item['job_id']

    def test_video_appears_ready_in_the_library(self, data_dir):
        import api_ext
        item = build()
        clips = api_ext.project_clips('p1')
        assert len(clips) == 1
        assert clips[0]['status'] == 'pronto'
        assert clips[0]['has_video'] is True
        assert clips[0]['title'] == 'O carro na estrada'
        assert clips[0]['duration'] >= 30
        assert clips[0]['id'] == item['variant_id']

    def test_library_delete_removes_variant_and_file(self, data_dir):
        import api_ext
        item = build()
        video = data_dir / 'projects' / 'p1' / 'output' / 'studio' / f"{item['job_id']}.mp4"
        assert video.is_file()
        result = api_ext.ext_delete_clip('p1', item['variant_id'])
        assert result['removed'] is True
        assert not video.exists()
        assert api_ext.project_clips('p1') == []

    def test_generated_audio_and_images_stay_in_the_project(self, data_dir):
        build()
        root = data_dir / 'projects' / 'p1'
        assert len(list((root / 'images').glob('scene-*.jpg'))) == 6
        assert len(list((root / 'audio').glob('*.mp3'))) == 10
        assert (root / 'audio' / 'track.wav').is_file()

    def test_image_provider_is_required(self, data_dir):
        with pytest.raises(projects.BuildError, match='provedor de imagem'):
            build(image_config={'enabled': False, 'configured': False, 'provider': 'openai',
                                'model': '', 'base_url': '', 'api_key': ''})

    def test_failure_marks_the_project_failed_without_output(self, data_dir):
        # O build falha alto (não devolve estado) e grava o motivo no projeto.
        with pytest.raises(projects.BuildError, match='cena'):
            build(project_id='p2', generate_image=_broken_image())
        item = projects.state('p2')
        assert item['status'] == 'failed'
        assert 'cena' in item['error'] and 'quebrou' in item['error']
        assert item['has_video'] is False
        studio = data_dir / 'projects' / 'p2' / 'output' / 'studio'
        assert not studio.exists() or not list(studio.glob('*.mp4'))
        meta = data_dir / 'projects' / 'p2' / 'metadata' / 'studio.json'
        state = json.loads(meta.read_text(encoding='utf-8'))
        job = next(row for row in state['jobs'] if row['job_id'] == item['job_id'])
        variant = next(row for row in state['output_variants'] if row['id'] == item['variant_id'])
        assert job['status'] == 'failed' and variant['status'] == 'failed'

    def test_failed_then_retried_starts_clean(self, data_dir):
        with pytest.raises(projects.BuildError):
            build(project_id='p3', generate_image=_broken_image())
        item = build(project_id='p3')
        assert item['status'] == 'ready'
        import api_ext
        # A tentativa que falhou continua listada como "falhou"; a nova fica pronta.
        statuses = [clip['status'] for clip in api_ext.project_clips('p3')]
        assert 'pronto' in statuses

    def test_template_failure_is_also_loud(self, data_dir):
        with pytest.raises(projects.BuildError, match='palavras|caso real'):
            build(project_id='p4', script_call=fake_call(_short_script()))


class TestStateAndListing:
    def test_unknown_project_is_idle(self, data_dir):
        item = projects.state('nada-aqui')
        assert item['status'] == 'idle'
        assert item['has_video'] is False

    def test_ready_without_file_reports_failed(self, data_dir):
        item = build()
        video = data_dir / 'projects' / 'p1' / 'output' / 'studio' / f"{item['job_id']}.mp4"
        video.unlink()
        after = projects.state('p1')
        assert after['status'] == 'failed'
        assert 'não está mais no disco' in after['error']

    def test_listing_shows_built_stories(self, data_dir):
        build()
        items = projects.list_projects()
        assert [item['project_id'] for item in items] == ['p1']
        assert items[0]['status'] == 'ready'

    def test_video_path_and_player(self, data_dir):
        item = build()
        video = projects.video_path('p1')
        assert video is not None and video.is_file()
        html = projects.player_file('p1')
        assert 'window.seek' in html and item['title'] in html


def _broken_image():
    def generate(*, provider, api_key, base_url, request):
        raise RuntimeError('o provedor quebrou')
    return generate


def _short_script():
    return {'title': 'Curta', 'scenes': [
        {'image_prompt': 'A dark room with a single lamp',
         'lines': [{'tone': 'narrador', 'text': 'Oi.'}]} for _ in range(6)]}


def item_duration(raw: dict) -> float:
    return round(raw['scenes'][-1]['end'], 3)
