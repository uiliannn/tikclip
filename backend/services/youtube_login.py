"""Login no YouTube dentro do próprio TikClip, para exportar os cookies da sessão.

Por que existe: o download exige sessão logada quando o IP está marcado. Antes isso
dependia do navegador do YT Saver; aqui o app abre um navegador **isolado** (sem
perfil compartilhado, sem tocar no Edge/Chrome do usuário), a pessoa faz o login uma
vez, e os cookies são gravados em `%APPDATA%\\TikClip\\cookies-youtube.txt`.

Isolamento importa: um perfil persistente com `channel='msedge'` reaproveitou a
sessão do Edge real (52 cookies do Google sem ninguém logar). Isso é inaceitável
por privacidade, então usamos `launch()` — contexto novo, sem perfil em disco.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from backend.services.youtube_cookies import para_netscape

DESTINO = Path(os.environ.get('APPDATA') or Path.home()) / 'TikClip' / 'cookies-youtube.txt'
INICIO = 'https://www.youtube.com/'
SESSAO = ('SAPISID', '__Secure-3PSID', '__Secure-1PSID', 'LOGIN_INFO')

_ESTADO = {
    'rodando': False, 'mensagem': 'Nenhum login em andamento.', 'ok': False,
    'total': 0, 'sessao': [], 'quando': None, 'erro': None,
}
_LOCK = threading.Lock()
_PARAR = threading.Event()


def _atualizar(**campos):
    with _LOCK:
        _ESTADO.update(campos)


def status() -> dict:
    with _LOCK:
        estado = dict(_ESTADO)
    estado['tem_arquivo'] = DESTINO.is_file()
    if DESTINO.is_file():
        linhas = [l for l in DESTINO.read_text(encoding='utf-8', errors='replace').splitlines()
                  if l and not l.startswith('#')]
        estado['total_arquivo'] = len(linhas)
        estado['atualizado_em'] = DESTINO.stat().st_mtime
    return estado


def _para_netscape(cookies: list[dict]) -> str:
    normalizados = [{
        'domain': c.get('domain') or '',
        'path': c.get('path') or '/',
        'secure': bool(c.get('secure')),
        'http_only': bool(c.get('httpOnly')),
        'expires': int(c.get('expires') or 0) if (c.get('expires') or 0) > 0 else 0,
        'name': c.get('name') or '',
        'value': c.get('value') or '',
    } for c in cookies]
    return para_netscape(normalizados)


def _sessao_google(cookies: list[dict]) -> list[str]:
    nomes = {c.get('name') for c in cookies}
    return [n for n in SESSAO if n in nomes]


def _rodar(limite_min: int) -> None:
    from backend.services import tiktok_local_publisher as ttl

    ttl._ensure_runtime()
    from playwright.sync_api import sync_playwright

    _atualizar(rodando=True, ok=False, erro=None, quando=None,
               mensagem='Abrindo o navegador. Entre na sua conta do Google.')
    _PARAR.clear()

    navegador_play = None
    navegador = None
    contexto = None
    try:
        navegador_play = sync_playwright().start()
        # launch() = contexto limpo, sem perfil: não usa nem altera o navegador do usuário
        navegador = navegador_play.chromium.launch(
            channel=ttl._channel(), headless=False,
            args=['--disable-blink-features=AutomationControlled',
                  '--no-first-run', '--no-default-browser-check'],
        )
        contexto = navegador.new_context(viewport={'width': 1280, 'height': 800},
                                         locale='pt-BR')
        pagina = contexto.new_page()
        try:
            pagina.goto(INICIO, wait_until='domcontentloaded', timeout=60000)
        except Exception:  # noqa: BLE001 - rede lenta: a pessoa navega sozinha
            pass

        limite = time.time() + limite_min * 60
        while time.time() < limite:
            if _PARAR.is_set():
                _atualizar(rodando=False, ok=False, mensagem='Login cancelado.')
                return
            time.sleep(2)
            try:
                if not contexto.pages:
                    _atualizar(rodando=False, ok=False,
                               mensagem='A janela foi fechada antes do login.')
                    return
                cookies = contexto.cookies()
            except Exception:  # noqa: BLE001
                _atualizar(rodando=False, ok=False,
                           mensagem='A janela foi fechada antes do login.')
                return
            sessao = _sessao_google(cookies)
            if len(sessao) >= 2:
                texto = _para_netscape(cookies)
                DESTINO.parent.mkdir(parents=True, exist_ok=True)
                DESTINO.write_text(texto, encoding='utf-8')
                total = len([l for l in texto.splitlines() if l and not l.startswith('#')])
                _atualizar(rodando=False, ok=True, total=total, sessao=sessao,
                           quando=time.time(),
                           mensagem=f'Login concluído: {total} cookies salvos '
                                    f'(sessão Google detectada).')
                time.sleep(1.5)
                return
            restante = max(0, int((limite - time.time()) / 60))
            _atualizar(mensagem=f'Aguardando o login… (até {restante} min)')
        _atualizar(rodando=False, ok=False,
                   mensagem='Tempo esgotado sem detectar a sessão do Google. '
                            'Tente de novo e conclua o login na janela.')
    except Exception as erro:  # noqa: BLE001
        _atualizar(rodando=False, ok=False, erro=str(erro),
                   mensagem=f'Não consegui abrir o navegador: {erro}')
    finally:
        for recurso in (contexto, navegador):
            try:
                if recurso is not None:
                    recurso.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            if navegador_play is not None:
                navegador_play.stop()
        except Exception:  # noqa: BLE001
            pass


def iniciar_login(limite_min: int = 5) -> dict:
    """Abre o navegador isolado em segundo plano. Devolve o estado inicial."""
    with _LOCK:
        if _ESTADO['rodando']:
            return {'iniciado': False, 'mensagem': 'Já existe um login em andamento.'}
        _ESTADO.update(rodando=True, ok=False, erro=None, quando=None,
                       mensagem='Iniciando…')
    threading.Thread(target=_rodar, args=(limite_min,), daemon=True,
                     name='youtube-login').start()
    return {'iniciado': True,
            'mensagem': 'Navegador abrindo. Entre na conta do Google na janela que abrir.'}


def cancelar_login() -> dict:
    _PARAR.set()
    return {'cancelando': True, 'mensagem': 'Fechando o navegador do login.'}


if __name__ == '__main__':
    print('destino:', DESTINO)
    print('status :', status())
