"""Voz sintética do modo história: três vozes do sistema, nenhuma clonada.

- `narrador` conduz a história;
- `personagem` dá voz a alguém dentro da cena;
- `sussurro` carrega segredo/tensão (mais baixo e mais lento).

As três são vozes neurais públicas do edge-tts, escolhidas por tom. Não existe
caminho para clonar voz de pessoa real: não há amostra de referência, não há
VoiceStudio e nenhum parâmetro de clonagem é aceito (ver `script.reject_voice_cloning`).

Os tempos de cada palavra vêm do próprio motor de síntese (`WordBoundary`), que é
a mesma fonte de tempo usada pela timeline. Quando o motor não devolve esses
eventos, a fala é marcada como `estimated` e as legendas são distribuídas dentro
do espaço declarado da fala — nunca se finge precisão que não existe.
"""
from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from backend.services import subtitle_timing
from backend.utils.ffmpeg_utils import get_ffmpeg_path

logger = logging.getLogger(__name__)

# O serviço de voz devolve `NoAudioReceived` de forma intermitente (é o mesmo
# tropeço de DRM que o edge-tts tenta contornar com um retry interno). Uma fala
# que falha sozinha não pode derrubar uma produção de 40 s.
SYNTH_ATTEMPTS = 3
SYNTH_RETRY_SECONDS = 0.8

VOICES: dict[str, dict[str, str]] = {
    'narrador': {'id': 'pt-BR-AntonioNeural', 'label': 'Antonio (narrador, grave)'},
    'personagem': {'id': 'pt-BR-FranciscaNeural', 'label': 'Francisca (personagem)'},
    'sussurro': {'id': 'pt-BR-ThalitaNeural', 'label': 'Thalita (sussurro, baixa)'},
}

# Prosódia por tom: o sussurro sai mais baixo e mais lento, o personagem sobe um
# pouco o tom para não se confundir com o narrador.
PROSODY: dict[str, dict[str, str]] = {
    'narrador': {},
    'personagem': {'pitch': '+14Hz'},
    'sussurro': {'volume': '-32%', 'rate': '-10%'},
}

# `synth` recebe (texto, id_voz, prosódia) e devolve (bytes do áudio, eventos WordBoundary crus).
Synth = Callable[[str, str, dict[str, str]], tuple[bytes, list[dict[str, Any]]]]


class VoiceError(RuntimeError):
    """Falha de síntese: o build para em vez de gravar um vídeo mudo."""


def _edge_once(text: str, voice: str, prosody: dict[str, str]) -> tuple[bytes, list[dict[str, Any]]]:
    """Uma tentativa de síntese, com os eventos de palavra do próprio motor."""
    import asyncio

    import edge_tts

    audio = bytearray()
    events: list[dict[str, Any]] = []

    async def run() -> None:
        communicate = edge_tts.Communicate(text, voice, boundary='WordBoundary', **prosody)
        async for chunk in communicate.stream():
            if chunk['type'] == 'audio':
                audio.extend(chunk['data'])
            elif chunk['type'] == 'WordBoundary':
                events.append({
                    'type': 'WordBoundary',
                    'offset': chunk['offset'],
                    'duration': chunk['duration'],
                    'text': chunk['text'],
                })

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(run())
    finally:
        loop.close()
    if not audio:
        raise VoiceError('O motor de voz não devolveu áudio para esta fala')
    return bytes(audio), events


def _edge_synth(text: str, voice: str, prosody: dict[str, str]) -> tuple[bytes, list[dict[str, Any]]]:
    """Sintetiza uma fala e captura os eventos de palavra do edge-tts.

    `boundary="WordBoundary"` é obrigatório: no edge-tts 7.x o padrão passou a ser
    `SentenceBoundary`, que devolve uma marca por frase e não serve para legenda
    palavra a palavra.
    """
    last: Exception | None = None
    for attempt in range(SYNTH_ATTEMPTS):
        try:
            return _edge_once(text, voice, prosody)
        except Exception as error:  # noqa: BLE001 - motivo real vai no VoiceError final
            last = error
            if attempt + 1 < SYNTH_ATTEMPTS:
                logger.info('Voz %s falhou (tentativa %s); repetindo: %s', voice, attempt + 1,
                            type(error).__name__)
                time.sleep(SYNTH_RETRY_SECONDS * (attempt + 1))
    raise VoiceError(f'Falha ao sintetizar a fala com o edge-tts (voz {voice}): {last}') from last


