"""Alinhamento de legendas a partir dos eventos ``WordBoundary`` do edge-tts.

Por que isso existe
-------------------
Quando a legenda é gerada a partir do áudio **dublado**, o Whisper não é a
fonte de verdade: quem sabe exatamente onde cada palavra começa é o próprio
motor de TTS. O edge-tts entrega um evento ``WordBoundary`` por palavra, com
``offset`` e ``duration`` em *ticks* de 100 ns.

Este módulo transforma esses eventos em cues de SRT. Ele é puro (só stdlib),
para ser testável sem rede, sem ffmpeg e sem o pacote ``edge-tts`` instalado.

Convenções
----------
- Tempo sempre em segundos (float) na API pública.
- ``offset``/``duration`` do edge-tts são convertidos de ticks na entrada.
- Nenhuma função aqui levanta exceção por dado estranho: entrada inválida
  vira lista vazia ou cue descartado. O chamador decide o fallback.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

# O edge-tts expressa offset/duration em ticks de 100 nanossegundos.
TICKS_PER_SECOND = 10_000_000

# Duração mínima de um cue, para não gerar legendas invisíveis.
MIN_CUE_DURATION = 0.20

# Texto padrão quando o TTS não devolveu nenhum WordBoundary.
_EMPTY_TEXT_FALLBACK_SECONDS = 0.0


@dataclass(frozen=True)
class Word:
    """Uma palavra falada, com início e fim absolutos em segundos."""

    text: str
    start: float
    end: float


@dataclass(frozen=True)
class Cue:
    """Um bloco de legenda."""

    start: float
    end: float
    text: str


# --------------------------------------------------------------------------
# conversão de ticks
# --------------------------------------------------------------------------
def ticks_to_seconds(ticks: Any) -> float:
    """Converte ticks de 100 ns em segundos, tolerando lixo na entrada."""
    try:
        value = float(ticks)
    except (TypeError, ValueError):
        return 0.0
    if value != value or value < 0:  # NaN ou negativo
        return 0.0
    return value / TICKS_PER_SECOND


def _seconds_to_ticks(seconds: float) -> int:
    return int(round(max(0.0, seconds) * TICKS_PER_SECOND))


# --------------------------------------------------------------------------
# entrada: eventos do edge-tts
# --------------------------------------------------------------------------
def words_from_boundaries(
    boundaries: Iterable[dict[str, Any]],
    fallback_text: str = "",
) -> list[Word]:
    """Converte eventos ``WordBoundary`` em :class:`Word`.

    Aceita o dicionário cru entregue por ``Communicate.stream()``. Eventos
    com texto vazio são ignorados. Se nada sobrar e ``fallback_text`` vier
    preenchido, devolve uma única palavra cobrindo o texto inteiro — assim o
    chamador nunca fica sem legenda quando o áudio existe.
    """
    words: list[Word] = []
    for event in boundaries or ():
        if not isinstance(event, dict):
            continue
        # Alguns consumidores recebem o evento dentro de "data".
        if event.get("type") not in (None, "WordBoundary"):
            continue
        text = str(event.get("text") or "").strip()
        if not text:
            continue
        start = ticks_to_seconds(event.get("offset"))
        duration = ticks_to_seconds(event.get("duration"))
        end = start + duration
        if end < start:
            end = start
        words.append(Word(text=text, start=start, end=end))

    if words:
        # Ordena por tempo: o edge-tts normalmente já vem ordenado, mas um
        # evento fora de ordem geraria cue invertido.
        words.sort(key=lambda w: (w.start, w.end))

    if not words and fallback_text.strip():
        words.append(
            Word(text=fallback_text.strip(), start=0.0, end=_EMPTY_TEXT_FALLBACK_SECONDS)
        )
    return words


# --------------------------------------------------------------------------
# agrupamento em cues
# --------------------------------------------------------------------------
_STRONG_END = (".", "!", "?", "…", ";", ":", "。", "！", "？", "；", "：")
_SOFT_END = (",", "，", "、", "—", "-")


def _ends_sentence(text: str) -> bool:
    return bool(text) and text.rstrip().endswith(_STRONG_END)


def _ends_clause(text: str) -> bool:
    return bool(text) and text.rstrip().endswith(_SOFT_END)


def group_words_into_cues(
    words: Sequence[Word],
    *,
    max_chars: int = 42,
    max_duration: float = 6.0,
    max_gap: float = 0.8,
) -> list[Cue]:
    """Agrupa palavras em cues legíveis.

    Regras de quebra, na ordem em que são avaliadas ao adicionar uma palavra:

    1. a palavra estoura ``max_chars`` (e o cue já tem conteúdo);
    2. o cue passaria de ``max_duration``;
    3. houve silêncio maior que ``max_gap`` desde a palavra anterior;
    4. a palavra fecha uma frase e o cue já tem pelo menos 40% do limite.

    A quebra por pontuação forte tem prioridade sobre o preenchimento: é o que
    evita legenda cortada no meio de uma oração.
    """
    cues: list[Cue] = []
    if not words:
        return cues

    max_chars = max(8, int(max_chars))
    max_duration = max(0.5, float(max_duration))
    max_gap = max(0.0, float(max_gap))

    current: list[Word] = []

    def flush() -> None:
        if not current:
            return
        text = " ".join(w.text for w in current).strip()
        if text:
            start = current[0].start
            end = max(current[-1].end, start + MIN_CUE_DURATION)
            cues.append(Cue(start=start, end=end, text=text))
        current.clear()

    for word in words:
        if current:
            candidate_len = len(" ".join([w.text for w in current] + [word.text]))
            would_exceed_chars = candidate_len > max_chars
            would_exceed_time = (word.end - current[0].start) > max_duration
            big_gap = (word.start - current[-1].end) > max_gap

            if would_exceed_chars or would_exceed_time or big_gap:
                flush()

        current.append(word)

        filled_enough = len(" ".join(w.text for w in current)) >= max_chars * 0.4
        if _ends_sentence(word.text) and filled_enough:
            flush()

    flush()
    return cues


def cues_from_words_anchored(
    words: Sequence[Word],
    anchor: float,
    *,
    max_chars: int = 42,
    max_duration: float = 6.0,
    max_gap: float = 0.8,
    clamp_to: float | None = None,
) -> list[Cue]:
    """Monta cues relativos ao áudio gerado e desloca para ``anchor``.

    ``clamp_to`` limita o fim do último cue (usado para não invadir a próxima
    fala quando o TTS ficou mais longo que o trecho original).
    """
    anchor = max(0.0, float(anchor or 0.0))
    shifted = [Word(text=w.text, start=w.start + anchor, end=w.end + anchor) for w in words]
    cues = group_words_into_cues(
        shifted, max_chars=max_chars, max_duration=max_duration, max_gap=max_gap
    )
    if not cues or clamp_to is None:
        return cues

    limit = float(clamp_to)
    if limit <= cues[0].start:
        return cues

    clamped: list[Cue] = []
    for cue in cues:
        if cue.start >= limit:
            break
        end = min(cue.end, limit)
        if end <= cue.start:
            end = cue.start + MIN_CUE_DURATION
        clamped.append(Cue(start=cue.start, end=end, text=cue.text))
    return clamped


# --------------------------------------------------------------------------
# saída: SRT
# --------------------------------------------------------------------------
def format_timestamp(seconds: float) -> str:
    """Formata segundos como ``HH:MM:SS,mmm`` (padrão SRT)."""
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        total = 0.0
    if total != total or total < 0:  # NaN ou negativo
        total = 0.0

    millis_total = int(round(total * 1000))
    hours, rem = divmod(millis_total, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def build_srt(cues: Sequence[Cue], *, uppercase: bool = False) -> str:
    """Serializa cues em SRT. Retorna string vazia se não houver cue."""
    blocks: list[str] = []
    index = 0
    for cue in cues:
        text = cue.text.strip()
        if not text:
            continue
        if cue.end <= cue.start:
            continue
        index += 1
        if uppercase:
            text = text.upper()
        blocks.append(
            f"{index}\n"
            f"{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}\n"
            f"{text}\n"
        )
    return "\n".join(blocks)


# --------------------------------------------------------------------------
# sidecar: guardar os WordBoundary ao lado do áudio gerado
# --------------------------------------------------------------------------
def words_to_payload(words: Sequence[Word]) -> list[dict[str, Any]]:
    """Serializa palavras no formato do edge-tts (ticks), para o sidecar."""
    return [
        {
            "type": "WordBoundary",
            "offset": _seconds_to_ticks(w.start),
            "duration": _seconds_to_ticks(max(0.0, w.end - w.start)),
            "text": w.text,
        }
        for w in words
    ]


def words_from_payload(payload: Any) -> list[Word]:
    """Lê o sidecar de volta para :class:`Word`."""
    if not isinstance(payload, list):
        return []
    return words_from_boundaries(payload)


def sidecar_path(audio_path: Path | str) -> Path:
    """``fala_000.mp3`` -> ``fala_000.words.json``."""
    path = Path(audio_path)
    return path.with_suffix(".words.json")


def write_words_sidecar(audio_path: Path | str, words: Sequence[Word]) -> Path | None:
    """Grava o sidecar sem nunca levantar exceção (best-effort)."""
    if not words:
        return None
    try:
        destino = sidecar_path(audio_path)
        destino.write_text(
            json.dumps(words_to_payload(words), ensure_ascii=False),
            encoding="utf-8",
        )
        return destino
    except OSError:
        return None


def read_words_sidecar(audio_path: Path | str) -> list[Word]:
    """Lê o sidecar; ausência ou JSON inválido resultam em lista vazia."""
    try:
        caminho = sidecar_path(audio_path)
        if not caminho.is_file():
            return []
        return words_from_payload(json.loads(caminho.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []


# --------------------------------------------------------------------------
# duração de mídia (para o fallback sem sidecar)
# --------------------------------------------------------------------------
def parse_ffmpeg_duration(stderr: str) -> float:
    """Extrai a duração de ``Duration: HH:MM:SS.ss`` do stderr do ffmpeg."""
    import re

    if not stderr:
        return 0.0
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if not match:
        return 0.0
    hours, minutes, secs = match.groups()
    try:
        return int(hours) * 3600 + int(minutes) * 60 + float(secs)
    except ValueError:
        return 0.0


def cues_from_segment_times(
    segments: Sequence[dict[str, Any]],
    *,
    max_chars: int = 42,
    uppercase: bool = False,
) -> list[Cue]:
    """Fallback puro: um cue por segmento, usando os tempos do transcritor.

    Mantém o comportamento anterior (tempo do Whisper no vídeo original), mas
    quebra trechos longos demais para não estourar a largura da tela.
    """
    cues: list[Cue] = []
    for segment in segments or ():
        try:
            start = float(segment.get("start", 0.0))
            end = float(segment.get("end", start))
        except (TypeError, ValueError):
            continue
        text = str(segment.get("trans") or segment.get("text") or "").strip()
        if not text:
            continue
        if end <= start:
            end = start + MIN_CUE_DURATION
        if len(text) <= max_chars:
            cues.append(Cue(start=start, end=end, text=text))
            continue

        # Divide por palavras e distribui o tempo proporcionalmente.
        palavras = text.split()
        blocos: list[str] = []
        atual = ""
        for palavra in palavras:
            if len(atual) + len(palavra) + 1 <= max_chars:
                atual = f"{atual} {palavra}".strip()
            else:
                if atual:
                    blocos.append(atual)
                atual = palavra
        if atual:
            blocos.append(atual)

        total_chars = sum(len(b) for b in blocos) or 1
        duracao = max(MIN_CUE_DURATION, end - start)
        cursor = start
        for bloco in blocos:
            fatia = duracao * len(bloco) / total_chars
            fim = min(end, cursor + fatia)
            if fim <= cursor:
                fim = cursor + MIN_CUE_DURATION
            cues.append(Cue(start=cursor, end=fim, text=bloco))
            cursor = fim

    if uppercase:
        return [Cue(start=c.start, end=c.end, text=c.text.upper()) for c in cues]
    return cues
