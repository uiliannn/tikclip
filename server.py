"""Servidor Web do TikClip (Edição SupoClip).

Roda em pasta própria (tikclip-web) sem alterar nenhum arquivo do TikClip Studio original,
reaproveitando:
- Contas do TikTok já logadas em %APPDATA%\\TikClip
- Projetos, cortes e banco de dados SQLite existentes
- Motores locais de Whisper, FFmpeg, yt-dlp e Playwright
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path


HERE = Path(__file__).resolve().parent
WEB_DIR = HERE / 'web'

# Pasta do TikClip Studio (opcional). Antes era um caminho absoluto fixo, que
# só existe na máquina do autor. Ordem: env var > pasta irmã do repositório.
STUDIO_ROOT = Path(
    os.environ.get('TIKCLIP_STUDIO_ROOT') or (HERE.parent / 'tikclip-studio')
)
STUDIO_APP = STUDIO_ROOT / 'app'
STUDIO_RUNTIME = STUDIO_APP / 'runtime'

DATA_DIR = Path(os.environ.get('APPDATA') or Path.home()) / 'TikClip'


def _proteger_logging_utf8() -> None:
    """Evita erro de UnicodeEncodeError no terminal Windows quando logs contêm caracteres não-ASCII."""
    import logging
    logging.raiseExceptions = False

    orig_emit = logging.StreamHandler.emit

    def _safe_emit(self, record):
        try:
            orig_emit(self, record)
        except Exception:
            try:
                msg = self.format(record)
                stream = getattr(self, 'stream', None)
                if stream and hasattr(stream, 'write'):
                    stream.write(msg.encode('ascii', errors='replace').decode('ascii') + getattr(self, 'terminator', '\n'))
                    if hasattr(self, 'flush'):
                        self.flush()
            except Exception:
                logger.debug("excecao ignorada", exc_info=True)

    logging.StreamHandler.emit = _safe_emit


def _sem_janelas_de_console() -> None:
    """Impede que chamadas internas ao ffmpeg/ffprobe/yt-dlp abram janelas pretas."""
    if os.name != 'nt':
        return
    sem_janela = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
    original = subprocess.Popen.__init__
    if getattr(original, '_ac_sem_janela', False):
        return

    def iniciar(self, *args, **kwargs):
        kwargs['creationflags'] = int(kwargs.get('creationflags') or 0) | sem_janela
        return original(self, *args, **kwargs)

    iniciar._ac_sem_janela = True
    subprocess.Popen.__init__ = iniciar


def _resolver_ffmpeg() -> None:
    candidatos = [
        STUDIO_ROOT / 'runtime' / 'ffmpeg',
        STUDIO_ROOT / 'ffmpeg',
        Path(os.environ.get('LOCALAPPDATA', '')) / 'TikClip Desktop' / 'resources' / 'ffmpeg',
    ]
    for pasta in candidatos:
        ffmpeg = pasta / 'ffmpeg.exe'
        ffprobe = pasta / 'ffprobe.exe'
        if ffmpeg.is_file() and ffprobe.is_file():
            os.environ.setdefault('TIKCLIP_FFMPEG_PATH', str(ffmpeg))
            os.environ.setdefault('TIKCLIP_FFPROBE_PATH', str(ffprobe))
            return


def _aprimorar_llm_manager() -> None:
    """Garante que o seletor Gemini vs DeepSeek funcione tanto via SDK nativo quanto via API OpenAI."""
    try:
        from backend.core import llm_manager as lm
        from backend.core.llm_providers import GeminiProvider

        orig_init = lm.LLMManager._initialize_provider

        def _novo_init(self):
            conn_prov = self.settings.get('connection_provider')
            if conn_prov == 'gemini':
                api_key = (self.settings.get('openai_api_key') or self.settings.get('gemini_api_key') or '').strip()
                model_name = self.settings.get('model_name') or 'gemini-2.5-flash'
                if api_key:
                    try:
                        self.current_provider = GeminiProvider(api_key=api_key, model_name=model_name)
                        return
                    except Exception:
                        logger.debug("excecao ignorada", exc_info=True)
            return orig_init(self)

        lm.LLMManager._initialize_provider = _novo_init
    except Exception as exc:
        print(f'[aviso] patch LLM opcional: {exc}')


def _aprimorar_publicador() -> None:
    """Adiciona suporte para pular o intervalo de espera imediatamente durante publicações em lote."""
    try:
        import api_ext
        import time

        def _custom_publish_loop(pid, account, vids, interval, render_wait):
            total = len(vids)
            published = 0
            failed = 0
            with api_ext._PUB_LOCK:
                api_ext._PUB.update(
                    running=True,
                    stop=False,
                    skip_wait=False,
                    index=0,
                    total=total,
                    message='Iniciando...',
                    account=account,
                    project_id=pid,
                )
            api_ext._pub_log(f'iniciando fila de {total} corte(s) na conta "{account}"')

            for idx, vid in enumerate(vids, start=1):
                with api_ext._PUB_LOCK:
                    if api_ext._PUB['stop']:
                        api_ext._pub_log('Publicação interrompida pelo usuário.')
                        break
                    api_ext._PUB['index'] = idx
                    api_ext._PUB['message'] = f'Publicando {idx}/{total}'

                clip = api_ext._variant_payload(pid, vid)
                if not clip or not clip.get('has_video'):
                    failed += 1
                    continue
                if clip.get('status') == 'publicado':
                    continue

                state = api_ext._read_studio(pid)
                jobs = {j['job_id']: j for j in state.get('jobs', []) if j.get('job_id')}
                variant = next((v for v in state.get('output_variants', []) if v.get('id') == vid), {})
                job = jobs.get(variant.get('render_job_id'))
                video = api_ext._video_path(pid, job)
                if not video:
                    failed += 1
                    continue

                cover = api_ext._cover_path(pid, job.get('job_id') if job else None, vid)
                api_ext._pub_log(f'[{idx}/{total}] enviando "{clip["title"]}"...')

                def progress(msg: str, _idx=idx, _t=total):
                    if msg:
                        api_ext._pub_log(f'[{_idx}/{_t}] {msg}')

                try:
                    result = api_ext.ttl.publish_video(
                        video, clip['title'], clip['description'], clip['tags'], cover, account=account, progress=progress
                    )
                    status = (result or {}).get('status')
                    try:
                        api_ext._registrar_publicacao(pid, clip, job, variant, video, result, account)
                    except Exception:
                        logger.debug("excecao ignorada", exc_info=True)
                    if status == 'completed':
                        published += 1
                        api_ext._pub_log(f'[{idx}/{total}] publicado com sucesso ✔')
                    else:
                        api_ext._pub_log(f'[{idx}/{total}] enviado ({status}) — confira o rascunho no TikTok')
                    # Limpa blobs temporários do IndexedDB para não inflar o disco
                    try:
                        p_dir = Path(api_ext.ttl.profile_dir(account))
                        idb = p_dir / 'Default' / 'IndexedDB'
                        if idb.is_dir():
                            import shutil
                            for b in idb.glob('*.indexeddb.blob'):
                                shutil.rmtree(b, ignore_errors=True)
                    except Exception:
                        logger.debug("excecao ignorada", exc_info=True)
                except Exception as exc:
                    failed += 1
                    api_ext._pub_log(f'[{idx}/{total}] falhou: {exc}')

                if idx >= total or not interval:
                    continue

                wait_s = int(interval * 60)
                api_ext._pub_log(f'aguardando {interval} min antes do próximo')
                if render_wait:
                    try:
                        api_ext.ext_render_pending({'project_id': pid})
                    except Exception:
                        logger.debug("excecao ignorada", exc_info=True)

                end = time.time() + wait_s
                while time.time() < end:
                    with api_ext._PUB_LOCK:
                        if api_ext._PUB['stop'] or api_ext._PUB.get('skip_wait'):
                            api_ext._PUB['skip_wait'] = False
                            break
                    time.sleep(1)

            with api_ext._PUB_LOCK:
                api_ext._PUB.update(running=False, message=f'Concluído: {published} publicados, {failed} falhas')
            return published, failed

        api_ext._publish_loop = _custom_publish_loop
    except Exception as exc:
        print(f'[aviso] patch publicador: {exc}')


def _aprimorar_pydantic_cenas() -> None:
    """Evita erro de validação do Pydantic quando a IA devolve tipos de eventos em português (ex: 'conteudo principal')."""
    try:
        from backend.services.studio import intelligence
        orig_validate = intelligence.HighlightCandidate.model_validate

        @classmethod
        def _safe_validate(cls, obj, *args, **kwargs):
            if isinstance(obj, dict):
                obj = dict(obj)
                evt = str(obj.get('event_type') or '').lower().strip()
                valid = {'gameplay', 'menu', 'reward_screen', 'loading', 'other', 'unknown'}
                if evt not in valid:
                    if any(k in evt for k in ('menu', 'loading', 'carregando', 'premio', 'recompensa')):
                        obj['event_type'] = 'menu'
                    elif any(k in evt for k in ('jogo', 'game', 'gameplay', 'jogando', 'play', 'conteudo', 'principal')):
                        obj['event_type'] = 'gameplay'
                    else:
                        obj['event_type'] = 'other'
                if 'watch_score' in obj and obj['watch_score'] is not None:
                    try:
                        obj['watch_score'] = min(100, max(0, int(float(obj['watch_score']))))
                    except Exception:
                        obj['watch_score'] = 50
                known = {'id', 'label', 'start', 'end', 'evidence', 'crop_x', 'crop_track', 'framing_source', 'framing_adjusted', 'event_type', 'watch_score', 'selection_reason'}
                for k in list(obj.keys()):
                    if k not in known:
                        obj.pop(k, None)
            return orig_validate(obj, *args, **kwargs)

        intelligence.HighlightCandidate.model_validate = _safe_validate
    except Exception as exc:
        print(f'[aviso] patch pydantic cenas: {exc}')


def _aprimorar_hashtags_projeto() -> None:
    """Garante leitura correta de hashtags do projeto mesmo quando salvas como lista JSON direta."""
    try:
        import api_ext
        import json

        def _safe_ler_hashtags_projeto(pid: str) -> list[str]:
            try:
                p = api_ext._hashtags_path(pid)
                if not p.is_file():
                    return []
                dados = json.loads(p.read_text(encoding='utf-8'))
                if isinstance(dados, list):
                    return [str(t).strip().lstrip('#') for t in dados if str(t).strip()][:15]
                if isinstance(dados, dict):
                    tags = dados.get('tags')
                    if isinstance(tags, list):
                        return [str(t).strip().lstrip('#') for t in tags if str(t).strip()][:15]
            except Exception:
                logger.debug("excecao ignorada", exc_info=True)
            return []

        api_ext.ler_hashtags_projeto = _safe_ler_hashtags_projeto
    except Exception as exc:
        print(f'[aviso] patch hashtags projeto: {exc}')


def escolher_porta(preferida: int = 8585) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(('127.0.0.1', preferida)) != 0:
            return preferida
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return int(s.getsockname()[1])


def preparar_ambiente() -> int:
    os.environ['TIKCLIP_DATA_DIR'] = str(DATA_DIR)
    os.environ['TIKCLIP_APP_DIR'] = str(DATA_DIR)
    os.environ['TIKCLIP_MODE'] = 'desktop'
    os.environ['DATABASE_URL'] = 'sqlite:///' + str(DATA_DIR / 'tikclip.db')
    os.environ['PYTHONUTF8'] = '1'
    os.environ['PYTHONIOENCODING'] = 'utf-8:replace'

    try:
        if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        logger.debug("excecao ignorada", exc_info=True)

    _resolver_ffmpeg()

    # O repositório tem prioridade sobre a pasta externa do Studio. Antes, o
    # insert(0) sucessivo colocava STUDIO_ROOT na frente de HERE no sys.path,
    # então `import backend` resolvia para a cópia de tikclip-studio — o código
    # deste repositório (inclusive as correções) nunca era executado.
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    for caminho in (str(STUDIO_APP), str(STUDIO_RUNTIME), str(STUDIO_ROOT)):
        if caminho not in sys.path and Path(caminho).is_dir():
            sys.path.append(caminho)

    _aprimorar_llm_manager()
    _aprimorar_publicador()
    _aprimorar_pydantic_cenas()
    _aprimorar_hashtags_projeto()
    porta_env = os.getenv('TIKCLIP_WEB_PORT')
    return int(porta_env) if porta_env else escolher_porta(8585)


def montar_app(porta: int):
    os.environ['TIKCLIP_ALLOWED_ORIGINS'] = f'http://localhost:{porta},http://127.0.0.1:{porta}'
    try:
        from backend.core import local_origin_guard as log_guard
        ip_local = socket.gethostbyname(socket.gethostname())
        log_guard.LOCAL_HOSTNAMES.add(ip_local)
    except Exception:
        logger.debug("excecao ignorada", exc_info=True)

    from fastapi.responses import RedirectResponse
    from fastapi.staticfiles import StaticFiles

    from backend.app_factory import create_app
    import api_ext
    import supo_api

    api_ext.SELF_PORT = porta
    app = create_app('desktop')
    app.include_router(api_ext.router, prefix='/api/v1/ext')
    app.include_router(supo_api.router, prefix='/api/v1/supo')

    try:
        import channel_monitor
        channel_monitor.start_channel_monitor_thread(server_port=porta)
    except Exception as exc:
        print(f'[aviso] monitor canais: {exc}')

    @app.get('/')
    def root_redirect():
        return RedirectResponse(url='/ui/')

    app.mount('/ui', StaticFiles(directory=str(WEB_DIR), html=True), name='ui')

    # Mesma resolução usada pelo video_translator: env > pasta legada > dados.
    _legado_downloads = Path.home() / '.gemini' / 'antigravity' / 'scratch' / 'tiktok-downloader-web' / 'downloads'
    tiktok_downloads_dir = Path(
        os.environ.get('TIKCLIP_DOWNLOADS_DIR')
        or (_legado_downloads if _legado_downloads.is_dir() else DATA_DIR / 'tiktok-downloads')
    )
    if tiktok_downloads_dir.is_dir():
        app.mount('/media/tiktok-downloads', StaticFiles(directory=str(tiktok_downloads_dir)), name='tiktok_downloads')

    return app


def aguardar_servidor(porta: int, limite_s: float = 45.0) -> bool:
    url = f'http://127.0.0.1:{porta}/api/v1/ext/status'
    fim = time.time() + limite_s
    while time.time() < fim:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.35)
    return False


def main() -> int:
    # Precisa vir antes de qualquer log: sem isso, mensagem com acento ou
    # caractere chinês estoura UnicodeEncodeError no console cp1252 do Windows.
    _proteger_logging_utf8()
    _sem_janelas_de_console()
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        logger.debug("excecao ignorada", exc_info=True)

    porta = preparar_ambiente()
    url = f'http://localhost:{porta}/ui/'

    print('=' * 62)
    print('  TikClip Web (Design SupoClip) — Iniciando Servidor')
    print('=' * 62)
    print(f'  Dados & Contas TikTok : {DATA_DIR}')
    print(f'  FFmpeg                : {os.environ.get("TIKCLIP_FFMPEG_PATH", "PATH")}')
    print(f'  Endereço no Navegador : {url}')
    print('=' * 62)

    app = montar_app(porta)

    def _abrir_navegador():
        if aguardar_servidor(porta):
            print(f'\n[OK] Servidor online! Abrindo no navegador: {url}\n')
            if not os.getenv('TIKCLIP_NO_BROWSER'):
                webbrowser.open(url)

    threading.Thread(target=_abrir_navegador, daemon=True).start()

    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=porta, log_level='warning')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
