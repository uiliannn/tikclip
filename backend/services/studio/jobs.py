import json
import logging
import os
import re
import shutil
from time import monotonic, sleep
from backend.core.sentry_setup import capture_studio_exception, studio_error_code, studio_failure_context
from copy import deepcopy
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from backend.core import llm_usage
from backend.services.studio import audio, intelligence, store
from backend.services.studio.features import resolve_features
from backend.services.studio.models import Draft, Preferences, Scene
from backend.services.studio.project_completion import sync_project_completion
from backend.services.studio.intelligence import analyze, make_drafts, VisionRequestError
from backend.services.studio.render import render_draft

logger = logging.getLogger(__name__)
executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='studio')


def _generation_route(plan):
    route = (plan or {}).get('recommended_analysis') or (plan or {}).get('confirmed_analysis')
    return {'route': route} if route in ('subtitle', 'visual') else {}
# Local encodes are CPU-bound: run one at a time on its own worker so screening and
# analysis of other imports never wait behind a render queue.
render_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='studio-render')


def source(project_id):
    from backend.services.publish_export import find_source_video
    return find_source_video(project_id)

def export(project_id, draft, *, brand_outro=False, _variant_identity=None):
    from backend.services.output_branding import load_settings
    brand_outro = bool(brand_outro and load_settings().enabled)
    job = {'job_id': uuid.uuid4().hex, 'status': 'queued', 'percent': 0, 'draft_id': draft.id, 'title': draft.title, 'revision': draft.revision, 'brand_outro': brand_outro, 'created_at': store.now(), 'instance': store.INSTANCE, 'snapshot': draft.model_dump()}
    if _variant_identity is not None:
        job['variant_links'] = [list(_variant_identity)]
    def add(data):
        active = next((j for j in data['jobs'] if j['status'] in ('queued', 'running') and j.get('instance') == store.INSTANCE
                       and j.get('snapshot') == job['snapshot']
                       and j.get('brand_outro', False) == brand_outro), None)
        if active:
            if _variant_identity is not None and list(_variant_identity) not in active.get('variant_links', []):
                active.setdefault('variant_links', []).append(list(_variant_identity))
            return active
        data['jobs'].insert(0, job)
        return job
    # Keep deduplication and dispatch atomic: another request must not receive
    # a queued job before we know the executor accepted it.
    with store.lock:
        added = store.change(project_id, add)
        if added['job_id'] == job['job_id']:
            try:
                if brand_outro:
                    render_executor.submit(_render, project_id, draft, job['job_id'], brand_outro=True, _io_receipt=('render', job['job_id']))
                else:
                    render_executor.submit(_render, project_id, draft, job['job_id'], _io_receipt=('render', job['job_id']))
            except Exception as error:
                logger.warning('Studio export dispatch failed: %s', type(error).__name__)
                capture_studio_exception(error, 'dispatch')
                store.mark_worker_error_reported()
                store.remember_submission_rejection(project_id, ('render', job['job_id']), error_code=studio_error_code(error))
                message = 'A exportação não conseguiu iniciar; tente de novo. As peças já geradas foram mantidas.'
                code = studio_error_code(error)
                def failed(data):
                    next(j for j in data['jobs'] if j['job_id'] == job['job_id']).update(status='failed', error=message, error_code=code)
                store.change(project_id, failed)
                raise ValueError(message) from None
        return {k: v for k, v in added.items() if k not in ('instance', 'snapshot', 'variant_links')}

def _tracked(stage):
    """Record model token usage of a studio job (first argument: project id) under `stage`."""
    def wrap(fn):
        from functools import wraps

        @wraps(fn)
        def run(project_id, *args, **kwargs):
            identity = kwargs.pop('_io_receipt', None)
            from backend.core.project_cancellation import ProjectDeleted, bind
            with bind(project_id), store.worker_read_scope(project_id, identity), llm_usage.tracking(project_id), llm_usage.stage(stage):
                try:
                    return fn(project_id, *args, **kwargs)
                except ProjectDeleted:
                    return None  # deleted: the binder removes leftover files; nothing to record
                except PermissionError as error:
                    if store.read_error_needs_report(error):
                        phase = {'visual_analysis': 'analysis', 'ai_cover': 'production'}.get(stage, stage)
                        capture_studio_exception(error, phase)
                    raise
        return run
    return wrap


@_tracked('render')
def _render(project_id, draft, job_id, *, brand_outro=False):
    started = monotonic()
    render_saved = False
    current = next((j for j in store.read(project_id, recover=False).get('jobs', []) if j.get('job_id') == job_id), None)
    if current and current.get('status') in ('completed', 'cancelled'):
        return
    def update(**values):
        def mutate(data):
            next(j for j in data['jobs'] if j['job_id'] == job_id).update(values)
        store.change(project_id, mutate)
    try:
        update(status='running', percent=5)
        with llm_usage.timed('render'):
            result = render_draft(project_id, source(project_id), draft, job_id, lambda p: update(percent=p), brand_outro=brand_outro)
        update(status='completed', percent=100, result=result, cover_pending=True, duration_ms=round((monotonic() - started) * 1000))
        render_saved = True
        try:
            _design_covers(project_id, draft, job_id)
        finally:
            update(cover_pending=False)
        _sync_variant_status(project_id, job_id, 'completed')
    except Exception as error:
        logger.warning('Studio render failed: %s', type(error).__name__)
        capture_studio_exception(error, 'render')
        store.mark_worker_error_reported()
        if render_saved:
            # Optional cover/index work cannot revoke an already committed video.
            _sync_variant_status(project_id, job_id, 'completed')
            return
        try:
            message = str(error)[:700]
            update(status='failed', error=message, error_code=studio_error_code(error), duration_ms=round((monotonic() - started) * 1000))
            _sync_variant_status(project_id, job_id, 'failed', message)
        except FileNotFoundError:
            pass
    finally:
        if render_saved:
            store.mark_render_finished(project_id, job_id)

def analyze_project(project_id, prefs, url=None, browser=None):
    def begin(data):
        if (data.get('analysis') or {}).get('status') == 'running':
            raise ValueError('Análise visual em andamento')
        data['analysis'] = {'status': 'running', 'phase': 'analysis', 'run_id': uuid.uuid4().hex, 'message': 'Baixando o material' if url else 'Entendendo as imagens', 'instance': store.INSTANCE, 'created_at': store.now()}
        return data['analysis']['run_id']
    run_id = store.change(project_id, begin)
    try:
        executor.submit(_analyze, project_id, prefs, url, browser, _io_receipt=('analysis', run_id, 'analysis'))
    except Exception as error:
        store.remember_submission_rejection(project_id, ('analysis', run_id, 'analysis'), error_code=studio_error_code(error))
        capture_studio_exception(error, 'dispatch')
        message = 'A análise não conseguiu iniciar; tente de novo. O material original e as peças já geradas foram mantidos.'
        failure = {'status': 'failed', 'error': message, 'error_code': studio_error_code(error)}
        store.change(project_id, lambda data: data.update(analysis=failure))
        raise ValueError(message) from None

def mark_project(project_id, status, **config):
    from backend.core.database import SessionLocal
    from backend.models.project import Project, ProjectStatus
    with SessionLocal() as db:
        p = db.query(Project).filter(Project.id == project_id).first()
        if p:
            p.status = ProjectStatus(status)
            p.processing_config = {**(p.processing_config or {}), **config}
            if status == 'completed':
                from datetime import datetime, timezone
                p.completed_at = datetime.now(timezone.utc)
            db.commit()

@_tracked('visual_analysis')
def _analyze(project_id, prefs, url, browser):
    try:
        mark_project(project_id, 'processing')
        if url:
            with llm_usage.timed('download'):
                download(project_id, url, browser)
        video = source(project_id)
        def stage(message):
            store.change(project_id, lambda data: data['analysis'].update(message=message))
        instruction = getattr(prefs, 'instruction', '')
        from backend.services.studio import intelligence
        if not intelligence.ready():
            raise ValueError('Este recurso requer modelo de IA visual configurado nas configuracoes.')
        events, coverage = analyze(video, prefs, stage, instruction) if instruction else analyze(video, prefs, stage)
        stage('Organizando ganchos e cortes' if prefs.goal == 'promo' else 'Organizando melhores momentos')
        drafts = make_drafts(events, prefs, instruction, source_duration=intelligence._probe(video).get('duration'))
        if (video.parent / 'input.srt').exists():
            for draft in drafts:
                draft['subtitles'] = True
        def complete(data):
            data['events'] = [e.model_dump() for e in events]
            data['drafts'].extend({**d, 'updated_at': store.now()} for d in drafts)
            data['analysis'] = {'status': 'completed', 'coverage': coverage, 'created_at': store.now()}
        store.change(project_id, complete)
        mark_project(project_id, 'completed', studio_draft_count=len(store.read(project_id)['drafts']))
    except Exception as error:
        store.raise_owned_read_error(error)
        logger.warning('Studio analysis failed: %s', type(error).__name__)
        capture_studio_exception(error, 'analysis')
        message = str(error)[:700]
        diagnostics = [error.diagnostics()] if isinstance(error, VisionRequestError) else None
        def failed(data):
            data['analysis'] = {'status': 'failed', 'error': message}
            if diagnostics:
                data['analysis']['diagnostics'] = diagnostics
        try:
            store.change(project_id, failed)
            mark_project(project_id, 'completed' if store.read(project_id)['drafts'] else 'failed')
        except FileNotFoundError:
            pass

def download_progress_hook(project_id, min_interval=1.0):
    """把 yt-dlp 的下载百分比写进 analysis.percent。

    以前整段下载只显示「Preparando o material」，几分钟不动，用户以为卡死。音视频分两个文件下载时
    百分比会回落，只写单调递增的值；最多每秒写一次。
    """
    state = {'at': 0.0, 'percent': -1}
    def hook(d):
        if d.get('status') != 'downloading':
            return
        total = d.get('total_bytes') or d.get('total_bytes_estimate')
        if not total:
            return
        percent = min(99, int((d.get('downloaded_bytes') or 0) * 100 / total))
        now = monotonic()
        if percent <= state['percent'] or now - state['at'] < min_interval:
            return
        state.update(at=now, percent=percent)
        try:
            store.change(project_id, lambda data: data['analysis'].update(percent=percent) if data.get('analysis') else None)
        except FileNotFoundError:
            pass
    return hook

