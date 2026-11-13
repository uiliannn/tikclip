"""Schema da timeline e gerador a partir do roteiro (sem rede, sem TTS)."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from backend.services.fiction import builder
from backend.services.fiction import timeline as tl
from backend.services.fiction.script import parse_script
from tests.fiction_helpers import script_payload


def sounds(seconds: float, with_words: bool = True) -> list[builder.SpokenLine]:
    items = []
    for index in range(10):
        words = (((('palavra', 0.1, 0.5), ('dita', 0.6, 1.0)) if with_words else ()))
        items.append(builder.SpokenLine(audio=f'audio/l{index}.mp3', duration=seconds,
                                        words=words, timing='words' if with_words else 'estimated'))
    return items


def make(seconds: float = 2.8, with_words: bool = True) -> tl.Timeline:
    story = parse_script(script_payload())
    images = [f'images/scene-{index}.jpg' for index in range(6)]
    return builder.build_timeline(story, images, sounds(seconds, with_words),
                                  builder.BuildInput(project_id='p1', title='O carro na estrada'))


def simple_timeline(seconds: float = 1.2) -> tl.Timeline:
    """Timeline mínima válida: serve para testar schema e render, não publicação."""
    from backend.services.fiction.timeline import Cue, Line, Scene, Timeline
    line = Line(id='s1-l1', tone='narrador', text='Uma linha', start=0.05,
                end=round(seconds - 0.05, 3), audio='audio/track.wav')
    scene = Scene(id='s1', index=0, start=0.0, end=seconds, image='images/scene-0.jpg',
                  lines=[line])
    return Timeline(project_id='p1', title='Teste', audio='audio/track.wav', scenes=[scene],
                    cues=[Cue(start=0.1, end=round(seconds - 0.1, 3), text='Uma linha')])


class TestSchema:
    def test_build_is_contiguous_and_broadcast_ready(self):
        timeline = make()
        tl.validate_broadcast_ready(timeline)
        assert len(timeline.scenes) == 6
        assert timeline.scenes[0].start == 0
        for previous, current in zip(timeline.scenes, timeline.scenes[1:]):
            assert current.start == previous.end
        assert 30 <= timeline.duration <= 45
        assert timeline.width == 1080 and timeline.height == 1920 and timeline.fps == 30

    def test_cues_inside_their_scenes(self):
        timeline = make()
        assert timeline.cues
        for cue in timeline.cues:
            assert any(scene.start <= cue.start and cue.end <= scene.end
                       for scene in timeline.scenes)

    def test_lines_inside_their_scenes(self):
        for scene in make().scenes:
            for line in scene.lines:
                assert scene.start <= line.start < line.end <= scene.end

    def test_cue_at(self):
        timeline = make()
        first = timeline.cues[0]
        assert first in timeline.cue_at(first.start + 0.01)
        assert timeline.cue_at(timeline.duration + 5) == []

    def test_gap_between_scenes_is_rejected(self):
        data = simple_timeline().model_dump()
        data['scenes'].append({
            'id': 's2', 'index': 1, 'start': 2.0, 'end': 3.2,
            'image': 'images/scene-0.jpg',
            'lines': [{'id': 's2-l1', 'tone': 'narrador', 'text': 'Outra cena',
                       'start': 2.05, 'end': 3.15, 'audio': 'audio/track.wav'}]})
        with pytest.raises(ValidationError, match='contíguas'):
            tl.Timeline.model_validate(data)

    def test_cue_outside_scene_is_rejected(self):
        data = simple_timeline().model_dump()
        data['cues'][0]['end'] = 9.0
        with pytest.raises(ValidationError, match='fora das cenas'):
            tl.Timeline.model_validate(data)

    def test_short_scene_is_rejected(self):
        data = simple_timeline().model_dump()
        data['scenes'][0]['end'] = 0.5
        data['cues'][0]['end'] = 0.4
        with pytest.raises(ValidationError):
            tl.Timeline.model_validate(data)

    def test_zoom_cannot_shrink(self):
        data = simple_timeline().model_dump()
        data['scenes'][0]['ken_burns'] = {'start_scale': 1.2, 'end_scale': 1.05,
                                          'focus': 'center'}
        with pytest.raises(ValidationError, match='zoom final'):
            tl.Timeline.model_validate(data)


class TestPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        timeline = make()
        path = tl.save(tmp_path, timeline)
        assert path == tmp_path / 'metadata' / 'fiction' / 'timeline.json'
        loaded = tl.load(tmp_path)
        assert loaded.duration == timeline.duration
        assert len(loaded.cues) == len(timeline.cues)
        assert loaded.scenes[0].image == timeline.scenes[0].image

    def test_load_missing_file_is_explicit(self, tmp_path):
        with pytest.raises(tl.TimelineError, match='não encontrada'):
            tl.load(tmp_path)

    def test_load_invalid_json_is_explicit(self, tmp_path):
        path = tmp_path / 'metadata' / 'fiction' / 'timeline.json'
        path.parent.mkdir(parents=True)
        path.write_text('{"version": 1}', encoding='utf-8')
        with pytest.raises(tl.TimelineError, match='inválida'):
            tl.load(tmp_path)

    def test_saved_file_is_plain_json(self, tmp_path):
        tl.save(tmp_path, make())
        raw = json.loads((tmp_path / 'metadata' / 'fiction' / 'timeline.json')
                         .read_text(encoding='utf-8'))
        assert raw['scenes'][0]['lines'][0]['audio'].startswith('audio/')
        assert raw['cues'] and raw['scenes'][0]['transition']['type'] == 'fade'


class TestBuilder:
    def test_stretches_short_story_to_the_minimum(self):
        timeline = make(seconds=2.6)  # 26 s de fala + respiros = 29,6 s
        assert timeline.duration == pytest.approx(30.0, abs=0.01)
        tl.validate_broadcast_ready(timeline)

    def test_too_long_story_is_refused(self):
        with pytest.raises(tl.TimelineError, match='limite'):
            make(seconds=4.5)

    def test_too_short_story_is_refused(self):
        # 6 cenas de 0,2 s: nem com a pausa máxima chega a 30 s
        story = parse_script(script_payload())
        images = [f'images/scene-{index}.jpg' for index in range(6)]
        with pytest.raises(tl.TimelineError, match='pausa máxima'):
            builder.build_timeline(story, images, sounds(0.2),
                                   builder.BuildInput(project_id='p1', title='Curta'))

    def test_wrong_image_count_is_refused(self):
        story = parse_script(script_payload())
        with pytest.raises(tl.TimelineError, match='imagens para'):
            builder.build_timeline(story, ['images/one.jpg'], sounds(2.8),
                                   builder.BuildInput(project_id='p1', title='X'))

    def test_wrong_sound_count_is_refused(self):
        story = parse_script(script_payload())
        images = [f'images/scene-{index}.jpg' for index in range(6)]
        with pytest.raises(tl.TimelineError, match='áudios de fala'):
            builder.build_timeline(story, images, sounds(2.8)[:3],
                                   builder.BuildInput(project_id='p1', title='X'))

    def test_estimated_timing_is_recorded(self):
        timeline = make(with_words=False)
        assert timeline.meta['timing'] == ['estimated']
        assert timeline.cues  # continua com legenda, distribuída no espaço da fala

    def test_word_timing_is_recorded(self):
        timeline = make()
        assert timeline.meta['timing'] == ['words']
        assert timeline.meta['tones'] == ['narrador', 'personagem', 'sussurro']

    def test_caption_chunks_never_break_a_word(self):
        text = 'Uma frase com várias palavras para quebrar em telas curtas de legenda'
        chunks = builder.caption_chunks(text, 20)
        assert ' '.join(chunks) == text
        assert all(len(chunk) <= 20 for chunk in chunks)

    def test_caption_chunks_breaks_a_giant_word(self):
        chunks = builder.caption_chunks('palavramonstruosamentecomprida', 10)
        assert ''.join(chunks) == 'palavramonstruosamentecomprida'
        assert all(len(chunk) <= 10 for chunk in chunks)

    def test_accent_colour_follows_the_palette(self):
        assert builder.accent_colour('azure') == '#5A8BFF'
        assert builder.accent_colour('nao-existe') == '#5A8BFF'


class TestBroadcastRules:
    def test_short_timeline_is_not_broadcast_ready(self):
        with pytest.raises(tl.TimelineError, match='duração'):
            tl.validate_broadcast_ready(simple_timeline())

    def test_six_scenes_are_required(self):
        timeline = simple_timeline()
        with pytest.raises(tl.TimelineError, match='cenas'):
            tl.validate_broadcast_ready(timeline.model_copy(update={'cues': [
                tl.Cue(start=0.1, end=0.5, text='x')]}))
