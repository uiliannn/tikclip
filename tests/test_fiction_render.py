"""Renderizador: comando do FFmpeg, captura quadro a quadro e falha explícita."""
from __future__ import annotations

import subprocess

import pytest

from backend.services.fiction import player, render
from backend.services.fiction.timeline import Cue, Line, Scene, Timeline
from backend.utils.ffmpeg_utils import get_ffmpeg_path
from tests.fiction_helpers import image_bytes, sine_wav, write_image


def make_timeline(folder, seconds: float = 1.2) -> Timeline:
    write_image(folder / 'images' / 'scene-0.jpg')
    sine_wav(folder / 'audio' / 'track.wav', seconds)
    line = Line(id='s1-l1', tone='narrador', text='Uma linha', start=0.05,
                end=round(seconds - 0.05, 3), audio='audio/track.wav')
    scene = Scene(id='s1', index=0, start=0.0, end=seconds, image='images/scene-0.jpg',
                  lines=[line])
    return Timeline(project_id='p1', title='Teste de render', audio='audio/track.wav',
                    scenes=[scene], cues=[Cue(start=0.1, end=round(seconds - 0.1, 3),
                                              text='Uma linha')])


class FakeCapture:
    """Captura falsa: escreve N quadros JPEG válidos, sem abrir navegador."""

    def __init__(self, frame: bytes | None = None, write: bool = True,
                 error: Exception | None = None):
        self.frame = frame if frame is not None else image_bytes()
        self.write_frames = write
        self.error = error
        self.calls: list[int] = []

    def __call__(self, html, fps, total, write, progress):
        assert 'data:image/jpeg;base64,' in html
        self.calls.append(total)
        if self.error:
            raise self.error
        for index in range(total):
            if self.write_frames:
                write(self.frame)
            if progress:
                progress(index + 1, total)


def probe(path) -> str:
    proc = subprocess.run([get_ffmpeg_path(), '-hide_banner', '-i', str(path)],
                          capture_output=True, text=True)
    return proc.stderr or ''


class TestCommand:
    def test_formats_are_the_publishing_contract(self):
        cmd = render._ffmpeg_command(__import__('pathlib').Path('a.wav'),
                                     __import__('pathlib').Path('o.mp4'), 30, None)
        joined = ' '.join(cmd)
        assert '-f image2pipe' in joined
        assert '-framerate 30' in joined
        assert 'aac' in joined and '160k' in joined
        assert '+faststart' in joined
        assert 'libx264' in joined or 'nvenc' in joined or 'qsv' in joined or 'amf' in joined
        assert '-r 30' in joined


class TestRender:
    def test_renders_vertical_mp4_with_audio(self, tmp_path):
        timeline = make_timeline(tmp_path)
        output = tmp_path / 'output' / 'studio' / 'job.mp4'
        capture = FakeCapture()
        result = render.render(timeline, tmp_path, output, capture=capture)
        assert output.is_file() and output.stat().st_size > 1000
        assert capture.calls == [36]  # 1,2 s a 30 fps
        info = probe(output)
        assert 'Video: h264' in info
        assert 'Audio: aac' in info
        assert '1080x1920' in info
        assert '30 fps' in info
        assert result['width'] == 1080 and result['height'] == 1920
        assert result['duration'] == pytest.approx(1.2)
        assert result['frames'] == 36

    def test_no_frame_written_is_a_failure(self, tmp_path):
        timeline = make_timeline(tmp_path)
        output = tmp_path / 'out.mp4'
        with pytest.raises(render.RenderError):
            render.render(timeline, tmp_path, output, capture=FakeCapture(write=False))
        assert not output.exists()

    def test_capture_error_propagates_and_leaves_nothing(self, tmp_path):
        timeline = make_timeline(tmp_path)
        output = tmp_path / 'out.mp4'
        with pytest.raises(RuntimeError, match='navegador caiu'):
            render.render(timeline, tmp_path, output,
                          capture=FakeCapture(error=RuntimeError('navegador caiu')))
        assert not output.exists()
        assert not (tmp_path / 'out.part.mp4').exists()

    def test_missing_track_is_refused(self, tmp_path):
        timeline = make_timeline(tmp_path)
        (tmp_path / 'audio' / 'track.wav').unlink()
        with pytest.raises(render.RenderError, match='Trilha de áudio ausente'):
            render.render(timeline, tmp_path, tmp_path / 'out.mp4', capture=FakeCapture())

    def test_wrong_frame_size_is_a_failure(self, tmp_path):
        timeline = make_timeline(tmp_path)
        output = tmp_path / 'out.mp4'
        small = image_bytes(size=(320, 568))
        with pytest.raises(render.RenderError, match='contrato'):
            render.render(timeline, tmp_path, output, capture=FakeCapture(frame=small))
        assert not output.exists()

    def test_inspect_reports_the_real_streams(self, tmp_path):
        timeline = make_timeline(tmp_path)
        output = tmp_path / 'out.mp4'
        render.render(timeline, tmp_path, output, capture=FakeCapture())
        info = render.inspect_output(output)
        assert info['has_video'] and info['has_audio']
        assert (info['width'], info['height']) == (1080, 1920)
        assert info['duration'] == pytest.approx(1.2, abs=0.2)

    def test_progress_reaches_hundred(self, tmp_path):
        timeline = make_timeline(tmp_path)
        seen: list[int] = []
        render.render(timeline, tmp_path, tmp_path / 'out.mp4', capture=FakeCapture(),
                      progress=seen.append)
        assert seen[-1] == 100
        assert any(0 < value < 100 for value in seen)


class TestPlayer:
    def test_html_embeds_every_image_and_calls_seek(self, tmp_path):
        timeline = make_timeline(tmp_path)
        html = player.render_html(timeline, tmp_path)
        assert html.count('data:image/jpeg;base64,') == 1
        assert 'window.seek' in html
        assert 'http://' not in html and 'https://' not in html
        assert '__CUE_SHIFT__' not in html and '__PAYLOAD__' not in html

    def test_missing_image_is_explicit(self, tmp_path):
        timeline = make_timeline(tmp_path)
        (tmp_path / 'images' / 'scene-0.jpg').unlink()
        with pytest.raises(FileNotFoundError, match='Imagem da cena'):
            player.render_html(timeline, tmp_path)

    def test_payload_carries_times_and_captions(self, tmp_path):
        timeline = make_timeline(tmp_path)
        data = player.payload(timeline, tmp_path)
        assert data['duration'] == pytest.approx(1.2)
        assert data['scenes'][0]['start'] == 0 and data['scenes'][0]['end'] == pytest.approx(1.2)
        assert data['cues'][0]['text'] == 'Uma linha'