def _mensagem_de_download(bruto: str) -> str:
    """Traduz o erro do yt-dlp para algo que o usuário entenda e possa agir."""
    texto = bruto or ''
    # yt-dlp usa tanto 'you're' quanto 'you’re' (apóstrofo curvo)
    if 'Sign in to confirm' in texto or 'not a bot' in texto or 'LOGIN_REQUIRED' in texto:
        return (
            'O YouTube pediu verificação anti-robô e recusou o download. '
            'A causa é o IP marcado, e o que resolve é usar os cookies de uma sessão '
            'logada: abra Ajustes e clique em "Importar cookies do YT Saver" '
            '(feche o YT Saver por alguns segundos, senão ele prende o arquivo). '
            'Se os cookies estiverem vencidos, entre de novo na conta do Google dentro '
            'do YT Saver e importe outra vez.'
        )
    if 'Private video' in texto:
        return 'Este vídeo é privado: só quem tem acesso ao link consegue baixar.'
    if 'members-only' in texto or 'Join this channel' in texto:
        return 'Este vídeo é exclusivo para membros do canal.'
    if 'age' in texto.lower() and ('restrict' in texto.lower() or 'confirm your age' in texto.lower()):
        return 'Este vídeo tem restrição de idade e exige login para baixar.'
    if 'unavailable' in texto or 'not available' in texto:
        return ('Este vídeo não está disponível: pode ter sido removido, ser privado ou '
                'estar bloqueado na sua região.')
    if 'Unsupported URL' in texto:
        return 'Este link não é suportado. Use um link de vídeo do YouTube.'
    # sem caso específico: tira o prefixo técnico e o bloco de ajuda do yt-dlp
    limpo = texto.split('Use --cookies')[0]
    limpo = limpo.replace('ERROR: ', '').strip().split('\n')[0]
    return f'Falha no download: {limpo}' if limpo else 'Falha no download do vídeo.'


def _id_do_youtube(url: str) -> str | None:
    """Extrai o id de um link do YouTube (watch, youtu.be, shorts, embed)."""
    import re
    from urllib.parse import parse_qs, urlparse

    try:
        partes = urlparse(url)
    except ValueError:
        return None
    host = (partes.hostname or '').lower()
    if host.endswith('youtu.be'):
        ident = partes.path.strip('/').split('/')[0]
        return ident if len(ident) == 11 else None
    if 'youtube.com' not in host:
        return None
    if partes.path.startswith('/shorts/') or partes.path.startswith('/embed/'):
        ident = partes.path.strip('/').split('/')[1]
        return ident if len(ident) == 11 else None
    ident = (parse_qs(partes.query).get('v') or [''])[0]
    return ident if re.fullmatch(r'[\w-]{11}', ident or '') else None


def _conferir_video_existe(url: str) -> None:
    """Confirma o vídeo pelo oembed (que não sofre bloqueio anti-bot).

    Sem isso, um link quebrado e um IP marcado produzem a mesma mensagem de
    "confirme que você não é um robô" — e o usuário não descobre qual é o caso.
    """
    ident = _id_do_youtube(url)
    if not ident:
        return
    import urllib.error
    import urllib.request

    consulta = f'https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={ident}&format=json'
    try:
        with urllib.request.urlopen(consulta, timeout=10) as resposta:
            if resposta.status == 200:
                return
    except urllib.error.HTTPError as erro:
        if erro.code in (401, 403, 404):
            raise RuntimeError(
                f'Este vídeo não está acessível publicamente (id {ident}). '
                'Ele pode ter sido removido, ser privado ou estar restrito por região/idade. '
                'Abra o link no navegador para confirmar e use outro vídeo.'
            ) from erro
    except Exception:
        # sem rede para conferir: segue para o yt-dlp, que dá o erro real
        return


def _arquivos_de_cookie() -> list:
    """Candidatos a cookie, do preferido para o menos provável."""
    from pathlib import Path as _Path

    from backend.api.v1.youtube import get_cookies_file

    lista = []
    # cookie da sessão logada, na ordem: login feito dentro do TikClip, depois o
    # exportado do navegador do YT Saver
    pasta_dados = _Path(os.getenv('APPDATA', '')) / 'TikClip'
    for nome in ('cookies-youtube.txt', 'cookies-cef.txt'):
        caminho = pasta_dados / nome
        if caminho.is_file():
            lista.append(caminho)
    preferido = get_cookies_file()
    if preferido and _Path(preferido) not in lista:
        lista.append(_Path(preferido))
    for pasta in (_Path(os.getenv('APPDATA', '')) / 'TikClip', _Path.home() / 'Downloads'):
        # cookies-cef.txt vem primeiro: são os da sessão Google logada, os únicos
        # que passam do bloqueio anti-robô quando o IP está marcado
        for nome in ('cookies-cef.txt', 'cookies.txt', 'yt_cookies.txt', 'cookies (1).txt'):
            caminho = pasta / nome
            if caminho.is_file() and caminho not in lista:
                lista.append(caminho)
    return lista


_CACHE_COOKIE = {'testado': False, 'cookie': None, 'quando': 0.0}
_VALIDADE_CACHE_S = 900  # 15 min

# marca a última vez que o YouTube recusou por bloqueio anti-robô, para a
# interface avisar antes de o usuário tentar de novo
_ULTIMO_BLOQUEIO = {'quando': 0.0}


def registrar_bloqueio() -> None:
    _ULTIMO_BLOQUEIO['quando'] = monotonic()


def estado_bloqueio() -> dict:
    quando = _ULTIMO_BLOQUEIO['quando']
    if not quando:
        return {'bloqueado': False, 'ha_minutos': None}
    minutos = (monotonic() - quando) / 60
    return {'bloqueado': minutos < 60, 'ha_minutos': round(minutos, 1)}


def _cookie_que_funciona(url: str, candidatos: list):
    """Devolve o primeiro cookie (ou None) que entrega fluxo de vídeo de verdade.

    Um arquivo de cookie com sessão inválida faz o YouTube responder só com
    storyboards — o download falha com "Requested format is not available" mesmo
    quando baixar sem cookie nenhum funcionaria. Aqui a escolha é por teste.

    Duas travas importantes:
    - resultado fica em cache por 15 min, para não sondar a cada import;
    - se aparecer bloqueio anti-robô, para na hora. Insistir com os outros
      cookies só multiplica requisições e piora a marcação do IP.
    """
    agora = monotonic()
    if _CACHE_COOKIE['testado'] and (agora - _CACHE_COOKIE['quando']) < _VALIDADE_CACHE_S:
        return _CACHE_COOKIE['cookie']

    import yt_dlp

    from backend.utils.ffmpeg_utils import ytdlp_js_runtimes

    for cookie in [str(c) for c in candidatos] + [None]:
        # sem js_runtimes o próprio teste cai no bloqueio e aborta antes de
        # experimentar o cookie da sessão (foi o que aconteceu em 22/10)
        opcoes = {'skip_download': True, 'quiet': True, 'no_warnings': True,
                  'socket_timeout': 30, 'noplaylist': True, **ytdlp_js_runtimes()}
        if cookie:
            opcoes['cookiefile'] = cookie
        try:
            with yt_dlp.YoutubeDL(opcoes) as y:
                info = y.extract_info(url, download=False, process=False) or {}
            formatos = info.get('formats') or []
            reais = [f for f in formatos
                     if f.get('vcodec') not in (None, 'none') and f.get('url')]
            if reais:
                rotulo = 'sem cookies' if not cookie else cookie
                logger.info('[download] cookie utilizável: %s (%d fluxos)', rotulo, len(reais))
                _CACHE_COOKIE.update(testado=True, cookie=cookie, quando=agora)
                return cookie
        except Exception as exc:  # noqa: BLE001
            texto = str(exc)
            if 'not a bot' in texto or 'Sign in to confirm' in texto:
                _CACHE_COOKIE.update(testado=False, cookie=None, quando=0.0)
                registrar_bloqueio()
                logger.warning('[download] bloqueio anti-robô; parando as tentativas')
                raise RuntimeError(_mensagem_de_download(texto)) from exc
            logger.info('[download] cookie descartado (%s): %s', cookie or 'sem cookies',
                        texto.split('\n')[0][:90])

    _CACHE_COOKIE.update(testado=True, cookie=None, quando=agora)
    return None


def download(project_id, url, browser):
    from backend.utils.ffmpeg_utils import get_ffmpeg_path, ytdlp_js_runtimes
    _conferir_video_existe(url)
    folder = store.directory(project_id) / 'raw'
    folder.mkdir(exist_ok=True)
    options = {'format': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]', 'outtmpl': str(folder / 'input.%(ext)s'), 'merge_output_format': 'mp4', 'noplaylist': True, 'quiet': True, 'ffmpeg_location': get_ffmpeg_path(), 'socket_timeout': 30, 'retries': 2, 'progress_hooks': [download_progress_hook(project_id)], **ytdlp_js_runtimes()}

    cookie_path = _cookie_que_funciona(url, _arquivos_de_cookie())
    if cookie_path:
        logger.info(f"[Studio download] Usando cookies de: {cookie_path}")
        options['cookiefile'] = cookie_path
    elif browser:
        options['cookiesfrombrowser'] = (browser,)

    from backend.utils.download_recovery import download_with_recovery
    try:
        download_with_recovery(url, {**options, 'writeinfojson': True})
    except Exception as exc:
        mensagem = _mensagem_de_download(str(exc))
        if 'anti-robô' in mensagem:
            registrar_bloqueio()
        raise RuntimeError(mensagem) from exc
    # Public listing text only: packaging checks nameplate names against it instead of guessing.
    info_path = folder / 'input.info.json'
    try:
        info = json.loads(info_path.read_text(encoding='utf-8')) if info_path.exists() else {}
    except (OSError, ValueError):
        info = {}
    finally:
        info_path.unlink(missing_ok=True)
    meta = {key: str(info.get(field) or '')[:300] for key, field in (('title', 'title'), ('channel', 'channel'))}
    # Contexto do vídeo original: serve para hashtags e nomes do pacote.
    descricao = str(info.get('description') or '')
    tags_origem = [str(t) for t in (info.get('tags') or []) if str(t).strip()][:20]
    meta['description'] = descricao[:1200]
    meta['tags'] = tags_origem
    meta['categories'] = [str(c) for c in (info.get('categories') or [])][:3]
    meta['uploader'] = str(info.get('uploader') or '')[:200]
    meta['url'] = url[:300]
    # Hashtags que o próprio autor usou (ou que aparecem na descrição): são prova,
    # não invenção — entram como semente na geração das hashtags do corte.
    achados = re.findall(r'#([\w\u00c0-\u024f]{2,30})', f'{info.get("title") or ""} {descricao}')
    meta['hashtags'] = [h for h in dict.fromkeys(achados)][:15]
    height = _ensure_source_resolution(url, options, info, folder)
    if height:
        meta['source_height'] = height
    if _fetch_platform_subtitles(url, options, info, folder):
        meta['subtitle_source'] = 'platform'
    store.change(project_id, lambda data: data.update(source_meta=meta))
    from backend.core.database import SessionLocal
    from backend.models.project import Project
    from backend.utils.thumbnail_generator import generate_project_thumbnail
    video = source(project_id)
    with SessionLocal() as db:
        p = db.query(Project).filter(Project.id == project_id).first()
        if not p:
            raise FileNotFoundError('O projeto foi excluído')
        p.video_path = str(video)
        p.thumbnail = generate_project_thumbnail(project_id, video)
        db.commit()


