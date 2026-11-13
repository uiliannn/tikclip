"""Orquestração do modo história dentro de um projeto do TikClip.

O build segue a ordem do pipeline e grava o estado **antes** de começar e
**depois** de cada etapa relevante. Se qualquer etapa falhar, o job e a variante
ficam `failed` com o motivo — nunca `completed` sem vídeo — e o arquivo parcial é
apagado. É o mesmo contrato de `processing_orchestrator` e de `studio/jobs.py`.
"""
from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from backend.core.path_utils import get_projects_directory
from backend.services.fiction import builder, images, player, render, script as script_mod, timeline as timeline_mod, voices

logger = logging.getLogger(__name__)

BUILD_LOCK = threading.Lock()
_running: dict[str, threading.Thread] = {}

STEPS = (
    ('roteiro', 5),
    ('imagens', 10),
    ('vozes', 45),
    ('timeline', 70),
    ('audio', 74),
    ('render', 78),
)


class BuildError(RuntimeError):
    """Falha do build: o projeto termina em erro, não em vídeo falso."""


# --------------------------------------------------------------- caminhos ---
def project_dir(project_id: str) -> Path:
    return get_projects_directory() / project_id


def fiction_dir(project_id: str) -> Path:
    return project_dir(project_id) / 'metadata' / 'fiction'


def new_project_id() -> str:
    return uuid.uuid4().hex


# ----------------------------------------------------------------- estado ---
def _fiction_block(data: dict) -> dict:
    return data.get('fiction') if isinstance(data.get('fiction'), dict) else {}


def _write_state(project_id: str, **fields: Any) -> dict:
    """Atualiza só o bloco `fiction` do studio.json (atômico, via store)."""
    from backend.services.studio import store

    def mutate(data: dict) -> dict:
        block = dict(_fiction_block(data))
        block.update(fields)
        block['updated_at'] = store.now()
        data['fiction'] = block
        return block

    return store.change(project_id, mutate)


def state(project_id: str) -> dict:
    """Estado do modo história neste projeto, para a tela e para os testes.

    Lê o bloco `fiction` do studio.json e confere com o que existe em disco: um
    bloco dizendo "pronto" sem MP4 não é reportado como pronto.
    """
    from backend.services.studio import store

    project = project_dir(project_id)
    try:
        data = store.read(project_id)
    except (OSError, ValueError):
        logger.warning('Não consegui ler o estado do projeto %s', project_id, exc_info=True)
        data = {}
    block = _fiction_block(data)
    jobs = {row['job_id']: row for row in data.get('jobs', []) if row.get('job_id')}
    job = jobs.get(block.get('job_id'), {})
    video = None
    if block.get('job_id'):
        candidate = project / 'output' / 'studio' / f"{block['job_id']}.mp4"
        video = candidate if candidate.is_file() else None
    duration = block.get('duration')
    if duration is None and isinstance(job.get('result'), dict):
        duration = job['result'].get('duration')
    status = block.get('status') or ('ready' if video else 'idle')
    if status == 'ready' and video is None:
        # Bloco antigo/inconsistente: o fato manda, não o rótulo gravado.
        status = 'failed'
        if not block.get('error'):
            block['error'] = 'O vídeo desta história não está mais no disco'
    return {
        'project_id': project_id,
        'status': status,
        'percent': int(block.get('percent') or (100 if status == 'ready' else 0)),
        'step': block.get('step') or '',
        'message': block.get('message') or '',
        'error': block.get('error'),
        'title': block.get('title') or job.get('title') or '',
        'duration': duration,
        'scenes': block.get('scenes'),
        'cues': block.get('cues'),
        'tones': block.get('tones') or [],
        'timing': block.get('timing') or [],
        'platforms': block.get('platforms') or [],
        'word_count': block.get('word_count'),
        'job_id': block.get('job_id'),
        'variant_id': block.get('variant_id'),
        'has_video': video is not None,
        'video_url': (f'/api/v1/ext/projects/{project_id}/clips/{block["variant_id"]}/video'
                      if video is not None and block.get('variant_id') else None),
        'player_url': f'/api/v1/ext/fiction/player/{project_id}' if video is not None else None,
        'updated_at': block.get('updated_at'),
    }


