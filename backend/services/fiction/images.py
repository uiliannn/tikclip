"""As seis imagens do modo história, geradas pelo provedor já configurado.

Reaproveita o mesmo serviço de imagem das capas (`backend/core/image_providers.py`
via `services/cover.py`), então não existe configuração nova de provedor: o que
estiver valendo em Ajustes → capa com IA é o que gera as cenas.

Nenhuma imagem de terceiro é reaproveitada: cada cena é gerada do seu próprio
prompt e o build falha se o provedor devolver a mesma imagem duas vezes.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any, Callable

# Instrução de casa somada ao prompt do roteiro: é o que garante o formato certo
# e evita texto queimado na imagem (a legenda é nossa, na timeline).
HOUSE_STYLE = (
    'Vertical 9:16 cinematic still for a short fiction video. '
    'Photographic, shallow depth of field, dramatic but natural lighting, film grain. '
    'One clear subject, readable at phone size, no text, no letters, no captions, '
    'no watermark, no logo, no user interface, no collage, no split screen. '
    'Original fictional scene; the people are anonymous and not based on any real person.'
)

# `generate` recebe os mesmos argumentos nomeados de image_providers.generate_image.
Generate = Callable[..., bytes]

TARGET = (1080, 1920)


class ImageError(RuntimeError):
    """Falha de imagem: sem cena, o build para em vez de gravar vídeo com buraco."""


def scene_prompt(base: str, index: int, total: int, palette: str = 'azure') -> str:
    """Prompt final de uma cena: o do roteiro + enquadramento + continuidade."""
    base = (base or '').strip()
    return (f'{base} {HOUSE_STYLE} '
            f'Shot {index + 1} of {total} in the same story, keeping the same characters, '
            f'wardrobe and colour mood. Colour accent: {palette}.')


def settings() -> dict[str, Any]:
    """Configuração efetiva de imagem (a mesma das capas), em campos públicos."""
    from backend.services import cover
    config = cover.load_config()
    return {
        'enabled': bool(config.enabled),
        'configured': bool(config.configured),
        'provider': config.provider,
        'model': config.model,
        'base_url': config.base_url,
        'api_key': config.api_key,
    }


def _require_settings(config: dict[str, Any] | None) -> dict[str, Any]:
    config = config or settings()
    if not (config.get('enabled') and config.get('configured')):
        raise ImageError(
            'Nenhum provedor de imagem configurado. Abra Ajustes → capa com IA, escolha o '
            'serviço de imagem e a chave, e gere a história de novo. Sem provedor eu não '
            'invento as cenas.')
    return config


def _normalise(raw: bytes, size: tuple[int, int] = TARGET):
    """Abre a imagem do provedor e encaixa em 1080x1920 sem cortar o assunto."""
    from PIL import Image

    from backend.services.studio.publish_kit import _fit
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as error:  # noqa: BLE001 - bytes do provedor não são confiáveis
        raise ImageError('O provedor de imagem devolveu um arquivo que não é imagem') from error
    return _fit(image.convert('RGB'), size)


def generate_scene_images(
    out_dir: Path,
    prompts: list[str],
    *,
    rel_prefix: str = 'images',
    config: dict[str, Any] | None = None,
    generate: Generate | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[str]:
    """Gera todas as cenas e devolve os caminhos relativos ao timeline."""
    config = _require_settings(config)
    if not prompts:
        raise ImageError('Não há prompts de cena para gerar')
    run = generate
    if run is None:
        from backend.core.image_providers import generate_image as run  # noqa: PLC0415
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    names: list[str] = []
    seen: dict[str, int] = {}
    from backend.core.image_providers import ImageRequest

    for index, prompt in enumerate(prompts):
        try:
            raw = run(provider=config['provider'], api_key=config['api_key'] or '',
                      base_url=config['base_url'] or '',
                      request=ImageRequest(prompt=prompt, width=TARGET[0], height=TARGET[1],
                                           model=config['model'] or ''))
        except ImageError:
            raise
        except Exception as error:  # noqa: BLE001 - erro do provedor, com contexto da cena
            raise ImageError(f'Falha ao gerar a imagem da cena {index + 1}: {error}') from error
        if not raw:
            raise ImageError(f'O provedor não devolveu imagem para a cena {index + 1}')
        image = _normalise(raw)
        buffer = io.BytesIO()
        image.save(buffer, format='JPEG', quality=92)
        payload = buffer.getvalue()
        digest = hashlib.sha256(payload).hexdigest()
        if digest in seen:
            # Provedor que ecoa a mesma imagem deixaria o vídeo com 6 cópias da cena 1.
            raise ImageError(
                f'A cena {index + 1} saiu idêntica à cena {seen[digest] + 1}; '
                'o provedor não gerou imagens distintas')
        seen[digest] = index
        name = f'scene-{index}.jpg'
        (out_dir / name).write_bytes(payload)
        names.append(f'{rel_prefix}/{name}' if rel_prefix else name)
        if progress:
            progress(index + 1, len(prompts))
    return names
