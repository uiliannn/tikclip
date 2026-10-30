"""Servidor Web do TikClip (Edição SupoClip).

Projeto completo para geração inteligente de cortes, monitoramento autônomo
de canais do YouTube e publicação automatizada no TikTok Studio.

Suporta:
- Dual AI: Google Gemini (2.5/3.5) e DeepSeek (V3/R1) com seletor instantâneo
- Monitoramento de canais do YouTube com corte e postagem automáticos
- Biblioteca de downloads do TikTok com legendas automáticas (.info.txt) e geração de capas
- Publicador TikTok multi-contas isoladas via Playwright
- Servidor MCP para integração com Claude, Cursor e agentes externos
"""
from __future__ import annotations

import os
import shutil
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

# Diretório de dados configurável (se TIKCLIP_DATA_DIR não definido, usa %APPDATA%\TikClip ou ~/.tikclip)
DATA_DIR = Path(
    os.environ.get('TIKCLIP_DATA_DIR')
    or (Path(os.environ.get('APPDATA') or Path.home()) / 'TikClip')
)
DATA_DIR.mkdir(parents=True, exist_ok=True)


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
                pass

    logging.StreamHandler.emit = _safe_emit


def _sem_janelas_de_console() -> None:
    """Impede que chamadas internas ao ffmpeg/ffprobe/yt-dlp abram janelas pretas no Windows."""
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
    """Localiza executáveis do FFmpeg e FFprobe dinamicamente no sistema ou pasta local."""
    env_ffmpeg = os.environ.get('TIKCLIP_FFMPEG_PATH')
    env_ffprobe = os.environ.get('TIKCLIP_FFPROBE_PATH')
    if env_ffmpeg and env_ffprobe and Path(env_ffmpeg).is_file() and Path(env_ffprobe).is_file():
        return

    candidatos = [
        HERE / 'runtime' / 'ffmpeg',
        HERE / 'ffmpeg',
        Path(os.environ.get('LOCALAPPDATA', '')) / 'TikClip Desktop' / 'resources' / 'ffmpeg',
    ]
    for pasta in candidatos:
        ffmpeg = pasta / 'ffmpeg.exe'
        ffprobe = pasta / 'ffprobe.exe'
        if ffmpeg.is_file() and ffprobe.is_file():
            os.environ.setdefault('TIKCLIP_FFMPEG_PATH', str(ffmpeg))
            os.environ.setdefault('TIKCLIP_FFPROBE_PATH', str(ffprobe))
            return

    # Procura no PATH do sistema
    which_ffmpeg = shutil.which('ffmpeg')
    which_ffprobe = shutil.which('ffprobe')
    if which_ffmpeg and which_ffprobe:
        os.environ.setdefault('TIKCLIP_FFMPEG_PATH', which_ffmpeg)
        os.environ.setdefault('TIKCLIP_FFPROBE_PATH', which_ffprobe)


def _aprimorar_llm_manager() -> None:
    """Garante que o seletor Gemini vs DeepSeek funcione tanto via SDK nativo quanto via API OpenAI."""
    try:
        from backend.core import llm_manager as lm
        from backend.core.llm_providers import GeminiProvider, LLMProviderFactory, ProviderType

        orig_init = lm.LLMManager._initialize_provider

        def _novo_init(self):
            conn_prov = self.settings.get('connection_provider')
            if conn_prov == 'gemini':
                api_key = (self.settings.get('openai_api_key') or self.settings.get('gemini_api_key') or '').strip()
                model_name = self.settings.get('model_name') or 'gemini-2.5-flash'
                if api_key:
                    try:
                        self.provider = GeminiProvider(api_key=api_key, model_name=model_name)
                        return
                    except Exception:
                        pass
            return orig_init(self)

        lm.LLMManager._initialize_provider = _novo_init
    except Exception as exc:
        print(f'[aviso] patch LLM opcional: {exc}')