MIN_SOURCE_HEIGHT = 720
# Clients tried when YouTube serves only low-resolution formats to the default one
# (its SABR streaming experiment drops the URLs of every format above 360p for some sessions).
RETRY_CLIENTS = (['web_safari'], ['web_embedded'], ['tv'])


def _video_height(path):
    from backend.services.publish_export import _probe
    try:
        return int(_probe(path).get('height') or 0)
    except Exception:  # noqa: BLE001
        return 0


def _ensure_source_resolution(url, options, info, folder):
    """Re-download with another YouTube client when the file is far below what the video offers.

    A 360p source cropped to 1080×1920 is unusable; the listing usually still offers 1080p.
    Returns the final source height (0 when unknown).
    """
    try:
        current = source_path = next(p for p in folder.glob('input.*') if p.suffix.lower() in ('.mp4', '.mkv', '.webm', '.mov'))
    except StopIteration:
        return 0
    height = _video_height(current)
    offered = max((f.get('height') or 0 for f in info.get('formats') or [] if f.get('vcodec') not in (None, 'none')), default=0)
    want = min(MIN_SOURCE_HEIGHT, offered or MIN_SOURCE_HEIGHT)
    youtube = 'youtube.com' in url or 'youtu.be' in url
    if not youtube or not height or height >= want:
        return height
    from backend.utils.download_recovery import download_with_recovery
    retry = folder / 'retry'
    for client in RETRY_CLIENTS:
        shutil.rmtree(retry, ignore_errors=True)
        retry.mkdir()
        try:
            download_with_recovery(url, {**{k: v for k, v in options.items() if k != 'progress_hooks'},
                                         'outtmpl': str(retry / 'input.%(ext)s'), 'extractor_args': {'youtube': {'player_client': client}}})
            candidate = next(retry.glob('input.*'))
        except Exception as error:  # noqa: BLE001 - keep the file we have
            logger.warning('Re-download with %s failed: %s', client[0], type(error).__name__)
            continue
        better = _video_height(candidate)
        if better > height:
            source_path.unlink(missing_ok=True)
            source_path = folder / candidate.name
            candidate.replace(source_path)
            height = better
        if height >= want:
            break
    shutil.rmtree(retry, ignore_errors=True)
    if height < want:
        logger.warning('Source stays at %sp although %sp is offered', height, offered)
    return height


MIN_PLATFORM_CUES = 20


def _fetch_platform_subtitles(url, options, info, folder):
    """Use the creator's own subtitles in the video's language instead of speech recognition.

    Uploaded subtitles are punctuated and human-checked, and skip ~9 minutes of local Whisper per
    two hours of audio. Automatic captions are not used: they have no punctuation and repeat
    rolling lines, which breaks sentence-based cut points. Any problem keeps speech recognition.
    """
    language = str(info.get('language') or '').lower()
    manual = info.get('subtitles') or {}
    code = next((c for c in manual if language and (c.lower() == language or c.lower().split('-')[0] == language)), None)
    target = folder / 'input.srt'
    if not code or target.exists():
        return False
    try:
        from backend.utils.download_recovery import download_with_recovery
        download_with_recovery(url, {
            **{key: value for key, value in options.items() if key not in ('format', 'merge_output_format', 'progress_hooks')},
            'skip_download': True, 'writesubtitles': True, 'subtitleslangs': [code], 'subtitlesformat': 'srt/best',
            'postprocessors': [{'key': 'FFmpegSubtitlesConvertor', 'format': 'srt'}], 'outtmpl': str(folder / 'platform.%(ext)s'),
        })
        fetched = folder / f'platform.{code}.srt'
        if not fetched.exists() or fetched.read_text(encoding='utf-8', errors='ignore').count('-->') < MIN_PLATFORM_CUES:
            return False
        fetched.replace(target)
        return True
    except Exception as error:  # noqa: BLE001 - speech recognition still works
        logger.warning('Platform subtitles unavailable: %s', type(error).__name__)
        return False
    finally:
        for leftover in folder.glob('platform.*'):
            leftover.unlink(missing_ok=True)


def ensure_project_thumbnail(project_id):
    """Local uploads need the same thumbnail initialization as URL imports."""
    from backend.core.database import SessionLocal
    from backend.models.project import Project
    from backend.utils.thumbnail_generator import generate_project_thumbnail
    try:
        with SessionLocal() as db:
            project = db.get(Project, project_id)
            if project is None or project.thumbnail:
                return
        thumbnail = generate_project_thumbnail(project_id, source(project_id))
        if thumbnail:
            with SessionLocal() as db:
                project = db.get(Project, project_id)
                if project is not None and not project.thumbnail:
                    project.thumbnail = thumbnail
                    db.commit()
    except Exception:
        logger.warning('Project thumbnail generation failed', exc_info=True)


def run_content(project_id, video):
    """Run the existing content pipeline in this worker, without a second broker queue."""
    from backend.tasks.processing import process_video_pipeline
    srt = video.parent / 'input.srt'
    result = process_video_pipeline.apply(kwargs={
        'project_id': project_id, 'input_video_path': str(video),
        'input_srt_path': str(srt) if srt.exists() else None,
        'clips_only': True,  # studio renders from the source: no clustering, no per-clip re-encode
    }, throw=True).get()
    if not result or not result.get('success'):
        message = (result or {}).get('error') or 'O corte de conteúdo não concluiu; verifique as configurações de voz e de modelo de texto e tente de novo.'
        failure = (result or {}).get('result') or {}
        # The original exception does not survive apply().get(): the adapter records the provider
        # HTTP status in the result dict and it is carried back here (RC156 Win QA #1).
        status = failure.get('http_status')
        status = status if type(status) is int and 400 <= status <= 599 else None
        if failure.get('error_code') or failure.get('stage'):
            from backend.pipeline.failures import PipelineFailure
            raise PipelineFailure(failure.get('stage', ''), message, code=failure.get('error_code') or '', http_status=status)
        error = RuntimeError(message)
        if status is not None:
            error.http_status = status
        raise error

    clips = result.get('result', {}).get('result', {}).get('titled_clips')
    if not clips:
        raise ValueError('Não extraí trechos utilizáveis; verifique o áudio/legenda ou mude o plano para destaque.')

    return clips

def _apply_strategy(draft, strategy_id, *, burned_subtitles=False, layout=None):
    """Materialize platform packaging defaults without reinterpreting content.

    When the source already has subtitles in the picture, do not stack a second track, and
    keep the full frame on vertical outputs so cropping cannot cut the original lines.
    `layout` overrides the strategy default once framing has decided crop vs full frame.
    """
    from backend.services.platform_strategy import platform_strategy
    strategy = platform_strategy(strategy_id)
    value = dict(draft)
    layout = layout or (strategy.layout if strategy.layout != 'none' else value.get('layout', 'fit'))
    if value.get('caption_mask'):
        burned_subtitles = False  # blurred out: our own captions and framing apply
    if burned_subtitles and strategy.aspect == 'portrait' and layout == 'crop':
        layout = 'blur'
    value.update(
        aspect=strategy.aspect,
        layout=layout,
        subtitle_style=strategy.subtitle_style,
        title_style=strategy.title_style,
        title_motion=strategy.title_motion,
    )
    if strategy.template == 'landscape' and strategy_id != 'original' and value.get('language', 'source') == 'source':
        # No template packaging here: the legacy captions and hook are translated at render time
        # (skipped when already in this language), so English YouTube never burns Chinese text.
        value['language'] = strategy.audience_language
    if burned_subtitles:
        value['subtitles'] = False
    # The template version follows the new title style: a draft derived for one platform (comic, v6)
    # must not carry a version the next platform's style rejects (card only supports v1).
    version = value.get('title_template_version', 1)
    if value['title_style'] == 'editorial':
        value['title_template_version'] = max(2, version if version in (2, 3, 6) else 2)
    elif value['title_style'] == 'comic':
        value['title_template_version'] = 6
    elif value['title_style'] in ('plain', 'impact', 'card'):
        value['title_template_version'] = 1
    elif value['title_style'] in ('pixel', 'frosted') and version not in (3, 6):
        value['title_template_version'] = 6
    elif version in (4, 5):
        value['title_template_version'] = 6
    return Draft.model_validate(value)


def _complete_thought_bounds(project_id, clips):
    """Clip bounds that start and end on complete sentences, and complete thoughts when a text model is set up."""
    from backend.services.studio import boundaries
    try:
        from backend.services.publish_export import _load_srt_entries
        rows = boundaries.rows_from_entries(_load_srt_entries(project_id))
    except Exception:  # noqa: BLE001 - no subtitles: keep the pipeline's bounds
        return clips
    call = None
    try:
        from backend.utils.llm_client import LLMClient
        if LLMClient().llm_manager.get_current_provider_info().get('available'):
            call = intelligence.text_json
    except Exception:  # noqa: BLE001
        call = None
    with llm_usage.stage('boundaries'), llm_usage.timed('boundaries'):
        return boundaries.refine_clips(rows, clips, call, boundaries.audio_silences(source(project_id)) if audio.has_audio(source(project_id)) else None)


MAX_DRAFT_OVERLAP = 0.5  # share of the shorter clip two content drafts may have in common


def _overlap_share(a, b):
    shared = min(a[1], b[1]) - max(a[0], b[0])
    shorter = min(a[1] - a[0], b[1] - b[0])
    return shared / shorter if shared > 0 and shorter > 0 else 0.0


def _distinct_ranges(picked, bounds):
    """(clip, start, end) without near-duplicate drafts (RC156 Win QA #7).

    Boundary polishing may push two clips onto the same span. Higher-scored clips keep their
    polished range; a lower-scored one falls back to its original pipeline range, and is
    dropped only when that still mostly repeats a kept clip. Original order is preserved.
    """
    order = sorted(range(len(picked)), key=lambda i: _score(picked[i][0]), reverse=True)
    kept = {}
    for i in order:
        clip, original_start, original_end = picked[i]
        for candidate in (tuple(bounds[i]), (original_start, original_end)):
            if all(_overlap_share(candidate, other) <= MAX_DRAFT_OVERLAP for other in kept.values()):
                kept[i] = candidate
                break
        else:
            logger.info('Dropped a content clip that repeats a higher-scored clip')
    return [(picked[i][0], *kept[i]) for i in sorted(kept)]


