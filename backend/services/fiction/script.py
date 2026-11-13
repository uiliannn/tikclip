"""Roteiro original de ficção para o modo história (uma chamada de LLM).

Saída obrigatória: 6 cenas, 80–100 palavras no total, cada fala marcada com um
tom (narrador / personagem / sussurro) e um prompt de imagem por cena.

Duas regras são de conteúdo, não de formato, e são verificadas aqui de propósito:
- a história é ficção original; "caso real", "história real" e afins são recusados;
- voz é sempre sintética. Clone de voz de pessoa real não é pedido ao modelo e
  não existe em nenhum caminho do modo história (ver `reject_voice_cloning`).
"""
from __future__ import annotations

import re
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.services.fiction.timeline import SCENE_COUNT

Tone = Literal['narrador', 'personagem', 'sussurro']
MIN_WORDS, MAX_WORDS = 80, 100

# Termos que transformam ficção em promessa de verdade: recusados no roteiro.
FORBIDDEN_TERMS = (
    'caso real', 'historia real', 'história real', 'caso veridico', 'caso verídico',
    'baseado em fatos reais', 'fatos reais', 'noticia real', 'notícia real',
)

# Pedido de clonagem de voz: recusado com explicação (requisito do modo).
CLONE_FIELDS = ('clone_voice', 'voice_clone', 'clone', 'voice_sample', 'reference_audio',
                'sample_audio', 'voz_clonada', 'clonar_voz')

CLONE_REFUSAL = (
    'Não clono a voz de uma pessoa real: isso exige o consentimento dela e cria risco '
    'de fraude e de uso indevido da imagem. O modo história usa apenas as vozes '
    'sintéticas do sistema (narrador, personagem e sussurro), que já são distintas entre si.'
)

PROMPT = """\
Você escreve histórias curtas de ficção para vídeos verticais de 40 segundos.
Escreva uma história ORIGINAL de ficção, em português do Brasil, com exatamente
6 cenas. A história precisa de um começo, uma virada e um fim — não é sinopse.

Regras:
- total de 80 a 100 palavras somando todas as falas (conte com atenção);
- cada cena tem 1 ou 2 falas curtas, ditas em voz alta;
- cada fala recebe um tom: "narrador" (conduz a história), "personagem" (fala de
  alguém dentro da cena) ou "sussurro" (segredo, tensão, medo);
- é ficção assumida: nunca escreva "caso real", "história real", "fatos reais"
  nem sugira que aconteceu de verdade;
- cada cena tem um prompt de imagem em inglês, descrevendo um quadro vertical
  9:16 coerente com a história: lugar, personagem, luz, clima. Sem texto na
  imagem, sem legenda, sem marca d'água, sem rosto de pessoa famosa.

Responda APENAS com JSON neste formato:
{"title": "título curto", "scenes": [
  {"image_prompt": "english prompt", "lines": [{"tone": "narrador", "text": "..."}]}
]}
"""


class ScriptError(ValueError):
    """Roteiro fora das regras: o build falha em vez de gerar vídeo errado."""


class ScriptLine(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tone: Tone
    text: str = Field(min_length=2, max_length=240)

    @model_validator(mode='after')
    def clean(self):
        self.text = self.text.strip()
        if not self.text:
            raise ValueError('Fala vazia')
        return self


class SceneScript(BaseModel):
    model_config = ConfigDict(extra='forbid')
    image_prompt: str = Field(min_length=10, max_length=700)
    lines: list[ScriptLine] = Field(min_length=1, max_length=2)


class Script(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=3, max_length=100)
    scenes: list[SceneScript] = Field(min_length=SCENE_COUNT, max_length=SCENE_COUNT)

    @property
    def all_lines(self) -> list[ScriptLine]:
        return [line for scene in self.scenes for line in scene.lines]

    @property
    def text(self) -> str:
        return ' '.join(line.text for line in self.all_lines)

    @property
    def word_count(self) -> int:
        return len(re.findall(r'\S+', self.text))


def reject_voice_cloning(payload: dict | None) -> None:
    """Recusa, com explicação, qualquer pedido de clonar voz de pessoa real."""
    if not isinstance(payload, dict):
        return
    for key in CLONE_FIELDS:
        value = payload.get(key)
        if value not in (None, '', False, [], {}):
            raise ScriptError(CLONE_REFUSAL)


def _words(text: str) -> int:
    return len(re.findall(r'\S+', text))


def parse_script(payload: object) -> Script:
    """Valida o JSON do modelo contra as regras do modo (falha alto e explicado)."""
    if not isinstance(payload, dict):
        raise ScriptError('O roteiro não veio como objeto JSON')
    try:
        script = Script.model_validate(payload)
    except ValueError as error:
        raise ScriptError(f'O roteiro veio incompleto ou fora do formato: {error}') from error
    if len(script.scenes) != SCENE_COUNT:
        raise ScriptError(f'O roteiro precisa de {SCENE_COUNT} cenas, veio com '
                          f'{len(script.scenes)}')
    lowered = script.text.lower()
    found = [term for term in FORBIDDEN_TERMS if term in lowered]
    if found:
        raise ScriptError(
            'O roteiro apresentou a ficção como caso real ("' + found[0] + '"); '
            'o modo história só produz ficção assumida')
    count = script.word_count
    if not MIN_WORDS <= count <= MAX_WORDS:
        raise ScriptError(f'O roteiro ficou com {count} palavras; o alvo é {MIN_WORDS}–'
                          f'{MAX_WORDS}')
    return script


Call = Callable[[str, dict], object]


def _default_call(prompt: str, data: dict) -> object:
    from backend.services.studio import intelligence
    if not intelligence.ready():
        raise ScriptError('Nenhum modelo de texto configurado. Ajuste em Configurações → IA '
                          'e tente de novo.')
    return intelligence.text_json(prompt, data)


def generate_script(theme: str, *, call: Call | None = None, attempts: int = 2) -> Script:
    """Uma chamada de LLM (com uma segunda chance só para o limite de palavras)."""
    theme = (theme or '').strip()
    if not theme:
        raise ScriptError('Escreva o tema da história antes de gerar')
    ask = call or _default_call
    last: ScriptError | None = None
    prompt = PROMPT
    for _ in range(max(1, attempts)):
        try:
            return parse_script(ask(prompt, {'tema': theme, 'idioma': 'pt-BR'}))
        except ScriptError as error:
            last = error
            # Só vale insistir quando o problema é contagem de palavras; formato
            # quebrado ou "caso real" indica que o modelo ignorou a instrução.
            if 'palavras' not in str(error):
                raise
            prompt = PROMPT + ('\nAtenção: a tentativa anterior não respeitou o limite de '
                               'palavras. Conte as palavras antes de responder.')
    raise last or ScriptError('Não consegui gerar o roteiro')
