"""Timeline declarativa do modo história ("o vídeo é código").

A composição inteira vive num arquivo de dados, ``metadata/fiction/timeline.json``,
dentro do projeto: cenas, tempos, transição, imagem, falas, áudio e as legendas
que serão queimadas. O renderizador lê este arquivo e mais nada — trocar o
roteiro e rodar de novo produz outro vídeo sem tocar em código.

O formato garante, por validação:
- cenas contíguas, começando em 0, sem buraco e sem sobreposição;
- toda legenda cai dentro da cena e da fala que a gerou;
- tempos sempre em segundos absolutos do vídeo final (o mesmo relógio que o
  renderizador usa), nunca relativos à cena.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Formato fixo do modo história: vertical, 30 fps, H.264 + AAC.
WIDTH, HEIGHT, FPS = 1080, 1920, 30
SCENE_COUNT = 6
MIN_SECONDS, MAX_SECONDS = 30.0, 45.0
TONES = ('narrador', 'personagem', 'sussurro')
Tone = Literal['narrador', 'personagem', 'sussurro']

# Margem de tolerância ao comparar tempos já gravados (somas de ponto flutuante).
EPSILON = 0.05


class TimelineError(ValueError):
    """Timeline inválida: o projeto falha em vez de renderizar algo errado."""


class Transition(BaseModel):
    """Transição de entrada da cena, em segundos absolutos da timeline."""

    model_config = ConfigDict(extra='forbid')
    type: Literal['cut', 'fade'] = 'fade'
    duration: float = Field(default=.35, ge=0, le=1.5)


class KenBurns(BaseModel):
    """Zoom/pan sobre a imagem parada; dá vida à cena sem vídeo de origem."""

    model_config = ConfigDict(extra='forbid')
    start_scale: float = Field(default=1.02, ge=1, le=1.4)
    end_scale: float = Field(default=1.10, ge=1, le=1.4)
    focus: Literal['center', 'top', 'bottom'] = 'center'

    @model_validator(mode='after')
    def grows(self):
        if self.end_scale < self.start_scale:
            raise ValueError('O zoom final não pode ser menor que o inicial')
        return self


class Cue(BaseModel):
    """Uma tela de legenda queimada, no relógio da timeline."""

    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str = Field(min_length=1, max_length=120)

    @model_validator(mode='after')
    def visible(self):
        if self.end - self.start < .12:
            raise ValueError('Legenda curta demais para ser lida')
        return self


class Line(BaseModel):
    """Uma fala sintetizada: quem fala, o tom, o texto e o áudio gerado."""

    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,60}$')
    tone: Tone
    text: str = Field(min_length=1, max_length=400)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    # Caminhos relativos ao próprio timeline.json: o projeto continua portátil.
    audio: str = Field(min_length=1, max_length=400)
    words: str | None = Field(default=None, max_length=400)
    # 'words': tempos vieram do WordBoundary do TTS; 'estimated': distribuídos
    # dentro do espaço da fala. Fica gravado para não fingir precisão que não há.
    timing: Literal['words', 'estimated'] = 'estimated'

    @model_validator(mode='after')
    def ordered(self):
        if self.end - self.start < .12:
            raise ValueError('A fala precisa de pelo menos 0,12 s')
        return self


class Scene(BaseModel):
    """Uma cena: uma imagem, uma transição e as falas ditas sobre ela."""

    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,60}$')
    index: int = Field(ge=0, le=99)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    image: str = Field(min_length=1, max_length=400)
    prompt: str = Field(default='', max_length=700)
    transition: Transition = Field(default_factory=Transition)
    ken_burns: KenBurns = Field(default_factory=KenBurns)
    lines: list[Line] = Field(min_length=1, max_length=4)

    @model_validator(mode='after')
    def consistent(self):
        if self.end - self.start < 1:
            raise ValueError('Cada cena precisa de pelo menos 1 segundo')
        for line in self.lines:
            if line.start < self.start - EPSILON or line.end > self.end + EPSILON:
                raise ValueError(f'A fala {line.id} cai fora da cena {self.id}')
        return self


class Style(BaseModel):
    """Aparência da legenda e do cartão de título (a UI também lê estes campos)."""

    model_config = ConfigDict(extra='forbid')
    font: str = Field(default='Segoe UI', max_length=80)
    caption_size: int = Field(default=62, ge=28, le=120)
    caption_y: float = Field(default=.72, ge=.45, le=.88, allow_inf_nan=False)
    caption_max_chars: int = Field(default=34, ge=12, le=60)
    text_color: str = Field(default='#FFFFFF', pattern=r'^#[0-9a-fA-F]{6}$')
    accent: str = Field(default='#5A8BFF', pattern=r'^#[0-9a-fA-F]{6}$')
    shadow: bool = True
    letterbox: bool = True


class Timeline(BaseModel):
    """O arquivo de composição completo de um vídeo do modo história."""

    model_config = ConfigDict(extra='forbid')
    version: Literal[1] = 1
    project_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,100}$')
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default='', max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=12)
    width: Literal[1080] = WIDTH
    height: Literal[1920] = HEIGHT
    fps: Literal[30] = FPS
    style: Style = Field(default_factory=Style)
    # Trilha única já concatenada; o renderizador só muxa.
    audio: str = Field(min_length=1, max_length=400)
    scenes: list[Scene] = Field(min_length=1, max_length=12)
    cues: list[Cue] = Field(default_factory=list, max_length=400)
    meta: dict = Field(default_factory=dict)

    @model_validator(mode='after')
    def ordered(self):
        first = self.scenes[0]
        if abs(first.start) > EPSILON:
            raise ValueError('A primeira cena precisa começar em 0')
        for previous, current in zip(self.scenes, self.scenes[1:]):
            if abs(current.start - previous.end) > EPSILON:
                raise ValueError(
                    f'A cena {current.id} começa em {current.start:.3f}s mas a anterior '
                    f'termina em {previous.end:.3f}s: cenas precisam ser contíguas')
        indexes = [scene.index for scene in self.scenes]
        if indexes != list(range(len(self.scenes))):
            raise ValueError('As cenas precisam estar numeradas de 0 a N-1, na ordem')
        for cue in self.cues:
            if not any(scene.start - EPSILON <= cue.start and cue.end <= scene.end + EPSILON
                       for scene in self.scenes):
                raise ValueError(f'A legenda "{cue.text[:24]}" cai fora das cenas')
        return self

    @property
    def duration(self) -> float:
        return round(self.scenes[-1].end, 3)

    def cue_at(self, at: float) -> list[Cue]:
        """Legendas visíveis no instante `at` (usado pelo player e pelos testes)."""
        return [cue for cue in self.cues if cue.start <= at < cue.end]

    def to_payload(self) -> dict:
        """Dados prontos para o JSON do timeline.json."""
        return self.model_dump(mode='json')


def validate_broadcast_ready(timeline: Timeline) -> None:
    """Confere as regras de publicação do modo (duração, cenas, legendas).

    Separado do schema de propósito: o schema garante integridade a qualquer
    momento; esta checagem só vale para o vídeo final, então um teste de render
    pode usar uma timeline curta sem mentir sobre a regra de 30–45 s.
    """
    problems = []
    if len(timeline.scenes) != SCENE_COUNT:
        problems.append(f'{len(timeline.scenes)} cenas (esperado {SCENE_COUNT})')
    if not MIN_SECONDS <= timeline.duration <= MAX_SECONDS:
        problems.append(f'{timeline.duration:.2f}s de duração (esperado {MIN_SECONDS:.0f}–'
                        f'{MAX_SECONDS:.0f}s)')
    if not timeline.cues:
        problems.append('nenhuma legenda')
    if not any(scene.lines for scene in timeline.scenes):
        problems.append('nenhuma fala')
    if problems:
        raise TimelineError('Timeline fora do padrão de publicação: ' + '; '.join(problems))


def timeline_path(project_dir: Path) -> Path:
    return Path(project_dir) / 'metadata' / 'fiction' / 'timeline.json'


def load(project_dir: Path) -> Timeline:
    path = timeline_path(project_dir)
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError as error:
        raise TimelineError(f'Timeline não encontrada em {path}') from error
    except (OSError, ValueError) as error:
        raise TimelineError(f'Não consegui ler a timeline: {error}') from error
    if not isinstance(raw, dict):
        raise TimelineError('A timeline precisa ser um objeto JSON')
    try:
        return Timeline.model_validate(raw)
    except ValueError as error:
        raise TimelineError(f'Timeline inválida: {error}') from error


def save(project_dir: Path, timeline: Timeline) -> Path:
    """Grava de forma atômica: timeline pela metade nunca é lida por engano."""
    path = timeline_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        temporary.write_text(json.dumps(timeline.to_payload(), ensure_ascii=False, indent=2),
                             encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
