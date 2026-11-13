"""Vozes sintéticas e trilha: três vozes, tempos reais e nada clonado."""
from __future__ import annotations

import pytest

from backend.services.fiction import timeline as tl, voices
from tests.fiction_helpers import (
    fake_synth,
    silent_synth,
    sine_wav,
    wav_bytes,
)


class TestVoices:
    def test_exactly_three_distinct_synthetic_voices(self):
        assert set(voices.VOICES) == {'narrador', 'personagem', 'sussurro'}
        ids = {voice['id'] for voice in voices.VOICES.values()}
        assert len(ids) == 3
        assert all(voice.startswith('pt-BR-') and voice.endswith('Neural') for voice in ids)

    def test_no_cloning_hook_exists(self):
        for name in dir(voices):
            assert 'clone' not in name.lower()


class TestSynthesis:
    def test_writes_audio_and_word_sidecar(self, tmp_path):
        target = tmp_path / 'audio' / 's1-l1.mp3'
        texts, timing = voices.synthesize('Duas palavras', 'narrador', target,
                                          synth=fake_synth(0.8))
        assert target.is_file() and target.stat().st_size > 1000
        assert timing == 'words'
        assert texts == ['Duas', 'palavras']

        from backend.services import subtitle_timing
        sidecar = subtitle_timing.sidecar_path(target)
        assert sidecar.is_file()
        words = voices.words_of(target)
        assert [word.text for word in words] == ['Duas', 'palavras']
        assert words[0].start < words[1].start

    def test_without_word_events_marks_timing_as_estimated(self, tmp_path):
        target = tmp_path / 'audio' / 's1-l1.mp3'
        texts, timing = voices.synthesize('Sem eventos', 'sussurro', target,
                                          synth=silent_synth(0.8))
        assert target.is_file()
        assert timing == 'estimated'
        assert texts == []
        assert voices.words_of(target) == []

    def test_unknown_tone_is_refused(self, tmp_path):
        with pytest.raises(voices.VoiceError, match='Tom de voz'):
            voices.synthesize('texto', 'gritando', tmp_path / 'a.mp3', synth=fake_synth(0.5))

    def test_empty_text_is_refused(self, tmp_path):
        with pytest.raises(voices.VoiceError, match='vazia'):
            voices.synthesize('   ', 'narrador', tmp_path / 'a.mp3', synth=fake_synth(0.5))

    def test_silent_engine_is_refused(self, tmp_path):
        def empty(text, voice, prosody):
            return b'', []

        with pytest.raises(voices.VoiceError, match='não devolveu áudio'):
            voices.synthesize('texto', 'narrador', tmp_path / 'a.mp3', synth=empty)

    def test_engine_failure_is_wrapped(self, tmp_path):
        def boom(text, voice, prosody):
            raise RuntimeError('sem rede')

        with pytest.raises(voices.VoiceError, match='sem rede'):
            voices.synthesize('texto', 'narrador', tmp_path / 'a.mp3', synth=boom)


class TestDurations:
    def test_measures_real_audio(self, tmp_path):
        path = tmp_path / 'a.wav'
        path.write_bytes(wav_bytes(0.6))
        assert voices.duration_seconds(path) == pytest.approx(0.6, abs=0.1)

    def test_missing_file_is_refused(self, tmp_path):
        with pytest.raises(voices.VoiceError, match='não encontrado'):
            voices.duration_seconds(tmp_path / 'nao-existe.mp3')


class TestTrack:
    def test_mixes_lines_at_their_timeline_positions(self, tmp_path):
        from backend.services.fiction.timeline import Cue, Line, Scene, Timeline
        first = tmp_path / 'audio' / 'a.wav'
        second = tmp_path / 'audio' / 'b.wav'
        sine_wav(first, 0.5)
        sine_wav(second, 0.5)
        scene = Scene(id='s1', index=0, start=0.0, end=1.5, image='images/scene-0.jpg',
                      lines=[Line(id='s1-l1', tone='narrador', text='um', start=0.2,
                                  end=0.7, audio='audio/a.wav'),
                             Line(id='s1-l2', tone='personagem', text='dois', start=0.8,
                                  end=1.3, audio='audio/b.wav')])
        timeline = Timeline(project_id='p1', title='Teste', audio='audio/track.wav',
                            scenes=[scene], cues=[Cue(start=0.2, end=1.3, text='um dois')])
        output = tmp_path / 'audio' / 'track.wav'
        assert voices.mix_track(timeline, tmp_path, output) == pytest.approx(1.5)
        assert output.is_file()
        assert voices.duration_seconds(output) == pytest.approx(1.5, abs=0.15)

    def test_missing_line_audio_is_refused(self, tmp_path):
        from backend.services.fiction.timeline import Cue, Line, Scene, Timeline
        scene = Scene(id='s1', index=0, start=0.0, end=1.5, image='images/scene-0.jpg',
                      lines=[Line(id='s1-l1', tone='narrador', text='um', start=0.2,
                                  end=1.2, audio='audio/falta.wav')])
        timeline = Timeline(project_id='p1', title='Teste', audio='audio/track.wav',
                            scenes=[scene], cues=[Cue(start=0.2, end=1.2, text='um')])
        with pytest.raises(voices.VoiceError, match='ausente'):
            voices.mix_track(timeline, tmp_path, tmp_path / 'audio' / 'track.wav')

    def test_empty_timeline_is_refused(self, tmp_path):
        timeline = tl.Timeline.model_construct(project_id='p1', scenes=[], audio='a.wav',
                                               duration=0.0)
        with pytest.raises(voices.VoiceError, match='nenhuma fala'):
            voices.mix_track(timeline, tmp_path, tmp_path / 'x.wav')


class TestProsody:
    def test_whisper_is_quieter_and_slower(self):
        assert voices.PROSODY['sussurro']['volume'].startswith('-')
        assert voices.PROSODY['sussurro']['rate'].startswith('-')
        assert voices.PROSODY['narrador'] == {}


class TestRetry:
    """O serviço de voz falha de forma intermitente: uma fala não pode matar a peça."""

    def test_transient_failure_is_retried(self, monkeypatch):
        calls = []

        def once(text, voice, prosody):
            calls.append(voice)
            if len(calls) < 3:
                raise RuntimeError('NoAudioReceived')
            return b'audio-valido', []

        monkeypatch.setattr(voices, '_edge_once', once)
        monkeypatch.setattr(voices, 'SYNTH_RETRY_SECONDS', 0)
        audio, events = voices._edge_synth('texto', 'pt-BR-AntonioNeural', {})
        assert audio == b'audio-valido' and events == []
        assert len(calls) == 3

    def test_permanent_failure_names_the_voice(self, monkeypatch):
        def once(text, voice, prosody):
            raise RuntimeError('NoAudioReceived')

        monkeypatch.setattr(voices, '_edge_once', once)
        monkeypatch.setattr(voices, 'SYNTH_RETRY_SECONDS', 0)
        with pytest.raises(voices.VoiceError, match='pt-BR-FranciscaNeural'):
            voices._edge_synth('texto', 'pt-BR-FranciscaNeural', {})