def _content_drafts(project_id, plan, video):
    """Create drafts once from the selected content route, before platform derivation."""
    route = plan.get('recommended_analysis', 'subtitle')
    prefs = Preferences.model_validate(plan['preferences'])
    if route == 'subtitle' or prefs.goal == 'content':
        clips = run_content(project_id, video)
        from backend.utils.text_processor import TextProcessor
        picked = []
        for clip in clips:
            try:
                start = float(clip.get('start_time_seconds')) if clip.get('start_time_seconds') is not None else TextProcessor.time_to_seconds(clip['start_time'])
                end = float(clip.get('end_time_seconds')) if clip.get('end_time_seconds') is not None else TextProcessor.time_to_seconds(clip['end_time'])
            except (KeyError, TypeError, ValueError):
                continue
            picked.append((clip, start, end))
        bounds = _complete_thought_bounds(project_id, [(s, e) for _, s, e in picked])
        source_duration = float(intelligence._probe(video).get('duration') or 0)
        drafts = []
        for clip, start, end in _distinct_ranges(picked, bounds):
            # Transcript timestamps can run past the final video frame. Keep valid content
            # from that clip, and never let one invalid range discard the whole batch.
            end = min(end, source_duration)
            try:
                scene = Scene(
                    id=uuid.uuid4().hex,
                    label=str(clip.get('generated_title') or clip.get('outline') or 'Trecho de conteúdo')[:120],
                    start=start,
                    end=end,
                )
                intelligence.validate_scenes([scene], source_duration)
            except (KeyError, TypeError, ValueError):
                continue
            drafts.append({**Draft(
                id=uuid.uuid4().hex, title=scene.label, scenes=[scene], origin='auto-content',
                language=prefs.language, aspect=prefs.aspect, layout='crop' if prefs.aspect == 'portrait' else 'fit',
                subtitles=True,
            ).model_dump(), SCORE_KEY: _score(clip)})
        if not drafts:
            raise ValueError('Não extraí trechos para renderizar; verifique a legenda ou importe o vídeo de novo.')
        return drafts
    if route != 'visual':
        raise ValueError('Não há caminho de análise automática disponível; verifique as configurações de modelo e tente de novo.')
    if not intelligence.ready():
        raise ValueError('Configure antes um modelo de compreensão visual nos ajustes')
    try:
        events, coverage = analyze(video, prefs)
        drafts = make_drafts(events, prefs, plan.get('overrides', {}).get('instruction', ''), source_duration=intelligence._probe(video).get('duration'))
    except ValueError as error:
        # A silent source that the visual route could not cut (RC156 Win QA #19): explain the missing
        # sound and point to an SRT instead of a bare 「Nenhum trecho aproveitável foi encontrado」. Model/network errors
        # (VisionRequestError) keep their own message.
        from backend.pipeline import media_precheck
        silent = media_precheck.silent_visual_failure(video, srt_available=(Path(video).parent / 'input.srt').is_file())
        if silent is None:
            raise
        raise silent from error
    return drafts, [event.model_dump() for event in events], coverage


def _source_has_burned_subtitles(project_id, video=None):
    """Detect once per project and cache on the generation; detection failure means 'no'."""
    cached = (store.read(project_id).get('generation') or {}).get('source_has_burned_subtitles')
    if cached is not None:
        return bool(cached)
    try:
        from backend.services.publish_export import _probe
        from backend.services.studio.burned_subtitles import detect
        video = video or source(project_id)
        found, band = detect(video, float(_probe(video).get('duration') or 0))
    except Exception as error:  # noqa: BLE001 - never block output on a heuristic
        logger.warning('Burned subtitle detection failed: %s', type(error).__name__)
        found, band = False, None
    language = _burned_caption_language(video, found, (store.read(project_id).get('source_meta') or {}).get('title', ''))
    if language == 'none':
        found, language = False, None  # the vision model saw no captions: the pixel heuristic was fooled by scene text

    def remember(data):
        if data.get('generation') is not None:
            data['generation']['source_has_burned_subtitles'] = found
            if language:
                data['generation']['burned_caption_language'] = language
            if found and band:
                data['generation']['burned_caption_band'] = list(band)
    store.change(project_id, remember)
    return found


def _caption_mask(project_id, strategy_id):
    """The burned caption band to blur out of this version, or None.

    An English platform must not show the source's Chinese (or other non-English) captions:
    the band is blurred and the version is framed and captioned like a caption-free source.
    """
    from backend.services.platform_strategy import platform_strategy
    generation = store.read(project_id).get('generation') or {}
    band, language = generation.get('burned_caption_band'), generation.get('burned_caption_language')
    if not (generation.get('source_has_burned_subtitles') and band and language and language != 'en'):
        return None
    return list(band) if platform_strategy(strategy_id).audience_language == 'en' else None


BURNED_LANGUAGE_PROMPT = (
    'Estas imagens são a faixa inferior do mesmo vídeo. Diga se há legenda de diálogo gravada na '
    'imagem (aquela faixa que muda conforme a fala) e em que idioma ela está. '
    'Marca d\u2019água, logotipo, nome de produto, slide ou texto de tela não contam como legenda. '
    'Retorne somente JSON: {"language":"zh|en|ja|ko|other|none"} (none = não há legenda de diálogo).'
)


