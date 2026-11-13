"""Peças reutilizadas pelos testes do modo história.

Nada aqui toca rede: roteiro, imagens, áudio e eventos de palavra são gerados
localmente. O único processo externo é o ffmpeg, que já é dependência do app.
"""
from __future__ import annotations

import copy
import io
import math
import struct
import wave
from pathlib import Path

# 83 palavras, 6 cenas, 10 falas, os três tons — dentro da regra 80–100.
SCRIPT_PAYLOAD = {
    'title': 'O carro na estrada',
    'scenes': [
        {'image_prompt': 'Rain-soaked night road, broken car headlights, cinematic',
         'lines': [{'tone': 'narrador',
                    'text': 'Chovia na estrada quando o farol do carro apagou de vez.'},
                   {'tone': 'narrador', 'text': 'Ninguém respondeu.'}]},
        {'image_prompt': 'Woman looking at the rear-view mirror, dashboard glow',
         'lines': [{'tone': 'personagem',
                    'text': 'Não desligue o motor, disse ela, olhando o retrovisor.'}]},
        {'image_prompt': 'Empty back seat in the dark, faint silhouette',
         'lines': [{'tone': 'sussurro',
                    'text': 'Há alguém sentado no banco de trás desde a última parada.'},
                   {'tone': 'sussurro', 'text': 'Ela não quis virar a cabeça.'}]},
        {'image_prompt': 'Man counting heads on the mirror, uneasy face',
         'lines': [{'tone': 'narrador',
                    'text': 'Ele contou as cabeças no espelho e encontrou uma a mais.'},
                   {'tone': 'narrador', 'text': 'O rádio ligou sozinho.'}]},
        {'image_prompt': 'Hand on the car door handle at night, rain outside',
         'lines': [{'tone': 'personagem',
                    'text': 'Abre a porta devagar, sem olhar para trás, e corre.'}]},
        {'image_prompt': 'Empty car rolling alone down a street, lights on',
         'lines': [{'tone': 'narrador',
                    'text': 'O carro seguiu sozinho, com as luzes acesas, até o fim da rua.'},
                   {'tone': 'narrador', 'text': 'Ela nunca mais voltou àquela estrada.'}]},
    ],
}


def script_payload() -> dict:
    return copy.deepcopy(SCRIPT_PAYLOAD)


def script_payload_with(text: str) -> dict:
    """Roteiro de uma cena só, para exercitar recusas pontuais."""
    return {'title': 'Teste', 'scenes': [
        {'image_prompt': 'A dark room with a single lamp',
         'lines': [{'tone': 'narrador', 'text': text}]} for _ in range(6)]}


def fake_call(payload: dict):
    """Dublê de `intelligence.text_json`."""
    def call(prompt: str, data: dict) -> dict:
        return copy.deepcopy(payload)
    return call


def image_bytes(colour=(24, 30, 46), size=(1080, 1920)) -> bytes:
    """JPEG vertical válido, sem depender de rede."""
    from PIL import Image, ImageDraw
    image = Image.new('RGB', size, colour)
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, size[0] - 40, size[1] - 40), outline=(240, 240, 240), width=8)
    draw.ellipse((size[0] // 3, size[1] // 3, size[0] * 2 // 3, size[1] * 2 // 3),
                 fill=(90, 139, 255))
    buffer = io.BytesIO()
    image.save(buffer, format='JPEG', quality=85)
    return buffer.getvalue()


def fake_generate(seen: list | None = None):
    """Dublê de `image_providers.generate_image` com uma cor diferente por cena."""
    calls: list = seen if seen is not None else []

    def generate(*, provider, api_key, base_url, request):
        index = len(calls)
        calls.append(request.prompt)
        hue = (index * 37) % 255
        return image_bytes(colour=(hue, (hue * 2) % 255, (hue * 3) % 255))

    generate.calls = calls
    return generate


def write_image(path: Path, colour=(24, 30, 46)) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image_bytes(colour))
    return path


def wav_bytes(seconds: float, rate: int = 8000) -> bytes:
    """WAV PCM de silêncio com duração exata (o ffmpeg lê a duração real)."""
    frames = max(1, int(round(seconds * rate)))
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b'\x00\x00' * frames)
    return buffer.getvalue()


def write_wav(path: Path, seconds: float, rate: int = 8000) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(wav_bytes(seconds, rate))
    return path


def word_events(text: str, seconds: float) -> list[dict]:
    """Eventos `WordBoundary` no formato do edge-tts (ticks de 100 ns)."""
    words = text.split()
    if not words:
        return []
    step = seconds / len(words)
    events = []
    for index, word in enumerate(words):
        events.append({'type': 'WordBoundary',
                       'offset': int(index * step * 10_000_000),
                       'duration': int(step * 9_000_000),
                       'text': word})
    return events


def fake_synth(seconds: float = 2.6):
    """Dublê do edge-tts: devolve WAV de silêncio + eventos de palavra."""
    def synth(text: str, voice: str, prosody: dict) -> tuple[bytes, list[dict]]:
        # As palavras ocupam 90% do áudio, como numa fala real.
        return wav_bytes(seconds), word_events(text, seconds * 0.9)

    synth.seconds = seconds
    return synth


def silent_synth(seconds: float = 2.6):
    """Dublê que não devolve eventos: força o caminho `estimated`."""
    def synth(text: str, voice: str, prosody: dict) -> tuple[bytes, list[dict]]:
        return wav_bytes(seconds), []

    return synth


def sine_wav(path: Path, seconds: float, rate: int = 8000) -> Path:
    """Sinal com conteúdo (não silêncio), útil para conferir o mux de áudio."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(round(seconds * rate))
    data = bytearray()
    for index in range(frames):
        value = int(6000 * math.sin(2 * math.pi * 220 * index / rate))
        data += struct.pack('<h', value)
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(data))
    return path
