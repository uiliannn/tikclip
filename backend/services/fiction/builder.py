"""Gerador da timeline a partir do roteiro.

Recebe o roteiro (6 cenas) e o que já foi produzido para ele — caminho e duração
de cada fala, mais as imagens — e devolve a `Timeline` completa, com todos os
tempos calculados. É uma função pura: não toca em disco, não chama modelo, não
usa rede. Por isso o formato pode ser testado sem TTS e sem provedor de imagem.

Regra de tempo: cada cena dura o que as suas falas duram, mais uma respiração no
começo e no fim. Se o total ficar abaixo de 30 s, o excedente é distribuído como
pausa no fim das cenas (a imagem continua na tela) — nunca se acelera a fala.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from backend.services.fiction.timeline import (
    MAX_SECONDS,
    MIN_SECONDS,
    SCENE_COUNT,
    Cue,
    KenBurns,
    Line,
    Scene,
    Style,
    Timeline,
    TimelineError,
    Transition,
)

LEAD_IN = 0.25          # respiro antes da primeira fala da cena
TAIL_OUT = 0.35         # respiro depois da última fala
MAX_STRETCH = 4.0       # pausa extra total aceitável para chegar aos 30 s
CAPTION_SECONDS = 2.8   # tempo máximo de uma tela de legenda
CAPTION_GAP = 0.45      # silêncio que já sugere nova tela
MIN_CUE = 0.20

# Zoom/pan por cena: alterna foco e intensidade para o vídeo não parecer estático.
KEN_BURNS = (
    ('center', 1.02, 1.10),
    ('top', 1.04, 1.12),
    ('bottom', 1.03, 1.11),
    ('center', 1.06, 1.14),
    ('top', 1.02, 1.10),
    ('bottom', 1.05, 1.13),
)

# (texto, início, fim) relativo ao começo do áudio da fala.
WordTimes = tuple[tuple[str, float, float], ...]


@dataclass(frozen=True)
class SpokenLine:
    """Uma fala já sintetizada, pronta para entrar na timeline."""

    audio: str
    duration: float
    words: WordTimes = ()
    timing: str = 'estimated'
    words_path: str | None = None


@dataclass
class BuildInput:
    project_id: str
    title: str
    description: str = ''
    tags: list[str] = field(default_factory=list)
    palette: str = 'azure'
    style: Style | None = None
    audio: str = 'audio/track.wav'


def caption_chunks(text: str, max_chars: int) -> list[str]:
    """Quebra o texto em telas de legenda sem cortar palavra."""
    max_chars = max(8, int(max_chars))
    chunks: list[str] = []
    current = ''
    for word in (text or '').split():
        candidate = f'{current} {word}'.strip()
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = word
        else:
            current = candidate
    if current:
        chunks.append(current)
    out: list[str] = []
    for chunk in chunks:
        while len(chunk) > max_chars:
            out.append(chunk[:max_chars])
            chunk = chunk[max_chars:]
        if chunk:
            out.append(chunk)
    return out


def accent_colour(palette: str) -> str:
    """A mesma paleta das capas e do packaging, em #rrggbb."""
    try:
        from backend.services.studio.packaging_render import colours
        raw = colours(palette)['accent_hex']  # '0xRRGGBB'
        return '#' + raw[2:]
    except Exception:  # noqa: BLE001 - paleta desconhecida usa o azul padrão
        return '#5A8BFF'


def _cues_for_words(line: Line, words: WordTimes, max_chars: int) -> list[Cue]:
    from backend.services import subtitle_timing
    shifted = [subtitle_timing.Word(text=text, start=line.start + start, end=line.start + end)
               for text, start, end in words]
    cues: list[Cue] = []
    for cue in subtitle_timing.group_words_into_cues(
            shifted, max_chars=max_chars, max_duration=CAPTION_SECONDS, max_gap=CAPTION_GAP):
        start = max(line.start, cue.start)
        end = min(line.end, max(cue.end, start + MIN_CUE))
        if end > start:
            cues.append(Cue(start=round(start, 3), end=round(end, 3), text=cue.text[:120]))
    return cues


def _cues_for_text(line: Line, max_chars: int) -> list[Cue]:
    chunks = caption_chunks(line.text, max_chars)
    total_chars = sum(len(chunk) for chunk in chunks) or 1
    length = line.end - line.start
    cues: list[Cue] = []
    cursor = line.start
    for index, chunk in enumerate(chunks):
        remaining = len(chunks) - index - 1
        span = length * len(chunk) / total_chars if remaining else max(MIN_CUE, line.end - cursor)
        end = min(line.end, cursor + max(MIN_CUE, span))
        if end <= cursor:
            end = cursor + MIN_CUE
        cues.append(Cue(start=round(cursor, 3), end=round(end, 3), text=chunk[:120]))
        cursor = end
    return cues


