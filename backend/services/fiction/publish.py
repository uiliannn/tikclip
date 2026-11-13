"""Publicação do modo história pelos publicadores locais que já existem.

Nada de API paga: o vídeo sai por `tiktok_local_publisher` e
`youtube_local_publisher`, os dois por automação de navegador com as sessões já
logadas no perfil do TikClip.

Duas travas do modo:
- a variável de ambiente `TIKCLIP_DISABLE_AUTO_PUBLISH` bloqueia este caminho
  (o lote manual do TikTok, que já existia, continua com o comportamento dele);
- cada plataforma é registrada no histórico do projeto assim que publica. O
  registro é o que marca o corte como publicado na Biblioteca e o que impede a
  republicação — o lote do modo história só considera plataformas ainda não
  registradas.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

PLATFORMS = ('tiktok', 'youtube')
LOCK = threading.RLock()
STATE: dict[str, Any] = {
    'running': False, 'stop': False, 'index': 0, 'total': 0, 'message': '',
    'log': [], 'project_id': None, 'platforms': [], 'published': [], 'failed': [],
}
_thread: threading.Thread | None = None


class PublishError(RuntimeError):
    """Publicação recusada ou falha; sempre com motivo legível."""


def env_locked() -> bool:
    """Trava dura por ambiente: com ela setada, nada é publicado por este caminho."""
    return os.getenv('TIKCLIP_DISABLE_AUTO_PUBLISH', '').strip().lower() in (
        '1', 'true', 'yes', 'on')


def _log(message: str) -> None:
    stamp = time.strftime('%H:%M:%S')
    with LOCK:
        STATE['message'] = message
        STATE['log'].append(f'{stamp}  {message}')
        if len(STATE['log']) > 300:
            del STATE['log'][:100]
    logger.info('[História] %s', message)


def state() -> dict:
    with LOCK:
        return {key: (list(value) if isinstance(value, list) else value)
                for key, value in STATE.items()}


def stop() -> dict:
    with LOCK:
        STATE['stop'] = True
    return {'stopping': True}


# --------------------------------------------------------------- registros ---
def published_platforms(project_id: str, variant_id: str) -> set[str]:
    """Plataformas em que esta variante já foi publicada com sucesso."""
    from backend.services.upload_post_publisher import list_records

    try:
        records = list_records(project_id)
    except (OSError, ValueError):
        logger.warning('Não consegui ler o histórico de publicação de %s', project_id)
        return set()
    found: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or record.get('status') != 'completed':
            continue
        if record.get('output_variant_id') != variant_id:
            continue
        for platform in record.get('platforms') or []:
            if isinstance(platform, str):
                found.add(platform)
    return found


def _record(project_id: str, clip: dict, job: dict | None, variant: dict | None,
            video: Path, platform: str, account: str | None, outcome: dict) -> Path:
    """Grava o histórico do projeto — é o que marca [PUBLICADO] na Biblioteca."""
    from backend.services.upload_post_publisher import LOCAL_USER, _write_record

    status = str(outcome.get('status') or 'completed')
    ok = status in ('completed', 'publicado')
    job_id = (job or {}).get('job_id')
    record = {
        'request_id': f'fiction-{uuid.uuid4()}',
        'job_id': None,
        'provider': f'{platform}_local',
        'project_id': project_id,
        'clip_id': f'studio-{job_id}' if job_id else None,
        'platforms': [platform],
        'user': LOCAL_USER,
        'title': clip.get('title') or '',
        'preset': platform,
        'path': str(video),
        'output_variant_id': clip.get('id'),
        'strategy_id': (variant or {}).get('strategy_id', 'tiktok'),
        'studio_job_id': job_id,
        'revision': 1,
        'scheduled_date': None,
        'submitted_at': datetime.now(timezone.utc).isoformat(),
        'http_status': 200,
        'status': 'completed' if ok else status,
        'account': account,
        'results': [{
            'platform': platform,
            'success': ok,
            'status': status,
            'url': outcome.get('url') or outcome.get('video_url'),
            'message': outcome.get('message') or f'Publicado em {platform}',
            'error': None if ok else outcome.get('message'),
            'skipped': False,
        }],
        'response': outcome,
    }
    return _write_record(project_id, record)


# ------------------------------------------------------------------- envio ---
def _publish_tiktok(video: Path, clip: dict, account: str | None,
                    progress: Callable[[str], None]) -> dict:
    from backend.services import tiktok_local_publisher as ttl
    return ttl.publish_video(video, clip['title'], clip.get('description', ''),
                             clip.get('tags') or [], None, account=account, progress=progress)


def _publish_youtube(video: Path, clip: dict, account: str | None,
                     progress: Callable[[str], None]) -> dict:
    import youtube_local_publisher as ytl
    return ytl.publish_video(video, clip['title'], clip.get('description', ''),
                             clip.get('tags') or '', None, visibility='public',
                             is_short=True, account=account, progress=progress)


_PUBLISHERS = {'tiktok': _publish_tiktok, 'youtube': _publish_youtube}


def _items(project_id: str, platforms: list[str]) -> list[dict]:
    """Itens com vídeo que ainda têm alguma plataforma pendente **nesta** lista.

    Um corte publicado em uma plataforma continua elegível para a outra, mas
    nunca volta a ser enviado para onde já foi — o registro é a trava.
    """
    import api_ext

    pending = []
    for clip in api_ext.project_clips(project_id):
        if not clip.get('has_video'):
            continue
        done = published_platforms(project_id, clip['id'])
        if any(platform not in done for platform in platforms):
            pending.append(clip)
    return pending


def _context(project_id: str, clip: dict) -> tuple[Path | None, dict | None, dict | None]:
    """(vídeo, job, variante) do corte, para publicar e registrar com o histórico certo."""
    import api_ext

    state_data = api_ext._read_studio(project_id)
    jobs = {row['job_id']: row for row in state_data.get('jobs', []) if row.get('job_id')}
    variant = next((row for row in state_data.get('output_variants', [])
                    if row.get('id') == clip['id']), None)
    job = jobs.get(variant.get('render_job_id')) if variant and variant.get('render_job_id') \
        else None
    video = api_ext._video_path(project_id, job) if job else None
    return video, job, variant


def _loop(project_id: str, platforms: list[str], tiktok_account: str | None,
          youtube_account: str | None, interval_sec: int) -> None:
    with LOCK:
        # `stop` só é zerado em `start`: aqui um pedido de parada já feito deve valer.
        STATE.update(running=True, index=0, total=0, project_id=project_id,
                     platforms=list(platforms), published=[], failed=[], log=[])
    published = 0
    failed = 0
    try:
        clips = _items(project_id, platforms)
        total = len(clips) * len(platforms)
        with LOCK:
            STATE['total'] = total
        position = 0
        for clip in clips:
            video, job, variant = _context(project_id, clip)
            if video is None:
                _log(f'"{clip["title"]}" não tem vídeo no disco; pulando')
                failed += 1
                continue
            done = published_platforms(project_id, clip['id'])
            for platform in platforms:
                position += 1
                with LOCK:
                    if STATE['stop']:
                        _log('Publicação interrompida pelo usuário.')
                        return
                    STATE['index'] = position
                if platform in done:
                    _log(f'[{position}] "{clip["title"]}" já está publicado em {platform}; pulando')
                    continue
                account = tiktok_account if platform == 'tiktok' else youtube_account
                publisher = _PUBLISHERS[platform]
                _log(f'[{position}] enviando "{clip["title"]}" para {platform}')
                try:
                    outcome = publisher(video, clip, account,
                                        lambda message, _p=position: _log(f'[{_p}] {message}'))
                    outcome = outcome if isinstance(outcome, dict) else {}
                    try:
                        record = _record(project_id, clip, job, variant, video, platform,
                                         account, outcome)
                        _log(f'[{position}] histórico gravado: {record.name}')
                    except (OSError, ValueError) as error:
                        # Sem histórico a variante continuaria "pronta" e poderia ser
                        # republicada: isso é falha, não aviso.
                        raise PublishError(f'não consegui registrar a publicação ({error})') \
                            from error
                    published += 1
                    with LOCK:
                        STATE['published'].append({'clip_id': clip['id'],
                                                   'title': clip['title'],
                                                   'platform': platform,
                                                   'url': outcome.get('url') or
                                                          outcome.get('video_url')})
                    _log(f'[{position}] publicado em {platform} ✔')
                except Exception as error:  # noqa: BLE001 - uma falha não derruba o lote
                    failed += 1
                    with LOCK:
                        STATE['failed'].append({'clip_id': clip['id'], 'platform': platform,
                                                'error': str(error)[:400]})
                    _log(f'[{position}] falhou em {platform}: {error}')
                if interval_sec and position < total:
                    _log(f'aguardando {interval_sec}s antes do próximo envio')
                    end = time.time() + interval_sec
                    while time.time() < end:
                        with LOCK:
                            if STATE['stop']:
                                _log('Publicação interrompida pelo usuário.')
                                return
                        time.sleep(1)
    except Exception as error:  # noqa: BLE001 - a fila nunca fica travada em "publicando"
        logger.exception('Falha inesperada na publicação da história %s', project_id)
        _log(f'Erro inesperado na publicação: {error}')
    finally:
        with LOCK:
            STATE['running'] = False
            STATE['stop'] = False
        _log(f'Concluído: {published} publicado(s), {failed} falha(s).')


def start(project_id: str, *, platforms: list[str] | None = None,
          tiktok_account: str | None = None, youtube_account: str | None = None,
          interval_sec: int = 30) -> dict:
    """Valida as travas e dispara a publicação em segundo plano."""
    global _thread
    if env_locked():
        raise PublishError(
            'A publicação está travada por ambiente (TIKCLIP_DISABLE_AUTO_PUBLISH). '
            'Remova a variável e suba o servidor de novo para publicar.')
    chosen = [platform for platform in (platforms or ['tiktok']) if platform in PLATFORMS]
    if not chosen:
        raise PublishError('Escolha ao menos uma plataforma: tiktok e/ou youtube')
    if not _items(project_id, chosen):
        raise PublishError('Nada pendente para publicar: este projeto não tem corte com vídeo '
                           'ou todas as plataformas escolhidas já foram publicadas')
    with LOCK:
        if STATE['running'] and _thread is not None and _thread.is_alive():
            raise PublishError('Já existe uma publicação da história em andamento')
        # `running` já vale aqui: a tela nunca vê "parado" enquanto a fila existe.
        STATE.update(running=True, stop=False, index=0, total=0, project_id=project_id,
                     platforms=list(chosen), published=[], failed=[], log=[],
                     message='Preparando a publicação')
        _thread = threading.Thread(
            target=_loop,
            args=(project_id, chosen, tiktok_account, youtube_account, max(0, int(interval_sec))),
            daemon=True, name='fiction-publish')
        _thread.start()
    return state()