def synthesize(text: str, tone: str, out_path: Path, *, synth: Synth | None = None
               ) -> tuple[list[str], str]:
    """Grava o áudio de uma fala e o sidecar de palavras; devolve (palavras, precisão).

    Retorno: (textos das palavras, 'words' quando veio do motor, 'estimated' quando não).
    """
    if tone not in VOICES:
        raise VoiceError(f'Tom de voz desconhecido: {tone}')
    text = (text or '').strip()
    if not text:
        raise VoiceError('Fala vazia não pode ser sintetizada')
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run = synth or _edge_synth
    try:
        audio, events = run(text, VOICES[tone]['id'], dict(PROSODY[tone]))
    except VoiceError:
        raise
    except Exception as error:  # noqa: BLE001 - motor externo, causa vai no texto
        raise VoiceError(f'Falha ao sintetizar a fala: {error}') from error
    if not audio:
        raise VoiceError('O motor de voz não devolveu áudio para esta fala')
    out_path.write_bytes(audio)

    timing = subtitle_timing
    words = timing.words_from_boundaries(events, text) if events else []
    if words:
        # Sidecar no próprio formato do edge-tts: qualquer consumidor do projeto
        # (inclusive a dublagem) lê sem conversão.
        timing.write_words_sidecar(out_path, words)
        return [word.text for word in words], 'words'
    return [], 'estimated'


def words_of(audio_path: Path) -> list[subtitle_timing.Word]:
    """Palavras já gravadas no sidecar (tempos relativos ao início da fala)."""
    return subtitle_timing.read_words_sidecar(audio_path)


def duration_seconds(path: Path) -> float:
    """Duração real do áudio lido pelo ffmpeg.

    Esta máquina não precisa de ffprobe: o ffmpeg imprime `Duration:` no stderr e
    `subtitle_timing.parse_ffmpeg_duration` já extrai esse valor.
    """
    path = Path(path)
    if not path.is_file():
        raise VoiceError(f'Áudio não encontrado: {path}')
    try:
        proc = subprocess.run([get_ffmpeg_path(), '-hide_banner', '-i', str(path)],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as error:
        raise VoiceError(f'Não consegui medir a duração do áudio: {error}') from error
    seconds = subtitle_timing.parse_ffmpeg_duration(proc.stderr or '')
    if seconds <= 0:
        raise VoiceError(f'Não consegui medir a duração de {path.name}')
    return seconds


def mix_track(timeline, project_dir: Path, out_path: Path) -> float:
    """Monta a trilha única do vídeo posicionando cada fala no seu tempo da timeline.

    Um WAV de 48 kHz sem perda: o AAC só é codificado uma vez, no mux final. Os
    tempos vêm do timeline.json, então mexer no roteiro muda a trilha sem tocar
    neste código.
    """
    lines = [line for scene in timeline.scenes for line in scene.lines]
    if not lines:
        raise VoiceError('A timeline não tem nenhuma fala para montar a trilha')
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sources = [Path(project_dir) / line.audio for line in lines]
    missing = [str(path.name) for path in sources if not path.is_file()]
    if missing:
        raise VoiceError('Áudio de fala ausente: ' + ', '.join(missing))

    cmd = [get_ffmpeg_path(), '-v', 'error']
    for source in sources:
        cmd += ['-i', str(source)]
    parts = []
    for index, line in enumerate(lines):
        delay_ms = max(0, int(round(line.start * 1000)))
        parts.append(f'[{index}:a]aresample=48000,'
                     f'aformat=sample_fmts=fltp:channel_layouts=stereo,'
                     f'adelay={delay_ms}:all=1[a{index}]')
    mix = ''.join(f'[a{index}]' for index in range(len(lines)))
    graph = ';'.join(parts) + (
        f';{mix}amix=inputs={len(lines)}:normalize=0:dropout_transition=0,'
        f'apad,atrim=duration={timeline.duration:.3f},asetpts=PTS-STARTPTS[track]')
    cmd += ['-filter_complex', graph, '-map', '[track]', '-c:a', 'pcm_s16le', '-ar', '48000',
            '-ac', '2', '-y', str(out_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as error:
        raise VoiceError(f'Falha ao montar a trilha de áudio: {error}') from error
    if proc.returncode or not out_path.is_file():
        raise VoiceError('Falha ao montar a trilha de áudio: '
                         + (proc.stderr or '')[-400:].strip())
    return timeline.duration
