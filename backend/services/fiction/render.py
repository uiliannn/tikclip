"""Renderizador do modo história: HTML/seek -> quadros -> FFmpeg -> MP4.

Fluxo:
1. a página (`player.py`) é carregada no Chromium via Playwright, já com as
   imagens embutidas em data URI (nenhum acesso a arquivo local é necessário);
2. para cada quadro, `seek(t)` posiciona a composição e um screenshot JPEG é
   escrito no stdin do FFmpeg — nada de gravação de tela em tempo real, então
   não há quadro perdido nem deriva de tempo;
3. o FFmpeg muxa esses quadros com a trilha de áudio já montada e entrega
   H.264 + AAC, 1080x1920, 30 fps.

Falha em qualquer etapa levanta exceção e não deixa arquivo de saída: quem
chamou marca o projeto como falho, como em `processing_orchestrator`.
"""
from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from backend.services import render_limits, video_encoder
from backend.services.fiction import player
from backend.utils.ffmpeg_utils import get_ffmpeg_path

logger = logging.getLogger(__name__)

WIDTH, HEIGHT = 1080, 1920
CAPTURE_TIMEOUT_MS = 120_000
# Tolerância entre a duração pedida e a entregue (o AAC tem padding de quadro).
DURATION_TOLERANCE = 0.6
# Sem `playwright install`, o Chromium empacotado não existe; o navegador do
# sistema (o mesmo que os publicadores usam) resolve sem download.
CHANNELS = ('msedge', 'chrome')
_SIZE = re.compile(r'Video:.*?(\d{2,5})x(\d{2,5})')

# `capture` recebe (html, fps, total_quadros, escreve_quadro, progresso).
FrameSink = Callable[[bytes], None]
FrameProgress = Callable[[int, int], None]
Capture = Callable[[str, int, int, FrameSink, FrameProgress | None], None]
# `progress` do render recebe a porcentagem (0–100).
Progress = Callable[[int], None]


class RenderError(RuntimeError):
    """Falha de render: nenhum MP4 parcial é entregue como se fosse o vídeo final."""


LAUNCH_ARGS = ['--hide-scrollbars', '--force-color-profile=srgb', '--disable-lcd-text',
               '--font-render-hinting=none', '--disable-gpu']


def _launch(playwright):
    """Chromium do Playwright; se não estiver baixado, o navegador do sistema.

    O TikClip já publica pelo Edge/Chrome instalado, então exigir um segundo
    navegador só para capturar a composição não faz sentido.
    """
    attempts = [None, *CHANNELS]
    problems = []
    for channel in attempts:
        try:
            options = {'headless': True, 'args': LAUNCH_ARGS}
            if channel:
                options['channel'] = channel
            return playwright.chromium.launch(**options)
        except Exception as error:  # noqa: BLE001 - tenta o próximo navegador
            problems.append(f'{channel or "chromium"}: {str(error)[:160]}')
            logger.warning('Navegador %s indisponível para a captura: %s',
                           channel or 'chromium', type(error).__name__)
    raise RenderError('Nenhum navegador disponível para capturar a composição ('
                      + '; '.join(problems) + ')')