def cues_for_line(line: Line, words: WordTimes, max_chars: int) -> list[Cue]:
    """Legendas de uma fala: pelos tempos reais das palavras, ou proporcionais."""
    if len(words) >= 2:
        cues = _cues_for_words(line, words, max_chars)
        if cues:
            return cues
    return _cues_for_text(line, max_chars)


def build_timeline(script, images: Sequence[str], sounds: Sequence[SpokenLine],
                   spec: BuildInput) -> Timeline:
    """Monta a timeline completa (cenas, tempos, legendas) a partir do roteiro."""
    if len(script.scenes) != SCENE_COUNT:
        raise TimelineError(f'O roteiro precisa de {SCENE_COUNT} cenas')
    if len(images) != len(script.scenes):
        raise TimelineError(f'{len(images)} imagens para {len(script.scenes)} cenas')
    expected = sum(len(scene.lines) for scene in script.scenes)
    if len(sounds) != expected:
        raise TimelineError(f'{len(sounds)} áudios de fala para {expected} falas do roteiro')

    style = spec.style or Style(accent=accent_colour(spec.palette))
    max_chars = style.caption_max_chars

    # 1) Duração crua de cada cena: respiro + falas + respiro.
    plan: list[dict] = []
    sound_iter = iter(sounds)
    for index, (scene_script, image) in enumerate(zip(script.scenes, images)):
        pairs = []
        length = LEAD_IN + TAIL_OUT
        for line_script in scene_script.lines:
            spoken = next(sound_iter)
            if spoken.duration <= 0:
                raise TimelineError('Uma fala veio sem duração de áudio')
            length += spoken.duration
            pairs.append((line_script, spoken))
        focus, start_scale, end_scale = KEN_BURNS[index % len(KEN_BURNS)]
        plan.append({'index': index, 'image': image, 'prompt': scene_script.image_prompt,
                     'pairs': pairs, 'length': length,
                     'ken_burns': KenBurns(start_scale=start_scale, end_scale=end_scale,
                                           focus=focus)})

    total = sum(item['length'] for item in plan)
    if total > MAX_SECONDS:
        raise TimelineError(
            f'A história ficou com {total:.1f}s de fala e o limite é {MAX_SECONDS:.0f}s. '
            'Corte algumas palavras do roteiro e gere de novo.')
    if total < MIN_SECONDS:
        deficit = MIN_SECONDS - total
        if deficit > MAX_STRETCH:
            raise TimelineError(
                f'A história ficou com {total:.1f}s e faltam {deficit:.1f}s para '
                f'{MIN_SECONDS:.0f}s; a pausa máxima aceita é {MAX_STRETCH:.0f}s. '
                'Escreva um roteiro um pouco maior.')
        # Pausa extra no fim de cada cena: a imagem respira, a fala nunca acelera.
        extra = deficit / len(plan)
        for item in plan:
            item['length'] += extra

    # 2) Tempos absolutos e falas posicionadas.
    cursor = 0.0
    scenes: list[Scene] = []
    cues: list[Cue] = []
    timing_sources: set[str] = set()
    tones: set[str] = set()
    for item in plan:
        start = cursor
        end = round(start + item['length'], 3)
        line_cursor = start + LEAD_IN
        lines: list[Line] = []
        for line_index, (line_script, spoken) in enumerate(item['pairs']):
            line_start = round(line_cursor, 3)
            line_end = round(line_start + spoken.duration, 3)
            line = Line(id=f's{item["index"] + 1}-l{line_index + 1}', tone=line_script.tone,
                        text=line_script.text, start=line_start, end=line_end,
                        audio=spoken.audio, words=spoken.words_path, timing=spoken.timing)
            lines.append(line)
            cues.extend(cues_for_line(line, spoken.words, max_chars))
            timing_sources.add(spoken.timing)
            tones.add(line_script.tone)
            line_cursor = line_end
        scenes.append(Scene(id=f's{item["index"] + 1}', index=item['index'],
                            start=round(start, 3), end=end, image=item['image'],
                            prompt=item['prompt'],
                            transition=Transition(type='fade',
                                                  duration=0.6 if item['index'] == 0 else 0.35),
                            ken_burns=item['ken_burns'], lines=lines))
        cursor = end

    return Timeline(
        project_id=spec.project_id, title=spec.title, description=spec.description,
        tags=list(spec.tags), style=style, audio=spec.audio, scenes=scenes,
        cues=sorted(cues, key=lambda cue: cue.start),
        meta={'word_count': script.word_count,
              'tones': sorted(tones),
              'timing': sorted(timing_sources),
              'generator': 'fiction-v1'})