# ------------------------------------------------------------------ build ---
def build(
    project_id: str,
    *,
    theme: str,
    platforms: list[str] | None = None,
    palette: str = 'azure',
    generate_image: Callable[..., bytes] | None = None,
    image_config: dict | None = None,
    synth: Callable[..., Any] | None = None,
    script_call: Callable[[str, dict], object] | None = None,
    render_fn: Callable[..., dict] | None = None,
    capture: Callable[..., None] | None = None,
    progress: Callable[[int, str], None] | None = None,
) -> dict:
    """Executa o pipeline inteiro, com dependências injetáveis para teste.

    Cada parâmetro externo (modelo, provedor de imagem, TTS, render) pode ser
    trocado por um dublê; em produção todos caem no caminho real.
    """
    project = project_dir(project_id)
    project.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex
    variant_id = uuid.uuid4().hex
    draft_id = uuid.uuid4().hex
    job: dict[str, Any] = {
        'job_id': job_id, 'status': 'queued', 'percent': 0, 'draft_id': draft_id,
        'title': (theme or 'História')[:120], 'revision': 1,
    }
    variant: dict[str, Any] = {
        'id': variant_id, 'draft_id': draft_id, 'draft_revision': 1,
        'strategy_id': 'tiktok', 'strategy_version': 1,
        'branding': {'outro_enabled': False, 'outro_version': 'v1'},
        'status': 'preparing',
    }
    draft: dict[str, Any] = {
        'id': draft_id, 'title': (theme or 'História')[:120], 'hook': '', 'language': 'source',
        'aspect': 'portrait', 'layout': 'fit', 'subtitles': True, 'revision': 1,
        'origin': 'fiction', 'scenes': [{'id': f's{index + 1}', 'label': f'Cena {index + 1}',
                                         'start': 0.0, 'end': 1.0}
                                        for index in range(timeline_mod.SCENE_COUNT)],
    }

    def report(percent: int, message: str) -> None:
        _write_state(project_id, status='building', percent=percent, message=message,
                     step=message, job_id=job_id, variant_id=variant_id, error=None)
        if progress:
            progress(percent, message)

    def register(data: dict) -> None:
        from backend.services.studio import store
        data.setdefault('drafts', []).append({**draft, 'updated_at': store.now()})
        data.setdefault('jobs', []).insert(0, {**job, 'created_at': store.now(),
                                               'instance': store.INSTANCE})
        data.setdefault('output_variants', []).append({**variant, 'created_at': store.now()})

    from backend.services.studio import store

    store.change(project_id, register)

    timeline = None
    try:
        report(1, 'Preparando a história')
        report(STEPS[0][1], 'Escrevendo o roteiro')
        story = script_mod.generate_script(theme, call=script_call)
        report(STEPS[1][1], 'Gerando as seis imagens')
        image_paths = images.generate_scene_images(            project / 'images',
            [images.scene_prompt(scene.image_prompt, index, len(story.scenes), palette)
             for index, scene in enumerate(story.scenes)],
            config=image_config, generate=generate_image,
            progress=lambda done, total: report(
                STEPS[1][1] + round(35 * done / max(1, total)),
                f'Gerando imagem {done} de {total}'))

        report(STEPS[2][1], 'Sintetizando as vozes')
        sounds: list[builder.SpokenLine] = []
        total_lines = sum(len(scene.lines) for scene in story.scenes)
        done_lines = 0
        for scene_index, scene in enumerate(story.scenes):
            for line_index, line in enumerate(scene.lines):
                name = f's{scene_index + 1}-l{line_index + 1}'
                audio_file = project / 'audio' / f'{name}.mp3'
                _, timing = voices.synthesize(line.text, line.tone, audio_file, synth=synth)
                duration = voices.duration_seconds(audio_file)
                words = tuple((word.text, word.start, word.end)
                              for word in voices.words_of(audio_file))
                from backend.services import subtitle_timing
                sidecar = subtitle_timing.sidecar_path(audio_file)
                sounds.append(builder.SpokenLine(
                    audio=f'audio/{audio_file.name}', duration=duration, words=words,
                    timing=timing,
                    words_path=f'audio/{sidecar.name}' if sidecar.is_file() else None))
                done_lines += 1
                report(STEPS[2][1] + round(20 * done_lines / max(1, total_lines)),
                       f'Voz {done_lines} de {total_lines}')

        report(STEPS[3][1], 'Montando a timeline')
        timeline = builder.build_timeline(
            story, image_paths, sounds,
            builder.BuildInput(project_id=project_id, title=story.title,
                               description=story.text[:2000], tags=[], palette=palette))
        timeline_mod.validate_broadcast_ready(timeline)
        timeline_mod.save(project, timeline)

        report(STEPS[4][1], 'Montando a trilha de áudio')
        voices.mix_track(timeline, project, project / timeline.audio)

        report(STEPS[5][1], 'Renderizando o vídeo')
        output = project / 'output' / 'studio' / f'{job_id}.mp4'

        def link_render(data: dict) -> None:
            # Liga a variante ao job antes de renderizar: durante o encode a
            # Biblioteca mostra "renderizando", e uma queda no meio deixa o
            # par amarrado em vez de uma variante órfã.
            found_job = next((row for row in data['jobs'] if row['job_id'] == job_id), None)
            if found_job is not None:
                found_job.update(status='running', percent=STEPS[5][1])
            found_variant = next((row for row in data['output_variants']
                                  if row['id'] == variant_id), None)
            if found_variant is not None:
                found_variant.update(status='running', render_job_id=job_id)

        store.change(project_id, link_render)
        renderer = render_fn or render.render
        result = renderer(timeline, project, output, capture=capture,
                          progress=lambda percent: report(
                              STEPS[5][1] + round(20 * percent / 100),
                              'Renderizando o vídeo'))

        def complete(data: dict) -> None:
            found_job = next((row for row in data['jobs'] if row['job_id'] == job_id), None)
            if found_job is not None:
                found_job.update(status='completed', percent=100, result=result)
            found_variant = next((row for row in data['output_variants']
                                  if row['id'] == variant_id), None)
            if found_variant is not None:
                found_variant.update(status='completed',
                                     post={'title': story.title[:100],
                                           'description': story.text[:2000], 'tags': []})

        store.change(project_id, complete)
        _write_state(project_id, status='ready', percent=100, message='Vídeo pronto',
                     error=None, title=story.title, duration=timeline.duration,
                     scenes=len(timeline.scenes), cues=len(timeline.cues),
                     tones=timeline.meta.get('tones', []), timing=timeline.meta.get('timing', []),
                     platforms=list(platforms or []), word_count=story.word_count)
        logger.info('História renderizada: projeto %s, job %s, %.1fs', project_id, job_id,
                    timeline.duration)
        return state(project_id)
    except Exception as error:  # noqa: BLE001 - grava o motivo e propaga
        message = str(error)[:700] or type(error).__name__
        logger.exception('Falha ao gerar a história do projeto %s', project_id)
        partial = project / 'output' / 'studio' / f'{job_id}.mp4'
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            logger.warning('Não consegui apagar o vídeo parcial de %s', project_id)
        try:
            def fail(data: dict) -> None:
                found_job = next((row for row in data['jobs'] if row['job_id'] == job_id), None)
                if found_job is not None:
                    found_job.update(status='failed', percent=100, error=message)
                found_variant = next((row for row in data['output_variants']
                                      if row['id'] == variant_id), None)
                if found_variant is not None:
                    found_variant.update(status='failed', error=message)

            store.change(project_id, fail)
            _write_state(project_id, status='failed', error=message,
                         message='A história falhou', job_id=job_id, variant_id=variant_id)
        except (OSError, FileNotFoundError):
            logger.warning('Não consegui registrar a falha do projeto %s', project_id)
        raise BuildError(message) from error