def capture_frames(html: str, fps: int, total: int, write: FrameSink,
                   progress: FrameProgress | None = None) -> None:
    """Captura `total` quadros determinísticos da página, um por `seek(t)`."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:  # pragma: no cover - dependência de requirements.txt
        raise RenderError('Playwright não está instalado; não consigo capturar a composição') \
            from error

    with sync_playwright() as playwright:
        browser = _launch(playwright)
        try:
            context = browser.new_context(
                viewport={'width': WIDTH, 'height': HEIGHT}, device_scale_factor=1,
                reduced_motion='reduce', color_scheme='dark')
            page = context.new_page()
            page.set_default_timeout(CAPTURE_TIMEOUT_MS)
            page.set_content(html, wait_until='load')
            page.wait_for_function('window.__ready === true')
            for index in range(total):
                page.evaluate('(t) => window.seek(t)', index / fps)
                write(page.screenshot(type='jpeg', quality=95))
                if progress:
                    progress(index + 1, total)
        finally:
            browser.close()


def _ffmpeg_command(audio: Path, target: Path, fps: int, encoder_name: str | None) -> list[str]:
    return [get_ffmpeg_path(), '-v', 'error',
            '-f', 'image2pipe', '-vcodec', 'mjpeg', '-framerate', str(fps), '-i', '-',
            '-i', str(audio),
            *video_encoder.h264_args(WIDTH, HEIGHT, encoder_name),
            '-r', str(fps),
            *render_limits.output_args(),
            '-c:a', 'aac', '-b:a', '160k', '-ar', '48000', '-ac', '2',
            '-shortest', '-movflags', '+faststart',
            '-y', str(target)]


def inspect_output(path: Path) -> dict:
    """Lê o MP4 pelo ffmpeg e devolve o que realmente saiu (sem depender de ffprobe)."""
    from backend.services.subtitle_timing import parse_ffmpeg_duration

    proc = subprocess.run([get_ffmpeg_path(), '-hide_banner', '-i', str(path)],
                          capture_output=True, text=True, timeout=120)
    stderr = proc.stderr or ''
    size = _SIZE.search(stderr)
    return {
        'has_video': 'Video:' in stderr,
        'has_audio': 'Audio:' in stderr,
        'width': int(size.group(1)) if size else 0,
        'height': int(size.group(2)) if size else 0,
        'duration': round(parse_ffmpeg_duration(stderr), 3),
    }


def _verify_output(path: Path, expected_seconds: float) -> dict:
    """Confere o contrato de publicação antes de chamar o arquivo de vídeo final.

    Sem esta checagem, um encode que perdeu todos os quadros entregaria um MP4
    só com áudio e o projeto seria marcado como pronto com saída falsa.
    """
    info = inspect_output(path)
    if not info['has_video']:
        raise RenderError('O MP4 saiu sem faixa de vídeo (nenhum quadro foi codificado)')
    if not info['has_audio']:
        raise RenderError('O MP4 saiu sem faixa de áudio')
    if (info['width'], info['height']) != (WIDTH, HEIGHT):
        raise RenderError(f'O vídeo saiu em {info["width"]}x{info["height"]}; '
                          f'o contrato é {WIDTH}x{HEIGHT}')
    if info['duration'] < expected_seconds - DURATION_TOLERANCE:
        raise RenderError(f'O vídeo saiu com {info["duration"]}s e a timeline tem '
                          f'{expected_seconds}s')
    return info


def _pump(cmd: list[str], total: int, capture: Capture, html: str, fps: int,
          progress: Progress | None) -> None:
    """Roda um encode completo alimentando os quadros pelo stdin."""
    cmd, priority = render_limits.low_priority(cmd)
    with tempfile.TemporaryFile('w+b') as errors:
        process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=errors, **priority)

        def write(frame: bytes) -> None:
            try:
                process.stdin.write(frame)
            except (BrokenPipeError, ValueError, OSError) as error:
                raise RenderError('O FFmpeg encerrou a codificação antes do fim dos quadros') \
                    from error

        def report(done: int, expected: int) -> None:
            if progress:
                # 5–95%: a captura domina o tempo; o mux é instantâneo.
                progress(5 + round(90 * done / max(1, expected)))

        try:
            capture(html, fps, total, write, report)
        finally:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            # Recolhe o processo mesmo quando a captura falhou: sem isso o
            # ffmpeg ficaria órfão segurando o arquivo parcial.
            try:
                code = process.wait(timeout=120)
            except subprocess.TimeoutExpired:
                process.kill()
                raise RenderError('O FFmpeg não terminou a codificação a tempo') from None
        errors.seek(0)
        tail = errors.read().decode('utf-8', 'replace')[-1200:].strip()
    if code:
        raise RenderError('Falha ao codificar o vídeo: ' + (tail or f'ffmpeg saiu com {code}'))


def render(timeline, project_dir: Path, out_path: Path, *,
           progress: Progress | None = None,
           capture: Capture | None = None,
           encoder_name: str | None = None) -> dict:
    """Renderiza o timeline em MP4 vertical e devolve o descritivo do resultado."""
    project_dir, out_path = Path(project_dir), Path(out_path)
    audio = project_dir / timeline.audio
    if not audio.is_file():
        raise RenderError(f'Trilha de áudio ausente: {audio}')
    fps = timeline.fps
    total = max(1, round(timeline.duration * fps))
    html = player.render_html(timeline, project_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    partial = out_path.with_name(out_path.stem + '.part' + out_path.suffix)
    sink = capture or capture_frames

    def attempt(name: str | None) -> None:
        _pump(_ffmpeg_command(audio, partial, fps, name), total, sink, html, fps, progress)

    try:
        try:
            attempt(encoder_name)
        except RenderError:
            chosen = encoder_name or video_encoder.encoder()
            if chosen == video_encoder.SOFTWARE:
                raise
            # Encoder de hardware recusou o fluxo: cai para libx264 e tenta uma vez.
            video_encoder.mark_broken(chosen)
            attempt(video_encoder.SOFTWARE)
        if not partial.is_file() or partial.stat().st_size == 0:
            raise RenderError('O FFmpeg não gerou o arquivo de vídeo')
        info = _verify_output(partial, timeline.duration)
        partial.replace(out_path)
    finally:
        partial.unlink(missing_ok=True)
    if progress:
        progress(100)
    return {'path': str(out_path), 'duration': timeline.duration, 'width': timeline.width,
            'height': timeline.height, 'fps': fps, 'frames': total,
            'bytes': out_path.stat().st_size, 'verified': info}