def _burned_caption_language(video, found, title=''):
    """Language of the captions burned into the picture.

    The speech language is not enough: foreign interviews often carry English captions. The vision
    model reads them when one is set up; otherwise captions are taken to be written for the
    video's own audience, i.e. in the language of its title. None means unknown (packaging then
    assumes the speech language).
    """
    if not found:
        return None
    from backend.services.studio.packaging import source_language
    guess = source_language([title]) if title else None
    guess = guess if guess in ('zh', 'en') else None
    if not intelligence.source_frame_analysis_allowed():
        return guess
    import base64
    import subprocess
    import tempfile
    from backend.utils.ffmpeg_utils import get_ffmpeg_path
    try:
        from backend.services.publish_export import _probe
        duration = float(_probe(video).get('duration') or 0)
        with tempfile.TemporaryDirectory(prefix='ac-captions-') as folder:
            frames = []
            for index, at in enumerate((duration * 0.3, duration * 0.5, duration * 0.7)):
                path = Path(folder) / f'{index}.jpg'
                subprocess.run([get_ffmpeg_path(), '-v', 'error', '-ss', f'{at:.2f}', '-i', str(video), '-frames:v', '1',
                                '-vf', 'crop=iw:ih*0.35:0:ih*0.65,scale=960:-2', '-y', str(path)], check=True, capture_output=True, timeout=30)
                frames.append({'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(path.read_bytes()).decode()}})
            from backend.services.studio.vision_settings import effective
            with llm_usage.stage('burned_captions'):
                answer = intelligence.vision_call([{'type': 'text', 'text': BURNED_LANGUAGE_PROMPT}, *frames], {**effective(), 'quick_screening': True})
        language = str((answer or {}).get('language') or '').lower()
        return language if language in ('zh', 'en', 'ja', 'ko', 'other', 'none') else guess
    except Exception as error:  # noqa: BLE001 - fall back to the title's language
        logger.warning('Burned caption language unknown: %s', type(error).__name__)
        return guess


def _prepare_speaker_framing(platforms):
    """Start the on-demand face-detection install as soon as a vertical output is requested."""
    from backend.services.platform_strategy import platform_strategy
    if not any(platform_strategy(p).aspect == 'portrait' for p in platforms):
        return
    try:
        from backend.services.studio import framing
        framing.start_install()
    except Exception as error:  # noqa: BLE001 - framing is an enhancement, never a blocker
        logger.warning('Framing install could not start: %s', type(error).__name__)


def _speaker_framing(value, video, *, window=None, wait_sec=120, scans=None):
    """Speaker-following crop tracks for a vertical draft (or the given output window).

    Returns (scenes, framing): `speaker` when faces drive the crop, `full_frame` when the clip has
    nobody to follow, `full_frame_pending` when the detector is not available yet.
    """
    from backend.services.studio import framing
    deadline = monotonic() + wait_sec
    while not framing.is_installed() and framing.get_status().get('status') == 'installing' and monotonic() < deadline:
        sleep(2)
    if not framing.is_installed():
        return None, 'full_frame_pending'
    from backend.services.publish_export import _probe
    info = _probe(video)
    if not info.get('width') or not info.get('height'):
        return None, 'full_frame'
    draft = Draft.model_validate({**value, 'aspect': 'portrait', 'layout': 'crop'})
    # Detection does not depend on the window: one pass serves the interview and podcast crops.
    key = tuple((scene.start, scene.end) for scene in draft.scenes)
    if scans is None or key not in scans:
        detected = framing.scan_speakers(video, draft.scenes)
        if scans is None:
            scans = {}
        scans[key] = detected
    result = framing.auto_frame(video, draft, int(info['width']), int(info['height']), window=window, scans=scans[key])
    tracks = {scene['id']: scene for scene in result['scenes']}
    if not any(scene.get('faces') for scene in result['scenes']):
        return None, 'full_frame'
    scenes = [{**scene, 'crop_x': tracks[scene['id']]['crop_x'], 'crop_track': tracks[scene['id']]['crop_track'],
               'framing_source': 'auto', 'framing_adjusted': False} for scene in value['scenes']]
    return scenes, 'speaker'


INTERVIEW_WINDOW = (1080, 810)  # 4:3 speaker window of the interview template


def _project_strategy(project_id, strategy_id):
    """Visual template is a preference; platform language, dimensions and limits stay intact."""
    from dataclasses import replace
    from backend.services.platform_strategy import platform_strategy
    strategy = platform_strategy(strategy_id)
    style = (store.read(project_id).get('generation') or {}).get('portrait_style', 'auto')
    template = {'interview': 'interview_zh', 'podcast': 'podcast_en'}.get(style)
    return replace(strategy, template=template) if template and strategy.aspect == 'portrait' else strategy


def _apply_framing(project_id, value, strategy_id, video, burned, cache):
    """Pick true vertical speaker framing when it is safe; otherwise keep the full frame on a backdrop.

    Burned-in captions would be cut by a 9:16 window, so those sources always keep the full frame.
    Tracks depend only on the scenes, so one detection pass is shared by every vertical platform.
    The interview template frames a 4:3 window instead; without a track it shows the whole frame.
    """
    strategy = _project_strategy(project_id, strategy_id)
    if strategy.aspect != 'portrait':
        return value, None
    interview = strategy.template == 'interview_zh'
    fallback_layout = 'window' if interview else 'blur'
    if burned and _caption_mask(project_id, strategy_id):
        burned = False  # the band is blurred out, so cropping cannot cut captions anyone reads
    if burned:
        plain = [{**scene, 'crop_x': None, 'crop_track': None, 'framing_source': None, 'framing_adjusted': False} for scene in value['scenes']]
        return {**value, 'scenes': plain, 'layout': fallback_layout}, 'full_frame_captions'
    window = INTERVIEW_WINDOW if interview else None
    key = (tuple((scene['start'], scene['end']) for scene in value['scenes']), window)
    if key not in cache:
        try:
            cache[key] = _speaker_framing(value, video, window=window, scans=cache.setdefault('scans', {}))
        except Exception as error:  # noqa: BLE001 - fall back to the full frame rather than fail output
            logger.warning('Speaker framing failed: %s', type(error).__name__)
            capture_studio_exception(error, 'auto_frame')
            cache[key] = (None, 'full_frame')
    scenes, framing = cache[key]
    if scenes is None:
        # Drop tracks inherited from another platform's framing: they were computed for a different window.
        plain = [{**scene, 'crop_x': None, 'crop_track': None, 'framing_source': None, 'framing_adjusted': False} for scene in value['scenes']]
        return {**value, 'scenes': plain, 'layout': fallback_layout}, framing
    return {**value, 'scenes': scenes, 'layout': 'window' if interview else 'crop'}, framing


def _audience_title(value, strategy_id, post):
    """The version's title in its audience's language: clip titles are written in Chinese."""
    from backend.services.platform_strategy import platform_strategy
    from backend.services.studio.packaging import CJK
    if platform_strategy(strategy_id).audience_language != 'en' or not CJK.search(value.get('title') or ''):
        return value
    from backend.services.studio.packaging import foreign_for
    lines = (value.get('packaging') or {}).get('title_lines') or []
    english = ' '.join(lines) if lines and not any(foreign_for('en', line) for line in lines) else (post or {}).get('title', '')
    if not english or foreign_for('en', english):
        # No English title came back: an English caption line beats the Chinese clip title.
        cues = (value.get('packaging') or {}).get('cues') or []
        english = next((cue['text'] for cue in cues if cue.get('text') and not foreign_for('en', cue['text'])), '') or 'Untitled clip'
    return {**value, 'title': english[:120]}


def _content_key(value):
    return tuple((scene['start'], scene['end']) for scene in value['scenes'])


def _posts_for(project_id, items, cache):
    """{(content key, strategy id): post copy}: one model call per clip covering all its platforms, side by side."""
    from backend.pipeline.concurrency import map_chunks
    from backend.services.studio import packaging, post_copy
    _packaging_inputs(project_id, cache)
    clips = {}
    for strategy_id, value in items:
        clips.setdefault(_content_key(value), (value, []))[1].append(strategy_id)

    def one(entry):
        key, (value, platforms) = entry
        lines = [line['text'] for line in packaging.draft_lines(cache['entries'], value['scenes'])]
        title = ((value.get('packaging') or {}).get('title_lines') and ''.join(value['packaging']['title_lines'])) or value.get('title', '')
        meta = cache.get('meta') or {}
        # Contexto do vídeo original (link): título, canal, categorias, tags do autor e
        # hashtags que ele mesmo usou — matéria-prima para as hashtags do corte.
        contexto = ' '.join(filter(None, [
            str(meta.get('title') or ''), str(meta.get('channel') or ''), str(meta.get('uploader') or ''),
            ' '.join(meta.get('categories') or []),
            ' '.join(meta.get('tags') or []),
            ' '.join('#' + str(h) for h in (meta.get('hashtags') or [])),
            str(meta.get('description') or '')[:500],
        ]))
        return key, post_copy.build_posts(title, lines, platforms, source=contexto,
                                          source_tags=meta.get('hashtags') or [])

    try:
        with llm_usage.stage('post_copy'):
            results = map_chunks(one, clips.items())
    except Exception as error:  # noqa: BLE001 - copy is never a blocker
        logger.warning('Post copy failed: %s', type(error).__name__)
        return {}
    return {(key, platform): post for key, by_platform in results for platform, post in by_platform.items()}


def _design_covers(project_id, draft, job_id):
    """Design the cover of every variant this render belongs to; never fails the render."""
    from backend.services.studio import publish_kit
    state = store.read(project_id)
    targets = [item for item in state.get('output_variants', []) if item.get('render_job_id') == job_id]
    designed = []
    for variant in targets:
        try:
            publish_kit.design_cover(project_id, source(project_id), draft.model_dump() if hasattr(draft, 'model_dump') else draft,
                                     job_id, variant['strategy_id'], (variant.get('post') or {}).get('title', ''))
            designed.append(variant['id'])
        except Exception as error:  # noqa: BLE001 - the video is done; the cover can be redone from the card
            logger.warning('Cover design failed: %s', type(error).__name__)
    if designed:
        def mark(data):
            for item in data.get('output_variants', []):
                if item['id'] in designed and item.get('cover') != 'ai':
                    item['cover'] = 'design'
        store.change(project_id, mark)
        for variant in targets:
            if variant['id'] in designed:
                request_ai_cover(project_id, variant['id'])


cover_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix='studio-cover')


def request_ai_cover(project_id, variant_id):
    """Share one tracked AI task between automatic covers and requests from the output card."""
    from backend.services import cover
    cfg = cover.load_config()
    if not (cfg.enabled and cfg.configured and cfg.allow_send_frame):
        return None
    job_id = uuid.uuid4().hex

    def claim(data):
        variant = next((item for item in data['output_variants'] if item['id'] == variant_id), None)
        if variant is None:
            raise FileNotFoundError('A versão da peça final não existe')
        active = variant.get('cover_job')
        if active and active['status'] in ('queued', 'running'):
            return active
        variant['cover_job'] = {'job_id': job_id, 'status': 'queued', 'instance': store.INSTANCE}
        return variant['cover_job']

    with store.lock:
        claimed = store.change(project_id, claim)
        if claimed['job_id'] == job_id:
            try:
                cover_executor.submit(_ai_cover_job, project_id, variant_id, job_id, _io_receipt=('cover', variant_id, job_id))
            except Exception as error:  # noqa: BLE001 - preserve the ready video and designed cover
                store.remember_submission_rejection(project_id, ('cover', variant_id, job_id), error_code=studio_error_code(error))
                logger.warning('AI cover dispatch failed: %s', type(error).__name__)
                _update_cover_job(project_id, variant_id, job_id, status='failed', error='A tarefa de capa com IA não conseguiu iniciar; tente de novo.')
        return ai_cover_status(project_id, variant_id)


def ai_cover_status(project_id, variant_id):
    variant = next((item for item in store.read(project_id)['output_variants'] if item['id'] == variant_id), None)
    if variant is None or not variant.get('cover_job'):
        raise FileNotFoundError('A tarefa de capa não existe')
    return {key: value for key, value in variant['cover_job'].items() if key != 'instance'}


def _update_cover_job(project_id, variant_id, job_id, **values):
    def update(data):
        variant = next(item for item in data['output_variants'] if item['id'] == variant_id)
        job = variant.get('cover_job') or {}
        if job.get('job_id') == job_id:
            job.update(values)
            if values.get('status') == 'completed':
                variant['cover'] = 'ai'
    store.change(project_id, update)


@_tracked('ai_cover')
def _ai_cover_job(project_id, variant_id, job_id):
    from backend.services.studio import publish_kit
    try:
        _update_cover_job(project_id, variant_id, job_id, status='running')
        variant = next(item for item in store.read(project_id).get('output_variants', []) if item['id'] == variant_id)
        with llm_usage.timed('ai_cover'):
            made = publish_kit.ai_cover(project_id, variant['render_job_id'], variant['strategy_id'])
    except Exception as error:  # noqa: BLE001 - the designed cover stays
        store.raise_owned_read_error(error)
        logger.warning('AI cover job failed: %s', type(error).__name__)
        made = False
    _update_cover_job(project_id, variant_id, job_id, **({'status': 'completed'} if made else
                      {'status': 'failed', 'error': 'A capa com IA não foi gerada; a capa anterior foi mantida'}))


def update_post(project_id, variant_id, post):
    """Save the user's edited copy for one variant (limits re-applied)."""
    from backend.services.studio import post_copy
    from backend.services.studio.models import PostCopy
    state = store.read(project_id)
    variant = next((item for item in state.get('output_variants', []) if item['id'] == variant_id), None)
    if not variant:
        raise FileNotFoundError('A versão da peça final não existe')
    rules = post_copy.RULES.get(variant['strategy_id'], post_copy.RULES['original'])
    clean = PostCopy(title=post_copy._fit(post_copy._clean(post.title), rules.title_max) or variant.get('post', {}).get('title', ''),
                     description=post_copy._fit(post_copy._clean(post.description), rules.description_max),
                     tags=post_copy._tags(post.tags, rules)).model_dump()
    store.change(project_id, lambda data: next(item for item in data['output_variants'] if item['id'] == variant_id).update(post=clean))
    return clean


def _packaging_inputs(project_id, cache):
    """Subtitle rows and listing names shared by every packaging call of one batch."""
    if 'entries' not in cache:
        from backend.services.publish_export import _load_srt_entries
        try:
            cache['entries'] = _load_srt_entries(project_id)
        except Exception:  # noqa: BLE001 - no subtitles: packaging falls back to the title only
            cache['entries'] = []
        state = store.read(project_id)
        meta = state.get('source_meta') or {}
        cache['names'] = f"{meta.get('title', '')} {meta.get('channel', '')}"
        cache['meta'] = meta
        cache['burned_language'] = (state.get('generation') or {}).get('burned_caption_language')


def _prefetch_packaging(project_id, items, burned, cache):
    """Run the packaging model calls of a batch side by side (one per distinct content and template).

    Each call waits ~20 s on the model; done one after another they were ~8 minutes for 25 clips.
    """
    from backend.pipeline.concurrency import map_chunks
    _packaging_inputs(project_id, cache)  # load shared inputs once, before the threads
    first = {}
    for strategy_id, value in items:
        strategy = _project_strategy(project_id, strategy_id)
        key = (tuple((scene['start'], scene['end']) for scene in value['scenes']), strategy.template, strategy.audience_language)
        first.setdefault(key, (strategy_id, value))
    map_chunks(lambda item: _apply_packaging(project_id, item[1], item[0], burned, cache), first.values())


def _apply_packaging(project_id, value, strategy_id, burned, cache):
    """Attach template packaging (title, captions, nameplates, tags) for vertical templates.

    One model call per scene set and template; every vertical platform of the same audience
    reuses it. Only the subtitle rows this draft uses are sent.
    """
    strategy = _project_strategy(project_id, strategy_id)
    mask = _caption_mask(project_id, strategy_id) if burned else None
    value = {**{key: item for key, item in value.items() if key != 'caption_mask'}, **({'caption_mask': mask} if mask else {})}
    if mask:
        burned = False
    if strategy.template not in ('interview_zh', 'podcast_en'):
        return {key: item for key, item in value.items() if key != 'packaging'}  # never inherit another template's packaging
    from backend.services.studio import packaging
    _packaging_inputs(project_id, cache)
    key = (tuple((scene['start'], scene['end']) for scene in value['scenes']), strategy.template, strategy.audience_language)
    if key not in cache:
        lines = packaging.draft_lines(cache['entries'], value['scenes'])
        used = cache.setdefault('palettes', [])
        with llm_usage.stage('packaging'):
            cache[key] = packaging.build_packaging(value, lines, strategy, burned=burned, burned_language=cache.get('burned_language'),
                                                   known_names=cache['names'],
                                                   avoid_palettes=tuple(used[-2:]))
        if cache[key].get('palette'):
            used.append(cache[key]['palette'])
    return {**value, 'packaging': cache[key]}


def _fit_platform_limit(project_id, value, strategy):
    """Trim a draft to the platform's hard length limit at the last subtitle sentence end.

    Only strategies with a real platform cap (YouTube Shorts) set `max_duration_sec`; every
    other platform keeps the complete moment. Returns (draft, trimmed_to_seconds_or_None).
    """
    limit = strategy.max_duration_sec
    scenes = value.get('scenes', [])
    if not limit or sum(s['end'] - s['start'] for s in scenes) <= limit:
        return value, None
    try:
        from backend.services.publish_export import _load_srt_entries, to_seconds
        ends = sorted(to_seconds(e['end_time']) for e in _load_srt_entries(project_id) if e.get('end_time'))
    except Exception:  # noqa: BLE001 - no subtitles: fall back to an exact cut
        ends = []
    remaining, kept = float(limit), []
    for scene in scenes:
        length = scene['end'] - scene['start']
        if length <= remaining:
            kept.append(scene)
            remaining -= length
            continue
        target = scene['start'] + remaining
        boundary = [end for end in ends if scene['start'] + 1 < end <= target]
        kept.append({**scene, 'end': max(boundary) if boundary else target})
        break
    return {**value, 'scenes': kept}, limit


@_tracked('production')
def _auto_generate(project_id, plan):
    """Produce and render platform variants after the cheap screening pass."""
    started = monotonic()
    state = store.read(project_id)
    generation = state.get('generation') or {}
    platforms = generation.get('requested_platforms') or []
    branding = generation.get('branding') or {'outro_enabled': True, 'outro_version': 'v1'}
    video = source(project_id)
    skipped = []
    try:
        produced = _content_drafts(project_id, plan, video)
        if isinstance(produced, tuple):
            base_drafts, events, coverage = produced
        else:
            base_drafts, events, coverage = produced, [], None
        variants = []
        derived_drafts = []
        from backend.services.platform_strategy import platform_strategy
        burned = _source_has_burned_subtitles(project_id, video)
        framing_cache = {}
        packaging_cache = {}
        # Platform eligibility must precede the score limit: otherwise ten short clips
        # can exclude the only valid YouTube long clip and leave nothing to render.
        automatic = {
            strategy_id: _automatic_drafts([base for base in base_drafts
                if _eligible_for_platform(base, platform_strategy(strategy_id))])
            for strategy_id in platforms
        }
        planned = []
        for base in base_drafts:
            base = {key: item for key, item in base.items() if key != SCORE_KEY}
            for strategy_id in platforms:
                strategy = platform_strategy(strategy_id)
                if not _eligible_for_platform(base, strategy):
                    skipped.append({'strategy_id': strategy_id, 'reason': 'O material não tem conteúdo longo o bastante'})
                    continue
                value, trimmed = _fit_platform_limit(project_id, {**base, 'id': uuid.uuid4().hex, 'revision': 1}, strategy)
                now = base['id'] in automatic[strategy_id]
                framed = None  # on-demand versions are framed and packaged when the user asks (produce_variant)
                if now:
                    with llm_usage.timed('framing'):
                        value, framed = _apply_framing(project_id, value, strategy_id, video, burned, framing_cache)
                planned.append((strategy_id, value, trimmed, framed, now))
        with llm_usage.timed('packaging'):
            _prefetch_packaging(project_id, [(strategy_id, value) for strategy_id, value, _, _, now in planned if now], burned, packaging_cache)
        with llm_usage.timed('post_copy'):
            posts = _posts_for(project_id, [(strategy_id, value) for strategy_id, value, _, _, now in planned if now], packaging_cache)
        for strategy_id, value, trimmed, framed, now in planned:
            if now:
                value = _apply_packaging(project_id, value, strategy_id, burned, packaging_cache)
                value = _audience_title(value, strategy_id, posts.get((_content_key(value), strategy_id)))
            draft = _apply_strategy(value, strategy_id, burned_subtitles=burned, layout=value.get('layout') if framed else None)
            derived_drafts.append(draft.model_dump())
            variants.append({
                'id': uuid.uuid4().hex, 'draft_id': draft.id, 'draft_revision': draft.revision,
                'strategy_id': strategy_id, 'strategy_version': 1, 'branding': branding,
                'status': 'queued' if now else 'on_demand', 'created_at': store.now(),
                **({'trimmed_to_sec': trimmed} if trimmed else {}),
                **({'framing': framed} if framed else {}),
                **({'post': posts[_content_key(value), strategy_id]} if (_content_key(value), strategy_id) in posts else {}),
            })
        if not variants:
            raise ValueError('As plataformas escolhidas não têm versão completa de conteúdo para gerar')
        def persist(data):
            data['drafts'].extend({**draft, 'updated_at': store.now()} for draft in derived_drafts)
            if events:
                data['events'] = events
            data['output_variants'].extend(variants)
            data['generation'].update(status='rendering', skipped=skipped, started_at=data['generation'].get('started_at') or store.now(), **_generation_route(data.get('plan')))
            data['analysis'] = {'status': 'running', 'phase': 'rendering', 'run_id': (data.get('analysis') or {}).get('run_id'), 'message': 'Gerando a peça pronta para publicar', 'instance': store.INSTANCE, 'created_at': store.now()}
        store.change(project_id, persist)
        _dispatch_pending_variants(project_id)
    except Exception as error:
        store.raise_owned_read_error(error)
        capture_studio_exception(error, 'production')
        code = studio_error_code(error)
        context = studio_failure_context(error, 'production')
        error_text = str(error)[:700]
        def fail(data):
            if data.get('generation'):
                data['generation'].update(status='failed', error=error_text, error_code=code, skipped=skipped, finished_at=store.now(), **context)
            data['analysis'] = {'status': 'failed', 'phase': 'production', 'run_id': (data.get('analysis') or {}).get('run_id'), 'error': error_text, 'error_code': code, 'duration_ms': round((monotonic() - started) * 1000)}
        store.change(project_id, fail)
        mark_project(project_id, 'failed')


def append_platform_variants(project_id, platforms, branding):
    """Derive only new platform versions from saved drafts; never reanalyze source media."""
    state = store.read(project_id)
    if not (state.get('generation') or {}).get('auto_start'):
        raise ValueError('Este projeto ainda não usou o fluxo de geração automática')
    from backend.services.platform_strategy import normalize_platform_ids, platform_strategy
    selected = normalize_platform_ids(platforms)
    draft_by_id = {draft['id']: draft for draft in state.get('drafts', [])}
    existing = {
        (item['strategy_id'], tuple((scene['start'], scene['end']) for scene in draft_by_id.get(item['draft_id'], {}).get('scenes', [])))
        for item in state.get('output_variants', [])
    }
    # A moment rendered automatically on one platform renders automatically on the new one too;
    # moments left on demand stay on demand.
    automatic = {
        tuple((scene['start'], scene['end']) for scene in draft_by_id.get(item['draft_id'], {}).get('scenes', []))
        for item in state.get('output_variants', []) if item.get('status') != 'on_demand'
    }
    seen_scenes, variants, derived = set(), [], []
    burned = _source_has_burned_subtitles(project_id)
    video = source(project_id)
    framing_cache = {}
    packaging_cache = {}
    for base in state.get('drafts', []):
        signature = tuple((scene['start'], scene['end']) for scene in base.get('scenes', []))
        if not signature or signature in seen_scenes:
            continue
        seen_scenes.add(signature)
        for strategy_id in selected:
            if (strategy_id, signature) in existing:
                continue
            strategy = platform_strategy(strategy_id)
            if not _eligible_for_platform(base, strategy):
                continue
            value, trimmed = _fit_platform_limit(project_id, {**base, 'id': uuid.uuid4().hex, 'revision': 1}, strategy)
            now = signature in automatic
            framed = None
            post = None
            if now:
                value, framed = _apply_framing(project_id, value, strategy_id, video, burned, framing_cache)
                value = _apply_packaging(project_id, value, strategy_id, burned, packaging_cache)
                post = _posts_for(project_id, [(strategy_id, value)], packaging_cache).get((_content_key(value), strategy_id))
                value = _audience_title(value, strategy_id, post)
            draft = _apply_strategy(value, strategy_id, burned_subtitles=burned, layout=value.get('layout') if framed else None)
            derived.append(draft.model_dump())
            variants.append({
                'id': uuid.uuid4().hex, 'draft_id': draft.id, 'draft_revision': 1,
                'strategy_id': strategy_id, 'strategy_version': 1, 'branding': branding,
                'status': 'queued' if now else 'on_demand', 'created_at': store.now(),
                **({'trimmed_to_sec': trimmed} if trimmed else {}),
                **({'framing': framed} if framed else {}),
                **({'post': post} if post else {}),
            })
    if not variants:
        raise ValueError('Não há versão de plataforma para acrescentar: já existe ou o material não atende aos requisitos da plataforma escolhida')
    def persist(data):
        data['drafts'].extend({**draft, 'updated_at': store.now()} for draft in derived)
        data['output_variants'].extend(variants)
        data['generation'].update(status='rendering')
        data['analysis'] = {'status': 'running', 'phase': 'rendering', 'message': 'Acrescentando a versão da plataforma', 'instance': store.INSTANCE, 'created_at': store.now()}
    store.change(project_id, persist)
    _dispatch_pending_variants(project_id)
    return variants


def _dispatch_pending_variants(project_id):
    """Queue a render for every automatic variant that does not have one yet.

    There is no per-project cap on how many variants get produced: the source decides the
    count, and `render_executor` encodes them one at a time. Re-running is safe because
    `export` deduplicates identical draft snapshots and attached variants are skipped.
    """
    state = store.read(project_id)
    pending = [item for item in state.get('output_variants', []) if item.get('status') == 'queued' and not item.get('render_job_id')]
    drafts = {draft['id']: draft for draft in state.get('drafts', [])}
    for variant in pending:
        expected = store.change(project_id, lambda data: store.claim_render(data, store.variant_identity(variant)))
        if expected is None:
            continue
        def reject(data, message, code, expected=expected):
            target = next((item for item in data['output_variants'] if item['id'] == expected[0]), None)
            if target and store.variant_identity(target) == expected and target.get('status') == 'queued' and not target.get('render_job_id'):
                target.update(status='failed', error=message, error_code=code)
                target.pop('render_dispatch_claimed', None)
                target.pop('render_dispatch_instance', None)
        raw = drafts.get(variant['draft_id'])
        if not raw:
            store.change(project_id, lambda data: reject(data, 'O rascunho de origem não existe', 'missing_resource'))
            continue
        try:
            job = export(project_id, Draft.model_validate(raw), brand_outro=bool(variant['branding'].get('outro_enabled', True)), _variant_identity=expected)
        except Exception as error:
            store.raise_owned_read_error(error)
            capture_studio_exception(error, 'dispatch')
            code = studio_error_code(error)
            store.change(project_id, lambda data: reject(data, 'Esta tarefa de peça final não conseguiu iniciar; tente de novo. As peças já geradas foram mantidas.', code))
            continue
        store.remember_render_link(project_id, expected, job['job_id'])
        store.change(project_id, lambda data, expected=expected, job_id=job['job_id']: store.bind_render(data, expected, job_id))
    # Failed dispatch prerequisites or a backup-only batch have no render callback to
    # settle them. Queued/running/preparing variants still keep generation open.
    store.change(project_id, lambda data: _finish_generation(data) if data.get('generation') and data.get('output_variants') else None)
    sync_project_completion(project_id)


SCORE_KEY = '_auto_score'
AUTO_RENDER_LIMIT = 10  # clips rendered automatically per import; the rest wait for a click


def _eligible_for_platform(draft, strategy):
    duration = sum(scene['end'] - scene['start'] for scene in draft['scenes'])
    return strategy.duration_policy != 'long' or duration >= (strategy.min_recommended_duration_sec or 0)


def _score(clip):
    try:
        return float(clip.get('final_score'))
    except (TypeError, ValueError):
        return 0.0


def _automatic_drafts(base_drafts):
    """Ids of the drafts rendered without asking: the highest-scored AUTO_RENDER_LIMIT.

    A two-hour talk yields 30+ clips; rendering all of them up front cost most of the run, while
    people publish a handful. Drafts without a score (visual highlights, at most a few) all render.
    """
    scored = [draft for draft in base_drafts if SCORE_KEY in draft]
    if len(scored) <= AUTO_RENDER_LIMIT:
        return {draft['id'] for draft in base_drafts}
    top = sorted(scored, key=lambda draft: draft[SCORE_KEY], reverse=True)[:AUTO_RENDER_LIMIT]
    return {draft['id'] for draft in top} | {draft['id'] for draft in base_drafts if SCORE_KEY not in draft}


def produce_variant(project_id, variant_id):
    """Frame, package and render one on-demand variant."""
    def start(data):
        item = next((value for value in data.get('output_variants', []) if value['id'] == variant_id), None)
        if not item:
            raise FileNotFoundError('A versão da peça final não existe')
        if item.get('status') != 'on_demand':
            raise ValueError('Esta peça final já está sendo gerada ou já foi concluída')
        if not any(draft['id'] == item['draft_id'] for draft in data.get('drafts', [])):
            raise FileNotFoundError('O rascunho de origem não existe')
        return _start_preparing(data, item)
    # One locked change checks and claims: a double click must not prepare (and pay for) it twice.
    attempt_id = store.change(project_id, start)
    _submit_on_demand(project_id, variant_id, attempt_id)
    return next(item for item in store.read(project_id)['output_variants'] if item['id'] == variant_id)


def _start_preparing(data, item):
    # 'preparing' is invisible to _dispatch_pending_variants: rendering the stored draft now would
    # ship it without speaker framing, packaging or captions.
    item.update(status='preparing', instance=store.INSTANCE, preparation_run_id=uuid.uuid4().hex)
    for key in ('error', 'error_code', 'needs_prepare', 'render_job_id', 'render_attempt_id', 'render_dispatch_claimed', 'render_dispatch_instance'):
        item.pop(key, None)
    data['generation'].update(status='rendering')
    return item['preparation_run_id']


def _submit_on_demand(project_id, variant_id, attempt_id):
    try:
        executor.submit(_produce_on_demand, project_id, variant_id, _io_receipt=('preparation', variant_id, attempt_id))
    except Exception as error:
        store.remember_submission_rejection(project_id, ('preparation', variant_id, attempt_id), error_code=studio_error_code(error))
        capture_studio_exception(error, 'dispatch')
        message = 'Esta tarefa de peça final não conseguiu iniciar; tente de novo. As peças já geradas foram mantidas.'
        code = studio_error_code(error)
        def failed(data):
            target = next(item for item in data['output_variants'] if item['id'] == variant_id)
            target.update(status='failed', error=message, error_code=code, needs_prepare=True)
            target.pop('instance', None)
            _finish_generation(data)
        store.change(project_id, failed)
        sync_project_completion(project_id)
        raise ValueError(message) from None


@_tracked('production')
def _produce_on_demand(project_id, variant_id):
    try:
        state = store.read(project_id)
        variant = next(item for item in state['output_variants'] if item['id'] == variant_id)
        raw = next(item for item in state['drafts'] if item['id'] == variant['draft_id'])
        video = source(project_id)
        burned = _source_has_burned_subtitles(project_id, video)
        strategy_id = variant['strategy_id']
        value, framed = _apply_framing(project_id, raw, strategy_id, video, burned, {})
        value = _apply_packaging(project_id, value, strategy_id, burned, {})
        posts = _posts_for(project_id, [(strategy_id, value)], {})
        value = _audience_title(value, strategy_id, posts.get((_content_key(value), strategy_id)))
        draft = _apply_strategy(value, strategy_id, burned_subtitles=burned, layout=value.get('layout') if framed else None).model_dump()

        def ready(data):
            for index, item in enumerate(data['drafts']):
                if item['id'] == draft['id']:
                    data['drafts'][index] = {**draft, 'updated_at': store.now()}
            target = next(item for item in data['output_variants'] if item['id'] == variant_id)
            if framed:
                target['framing'] = framed
            if (_content_key(value), strategy_id) in posts:
                target['post'] = posts[_content_key(value), strategy_id]
            target['status'] = 'queued'  # ready: only now may a dispatcher render it
            target.pop('instance', None)
        store.change(project_id, ready)
        _dispatch_pending_variants(project_id)
    except Exception as error:
        store.raise_owned_read_error(error)  # noqa: BLE001 - the variant shows the failure and can be retried
        logger.warning('On-demand variant failed: %s', type(error).__name__)
        capture_studio_exception(error, 'production')
        code = studio_error_code(error)

        def failed(data):
            target = next(item for item in data['output_variants'] if item['id'] == variant_id)
            target.update(status='failed', error='Falha ao preparar esta peça final; tente de novo', error_code=code, needs_prepare=True)
            target.pop('instance', None)
            _finish_generation(data)
        try:
            store.change(project_id, failed)
            sync_project_completion(project_id)
        except Exception:  # noqa: BLE001 - never lose the failure silently inside the executor
            logger.exception('Could not record the on-demand failure')


def retry_variant(project_id, variant_id):
    state = store.read(project_id)
    variant = next((item for item in state.get('output_variants', []) if item['id'] == variant_id), None)
    if not variant:
        raise FileNotFoundError('A versão da peça final não existe')
    if variant.get('status') != 'failed':
        raise ValueError('Só versões de peça final que falharam podem ser repetidas')
    raw = next((item for item in state.get('drafts', []) if item['id'] == variant['draft_id']), None)
    if not raw:
        raise FileNotFoundError('O rascunho de origem não existe')
    if variant.get('needs_prepare'):
        def prepare(data):
            item = next(value for value in data['output_variants'] if value['id'] == variant_id)
            if item.get('status') != 'failed':
                raise ValueError('Só versões de peça final que falharam podem ser repetidas')
            return _start_preparing(data, item)
        attempt_id = store.change(project_id, prepare)
        _submit_on_demand(project_id, variant_id, attempt_id)
        return next(item for item in store.read(project_id)['output_variants'] if item['id'] == variant_id)
    def update(data):
        item = next(value for value in data['output_variants'] if value['id'] == variant_id)
        if item.get('status') != 'failed':
            raise ValueError('Só versões de peça final que falharam podem ser repetidas')
        item.update(status='queued', render_attempt_id=uuid.uuid4().hex)
        item.pop('render_dispatch_claimed', None)
        item.pop('render_dispatch_instance', None)
        item.pop('render_job_id', None)
        item.pop('error', None)
        item.pop('error_code', None)
        data['generation'].update(status='rendering')
        data['analysis'] = {'status': 'running', 'phase': 'rendering', 'message': 'Repetindo a versão da peça final', 'instance': store.INSTANCE, 'created_at': store.now()}
    store.change(project_id, update)
    _dispatch_pending_variants(project_id)
    return next(item for item in store.read(project_id)['output_variants'] if item['id'] == variant_id)


def _sync_variant_status(project_id, job_id, status, error=None):
    def update(data):
        changed = False
        for variant in data.get('output_variants', []):
            if variant.get('render_job_id') == job_id:
                variant.update(status=status)
                if error:
                    variant['error'] = error
                changed = True
        if changed:
            _finish_generation(data)
    store.change(project_id, update)
    sync_project_completion(project_id)


def _finish_generation(data):
    store.settle_generation(data)


def inspect_project(project_id, options, url=None, browser=None, *, producer=None, features=None):
    """Only ingest and screen. Expensive production requires an explicit confirmation."""
    # Reserve and dispatch together so another request cannot observe an
    # accepted task before submission succeeds. Preserve existing exports/plan.
    with store.lock:
        previous = store.read(project_id)
        if (previous.get('analysis') or {}).get('status') == 'running':
            raise ValueError('Já existe uma tarefa rodando; tente de novo em instantes')
        state = deepcopy(previous)
        state.setdefault('schema_version', 2)
        state['generation'] = {
            'requested_platforms': list(options.platforms),
            'branding': options.branding.model_dump(),
            'auto_start': options.auto_start,
            'portrait_style': options.portrait_style,
            'features': resolve_features(features),
            **({'producer': producer} if producer else {}),
            'status': 'screening',
            'created_at': store.now(),
        }
        state.setdefault('output_variants', [])
        state['analysis'] = {'status':'running', 'phase':'screening', 'run_id':uuid.uuid4().hex, 'message':'Preparando o material' if url else 'Avaliando rapidamente o tipo de produção', 'instance':store.INSTANCE, 'created_at':store.now()}
        store.write(project_id, state)
        if options.auto_start:
            _prepare_speaker_framing(options.platforms)
        try:
            executor.submit(_inspect, project_id, options, url, browser, _io_receipt=('analysis', state['analysis']['run_id'], 'screening'))
        except Exception as error:
            store.remember_submission_rejection(project_id, ('analysis', state['analysis']['run_id'], 'screening'), error_code=studio_error_code(error))
            logger.warning('Studio screening dispatch failed: %s', type(error).__name__)
            capture_studio_exception(error, 'dispatch')
            message = 'A importação não conseguiu iniciar; tente de novo. O material original e as peças já geradas foram mantidos.'
            if not previous.get('analysis'):
                previous['analysis'] = {'status':'failed', 'phase':'screening', 'error':message}
            # This unaccepted reservation must not enter terminal history before rollback.
            store.write(project_id, previous, _recover_previous=False)
            raise ValueError(message) from None

        return state['analysis']['run_id']


@_tracked('screening')
def _inspect(project_id, options, url, browser):
    started = monotonic()
    try:
        from backend.services.studio.planning import recommend
        mark_project(project_id, 'processing', awaiting_confirmation=False)
        if url:
            with llm_usage.timed('download'):
                download(project_id, url, browser)
        store.change(project_id, lambda data:(data['analysis'].pop('percent', None), data['analysis'].update(message='Avaliando rapidamente o tipo de produção')))
        plan = recommend(source(project_id), options)
        ensure_project_thumbnail(project_id)
        plan['id'] = uuid.uuid4().hex
        if options.auto_start:
            def start_automatic(data):
                run_id = (data.get('analysis') or {}).get('run_id')  # observers match the run across phases
                data.update(plan=plan, analysis={'status': 'running', 'phase': 'production', 'run_id': run_id, 'message': 'Produzindo a peça pronta para publicar', 'instance': store.INSTANCE, 'created_at': store.now()})
                data['generation'].update(status='production', started_at=store.now(), **_generation_route(plan))
                return run_id
            run_id = store.change(project_id, start_automatic)
            mark_project(project_id, 'processing', creative=plan['preferences'], awaiting_confirmation=False, import_staging=False)
            try:
                executor.submit(_auto_generate, project_id, plan, _io_receipt=('analysis', run_id, 'production'))
            except Exception as error:
                store.analysis_handoff_rejected(project_id, run_id)
                store.remember_submission_rejection(project_id, ('analysis', run_id, 'production'), error_code=studio_error_code(error))
                capture_studio_exception(error, 'dispatch')
                message = 'A produção automática não conseguiu iniciar; tente de novo. O material original foi mantido.'
                code = studio_error_code(error)
                def fail_automatic(data):
                    data['generation'].update(status='failed', error=message, error_code=code, finished_at=store.now(), failure_stage='dispatch', **_generation_route(plan))
                    data['analysis'] = {'status': 'failed', 'phase': 'production', 'error': message, 'error_code': code}
                store.change(project_id, fail_automatic)
                mark_project(project_id, 'failed')
            return
        def awaiting_confirmation(data):
            data.update(plan=plan, analysis={'status':'awaiting_confirmation', 'created_at':store.now(), 'duration_ms':round((monotonic() - started) * 1000)})
            if data.get('generation'):
                data['generation']['status'] = 'awaiting_confirmation'
        store.change(project_id, awaiting_confirmation)
        mark_project(project_id, 'pending', creative=plan['preferences'], awaiting_confirmation=True)
    except Exception as error:
        store.raise_owned_read_error(error)
        logger.warning('Studio screening failed: %s', type(error).__name__)
        capture_studio_exception(error, 'screening')
        failure = {'status':'failed','phase':'screening','error':str(error)[:700], 'error_code':studio_error_code(error), 'duration_ms':round((monotonic() - started) * 1000)}
        context = studio_failure_context(error, 'screening')
        try:
            def fail_screening(data):
                data['analysis'] = failure
                if (data.get('generation') or {}).get('auto_start'):
                    data['generation'].update(status='failed', error=failure['error'], error_code=failure['error_code'], finished_at=store.now(), **context)
            store.change(project_id, fail_screening)
            mark_project(project_id, 'failed')
        except FileNotFoundError:
            pass


def confirm_project(project_id, body):
    with store.lock:
        state = store.read(project_id)
        plan = state.get('plan') or {}
        if plan.get('id') != body.plan_id or (state.get('analysis') or {}).get('status') != 'awaiting_confirmation':
            raise store.ConflictError('O plano mudou ou a tarefa já começou; atualize a tela e confirme')
        route = body.analysis_mode or plan.get('recommended_analysis', 'subtitle')
        if route == 'visual' and body.goals == ['content']:
            raise ValueError('O corte de conteúdo usa análise por legenda; selecione o modo legenda e confirme')
        if route == 'visual':
            from backend.services.studio import intelligence
            if not intelligence.ready():
                raise ValueError('O modelo de visão não está disponível; configure e confirme de novo. O material original foi mantido.')
        if route == 'subtitle' or 'content' in body.goals:
            # Too short / no audio and no SRT: refuse before production spends any model call (RC156 #10/#11).
            from backend.pipeline import media_precheck
            video = source(project_id)
            blocked = media_precheck.subtitle_route_failure(
                video, srt_available=(video.parent / 'input.srt').is_file(), duration=plan.get('source_duration'))
            if blocked:
                raise ValueError(str(blocked))
        previous = deepcopy(state)
        plan['confirmed_analysis'] = route
        plan['selected_goals'] = body.goals
        plan['confirmed_at'] = store.now()
        # A confirmation override is persisted separately from the AI recommendation.
        plan['confirmed_preferences'] = {**plan['preferences'], 'goal':body.goals[0], **body.model_dump(exclude={'plan_id','goals','analysis_mode'}, exclude_none=True)}
        # Null explicitly restores automatic matching; omitted fields retain
        # import overrides for older clients. Match each output independently.
        aspect = body.aspect if 'aspect' in body.model_fields_set else plan.get('overrides', {}).get('aspect')
        plan['goal_preferences'] = {
            goal: {**plan['confirmed_preferences'], 'goal':goal,
                   'aspect':aspect or ('portrait' if goal == 'promo' else plan.get('aspect', 'original'))}
            for goal in body.goals
        }
        plan['confirmed_preferences'] = plan['goal_preferences'][body.goals[0]].copy()
        state['analysis'] = {'status':'running', 'phase':'production', 'run_id':uuid.uuid4().hex, 'message':'Iniciando a produção do conteúdo escolhido', 'instance':store.INSTANCE, 'created_at':store.now()}
        store.write(project_id, state)
        try:
            executor.submit(_produce_selected, project_id, plan, _io_receipt=('analysis', state['analysis']['run_id'], 'production'))
        except Exception as error:
            store.remember_submission_rejection(project_id, ('analysis', state['analysis']['run_id'], 'production'), error_code=studio_error_code(error))
            logger.warning('Studio production dispatch failed: %s', type(error).__name__)
            capture_studio_exception(error, 'dispatch')
            # No worker accepted this confirmation. Preserve the exact plan and
            # staging state so an explicit retry can use the same plan ID.
            store.write(project_id, previous, _recover_previous=False)
            raise ValueError('A produção não conseguiu iniciar; confirme de novo. O material original e as peças já geradas foram mantidos.') from None

        return state['analysis']['run_id']


@_tracked('production')
def _produce_selected(project_id, plan):
    from backend.services.studio import intelligence
    started = monotonic()
    succeeded_goals, failed_goals, reported = [], [], []
    result_count = 0
    labels = {'content':'Corte de conteúdo','highlight':'Destaque','promo':'Peça de divulgação'}
    errors = []
    error_codes = []
    diagnostics = []
    visual_error = None
    events = coverage = None
    subtitle_clips = None
    subtitle_error = None
    def stage(message):
        store.change(project_id, lambda data:data['analysis'].update(message=message))
    try:
        mark_project(project_id, 'processing', awaiting_confirmation=False, import_staging=False, creative=plan['confirmed_preferences'])
        video = source(project_id)
        instruction = plan.get('overrides', {}).get('instruction', '')
        for goal in plan['selected_goals']:
            try:
                prefs = Preferences.model_validate(plan.get('goal_preferences', {}).get(goal) or {**plan['confirmed_preferences'], 'goal':goal})
                stage('Produção' + labels[goal])
                if goal == 'content' or plan.get('confirmed_analysis', 'subtitle') == 'subtitle':
                    if subtitle_error is not None:
                        raise subtitle_error
                    if subtitle_clips is None:
                        try:
                            subtitle_clips = run_content(project_id, video)
                        except Exception as error:
                            subtitle_error = error
                            raise
                    if goal == 'highlight':
                        from backend.services.studio.subtitle_highlights import make_highlights
                        drafts = make_highlights(subtitle_clips, prefs, intelligence._probe(video).get('duration'))
                        store.change(project_id, lambda data:data['drafts'].extend({**d,'updated_at':store.now()} for d in drafts))
                    if goal == 'promo':
                        from backend.services.studio.subtitle_promo import make_promos
                        drafts = make_promos(project_id, subtitle_clips, prefs, intelligence._probe(video).get('duration'), instruction)
                        store.change(project_id, lambda data:data['drafts'].extend({**d,'updated_at':store.now()} for d in drafts))
                    mark_project(project_id, 'processing')
                    result_count += len(subtitle_clips) if goal == 'content' else len(drafts)
                    succeeded_goals.append(goal)
                    continue
                if plan.get('confirmed_analysis', 'subtitle') != 'visual':
                    raise ValueError('Esta confirmação não autorizou análise visual; escolha de novo a forma de processar')
                if not intelligence.ready():
                    raise ValueError('Configure antes um modelo de compreensão visual nos ajustes')
                if visual_error is not None:
                    raise visual_error
                if events is None:
                    try:
                        events, coverage = analyze(video, prefs, stage, instruction) if instruction else analyze(video, prefs, stage)
                    except Exception as error:
                        # Both visual goals share one attempt, including its failure.
                        # A second paid scan requires a new explicit user confirmation.
                        visual_error = error
                        raise
                    store.change(project_id, lambda data:data.update(events=[e.model_dump() for e in events]))
                drafts = make_drafts(events, prefs, instruction, source_duration=intelligence._probe(video).get('duration'))
                if (video.parent / 'input.srt').exists():
                    for draft in drafts:
                        draft['subtitles'] = True
                store.change(project_id, lambda data:data['drafts'].extend({**d,'updated_at':store.now()} for d in drafts))
                result_count += len(drafts)
                succeeded_goals.append(goal)
            except Exception as error:
                store.raise_owned_read_error(error)
                failed_goals.append(goal)
                error_codes.append(studio_error_code(error))
                if all(error is not previous_error for previous_error in reported):
                    capture_studio_exception(error, 'production', analysis_mode=plan.get('confirmed_analysis', 'subtitle'), goal=goal)
                    reported.append(error)
                errors.append(labels[goal] + '：' + str(error)[:500])
                if isinstance(error, VisionRequestError):
                    diagnostics.append({'goal':goal, **error.diagnostics()})
        result = {'status':'failed' if errors else 'completed', 'created_at':store.now(),
                  'outcome':'partial' if errors and succeeded_goals else 'failed' if errors else 'completed',
                  'requested_goals':plan['selected_goals'], 'succeeded_goals':succeeded_goals,
                  'failed_goals':failed_goals, 'result_count':result_count,
                  'duration_ms':round((monotonic() - started) * 1000)}
        if coverage:
            result['coverage'] = coverage
        if diagnostics:
            result['diagnostics'] = diagnostics
        if errors:
            result['error'] = '；'.join(errors)
            result['error_code'] = error_codes[0] if len(set(error_codes)) == 1 else 'multiple'
        store.change(project_id, lambda data:data.update(analysis=result))
        mark_project(project_id, 'failed' if errors else 'completed', studio_draft_count=len(store.read(project_id)['drafts']))
    except Exception as error:
        store.raise_owned_read_error(error)
        capture_studio_exception(error, 'production', analysis_mode=plan.get('confirmed_analysis', 'subtitle'))
        failure = {'status':'failed','error':str(error)[:700], 'error_code':studio_error_code(error),
            'outcome':'partial' if succeeded_goals else 'failed', 'requested_goals':plan['selected_goals'],
            'succeeded_goals':succeeded_goals, 'failed_goals':[g for g in plan['selected_goals'] if g not in succeeded_goals],
            'result_count':result_count, 'duration_ms':round((monotonic() - started) * 1000)}
        try:
            store.change(project_id, lambda data:data.update(analysis=failure))
            mark_project(project_id,'failed')
        except FileNotFoundError:
            pass