# ------------------------------------------------------------- em segundo ---
def start(project_id: str, **kwargs: Any) -> dict:
    """Dispara o build numa thread; o estado fica consultável por `state`."""
    with BUILD_LOCK:
        running = _running.get(project_id)
        if running is not None and running.is_alive():
            raise BuildError('Já existe uma história sendo gerada neste projeto')
        thread = threading.Thread(target=_run_quietly, args=(project_id,),
                                  kwargs=kwargs, daemon=True, name=f'fiction-{project_id[:8]}')
        _running[project_id] = thread
        thread.start()
    return state(project_id)


def _run_quietly(project_id: str, **kwargs: Any) -> None:
    try:
        build(project_id, **kwargs)
    except Exception:  # noqa: BLE001 - o erro já foi gravado no estado e no log
        logger.debug('Build da história terminou em falha para %s', project_id, exc_info=True)
    finally:
        with BUILD_LOCK:
            _running.pop(project_id, None)


def is_running(project_id: str) -> bool:
    with BUILD_LOCK:
        thread = _running.get(project_id)
        return bool(thread and thread.is_alive())


def list_projects() -> list[dict]:
    """Projetos que têm uma história (para a lista da tela)."""
    root = get_projects_directory()
    if not root.is_dir():
        return []
    items = []
    for entry in sorted(root.iterdir(), key=lambda path: path.stat().st_mtime, reverse=True):
        if not entry.is_dir() or not timeline_mod.timeline_path(entry).is_file():
            continue
        try:
            item = state(entry.name)
        except (OSError, ValueError):
            logger.warning('História ilegível no projeto %s', entry.name)
            continue
        item['updated_at'] = entry.stat().st_mtime
        items.append(item)
    return items


def video_path(project_id: str) -> Path | None:
    """MP4 final da história deste projeto, quando existir."""
    try:
        item = state(project_id)
    except (OSError, ValueError):
        return None
    job_id = item.get('job_id')
    if not job_id:
        return None
    candidate = project_dir(project_id) / 'output' / 'studio' / f'{job_id}.mp4'
    return candidate if candidate.is_file() else None


def player_file(project_id: str) -> str:
    """HTML do player (para conferir no navegador com `?preview`)."""
    timeline = timeline_mod.load(project_dir(project_id))
    return player.render_html(timeline, project_dir(project_id))