def _aprimorar_publicador() -> None:
    """Aprimora o publicador em lote do TikTok com timeouts generosos e progresso em tempo real."""
    try:
        import api_ext

        def _custom_publish_loop(pid: str, variant_ids: list[str], account: str, interval: int, render_wait: bool):
            published = 0
            failed = 0
            total = len(variant_ids)

            with api_ext._PUB_LOCK:
                api_ext._PUB.update(running=True, stop=False, total=total, index=0, project_id=pid)

            for idx, vid in enumerate(variant_ids, 1):
                with api_ext._PUB_LOCK:
                    if api_ext._PUB['stop']:
                        api_ext._pub_log('Publicação interrompida pelo usuário.')
                        break
                    api_ext._PUB['index'] = idx

                clip = api_ext._variant_payload(pid, vid)
                if not clip or not clip.get('has_video'):
                    failed += 1
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
                api_ext._pub_log(f'[{idx}/{total}] Enviando "{clip["title"]}"...')

                def progress(msg: str, _idx=idx, _t=total):
                    if msg:
                        api_ext._pub_log(f'[{_idx}/{_t}] {msg}')

                try:
                    result = api_ext.ttl.publish_video(
                        video, clip['title'], clip['description'], clip['tags'],
                        cover_path=cover, account=account, progress=progress
                    )
                    status = (result or {}).get('status')
                    try:
                        api_ext._registrar_publicacao(pid, clip, job, variant, video, result, account)
                    except Exception:
                        pass
                    if status == 'completed':
                        published += 1
                        api_ext._pub_log(f'[{idx}/{total}] Publicado com sucesso ✔')
                    else:
                        api_ext._pub_log(f'[{idx}/{total}] Enviado ({status}) — confira o rascunho no TikTok')
                except Exception as exc:
                    failed += 1
                    api_ext._pub_log(f'[{idx}/{total}] Falhou: {exc}')

                if idx >= total or not interval:
                    continue

                wait_s = int(interval * 60)
                api_ext._pub_log(f'Aguardando {interval} min antes do próximo corte...')
                if render_wait:
                    try:
                        api_ext.ext_render_pending({'project_id': pid})
                    except Exception:
                        pass

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
        pass

    _resolver_ffmpeg()

    for caminho in (str(HERE), str(HERE / 'backend')):
        if caminho not in sys.path:
            sys.path.insert(0, caminho)

    _aprimorar_llm_manager()
    _aprimorar_publicador()
    _aprimorar_pydantic_cenas()
    porta_env = os.getenv('TIKCLIP_WEB_PORT') or os.getenv('PORT')
    return int(porta_env) if porta_env else escolher_porta(8585)


def montar_app(porta: int):
    os.environ['TIKCLIP_ALLOWED_ORIGINS'] = f'http://localhost:{porta},http://127.0.0.1:{porta}'
    try:
        from backend.core import local_origin_guard as log_guard
        ip_local = socket.gethostbyname(socket.gethostname())
        log_guard.LOCAL_HOSTNAMES.add(ip_local)
    except Exception:
        pass

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

    # Diretório configurável para biblioteca de downloads do TikTok
    raw_tt_dir = os.environ.get('TIKTOK_DOWNLOADS_DIR')
    if raw_tt_dir:
        tiktok_downloads_dir = Path(raw_tt_dir)
    else:
        # Fallback inteligente: pasta local downloads/tiktok
        tiktok_downloads_dir = HERE / 'downloads' / 'tiktok'

    tiktok_downloads_dir.mkdir(parents=True, exist_ok=True)
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
    _sem_janelas_de_console()
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    porta = preparar_ambiente()
    url = f'http://localhost:{porta}/ui/'

    print('=' * 62)
    print('  TikClip Web (Design SupoClip) — Servidor Portátil')
    print('=' * 62)
    print(f'  Dados & Contas        : {DATA_DIR}')
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
