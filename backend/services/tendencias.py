"""Parser do texto da página de inspiração (o grid não usa <tr>).

A página, lida de cima para baixo, sai assim:

    Tópicos | Visualizações | Tendência | Publicações relacionadas
    1 | Desire Doué Puts PSG Ahead Against Barcelona | 267M | 15M | <legenda> | 5.4M | ...
    2 | Blueface Breaks Up With Chrisean | 242M | ...

Então: linha que é um número pequeno = posição; a seguinte = título; a seguinte = views.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

CACHE = Path(os.environ.get('APPDATA') or Path.home()) / 'TikClip' / 'tendencias.json'
URL = 'https://www.tiktok.com/tiktokstudio/inspiration?lang=pt-BR'

_ESTADO: dict = {'rodando': False, 'mensagem': 'Nunca atualizado.', 'quando': None,
                 'total': 0, 'erro': None}
_LOCK = threading.Lock()

_VIEWS = re.compile(r'^([\d.,]+)\s*([KMB])$', re.IGNORECASE)


def _atualizar(**campos):
    with _LOCK:
        _ESTADO.update(campos)


def status() -> dict:
    with _LOCK:
        estado = dict(_ESTADO)
    if CACHE.is_file():
        try:
            dados = json.loads(CACHE.read_text(encoding='utf-8'))
            estado['quando'] = dados.get('quando')
            estado['total'] = len(dados.get('itens') or [])
        except (OSError, ValueError):
            pass
    return estado


def numero(texto: str) -> int | None:
    """'267M' -> 267000000 ; '5,4M' -> 5400000 ; '985K' -> 985000"""
    achado = _VIEWS.match(str(texto).strip())
    if not achado:
        return None
    cru, sufixo = achado.group(1), achado.group(2).upper()
    # pt-BR usa vírgula decimal; ponto pode ser separador de milhar
    if ',' in cru and '.' in cru:
        cru = cru.replace('.', '').replace(',', '.')
    elif ',' in cru:
        cru = cru.replace(',', '.')
    try:
        valor = float(cru)
    except ValueError:
        return None
    return int(valor * {'K': 1_000, 'M': 1_000_000, 'B': 1_000_000_000}[sufixo])


def extrair(texto: str, limite: int = 25) -> list[dict]:
    """Converte o texto da página na lista de tópicos. Separado para poder testar."""
    linhas = [l.strip() for l in str(texto).splitlines() if l.strip()]
    itens: list[dict] = []
    i = 0
    while i < len(linhas) - 2:
        posicao = linhas[i]
        if posicao.isdigit() and 1 <= int(posicao) <= 100:
            titulo = linhas[i + 1]
            views = numero(linhas[i + 2])
            if views is not None and len(titulo) >= 4 and not titulo.isdigit():
                itens.append({'posicao': int(posicao), 'titulo': titulo,
                              'views': linhas[i + 2].upper(), 'views_num': views})
                i += 3
                if len(itens) >= limite:
                    break
                continue
        i += 1
    return itens


def ler(limite: int = 25) -> list[dict]:
    """Abre a página de inspiração (com janela: em headless ela não renderiza)."""
    from backend.services import tiktok_local_publisher as ttl

    ttl._ensure_runtime()
    from playwright.sync_api import sync_playwright

    perfil = ttl.profile_dir()
    if not perfil.is_dir():
        raise RuntimeError('Nenhum perfil logado: entre no TikTok pelos Ajustes primeiro.')

    with sync_playwright() as p:
        contexto = p.chromium.launch_persistent_context(
            str(perfil), channel=ttl._channel(), headless=False, viewport=None,
            locale='pt-BR',
            args=['--disable-blink-features=AutomationControlled',
                  '--window-position=-32000,-32000'],  # abre fora da tela
            ignore_default_args=['--enable-automation'],
        )
        try:
            pagina = contexto.pages[0] if contexto.pages else contexto.new_page()
            pagina.set_default_timeout(60_000)
            pagina.goto(URL, wait_until='domcontentloaded', timeout=90_000)
            pagina.wait_for_timeout(9000)
            if 'login' in pagina.url.lower():
                raise RuntimeError('A sessão do TikTok expirou: entre de novo pelos Ajustes.')
            texto = pagina.locator('body').inner_text()
        finally:
            try:
                contexto.close()
            except Exception:  # noqa: BLE001
                pass
    return extrair(texto, limite)


def _rodar(limite: int) -> None:
    try:
        itens = ler(limite)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({'quando': time.time(), 'itens': itens},
                                    ensure_ascii=False, indent=2), encoding='utf-8')
        _atualizar(rodando=False, erro=None, quando=time.time(), total=len(itens),
                   mensagem=f'{len(itens)} tópico(s) em alta.' if itens
                   else 'A página abriu, mas nenhum tópico foi reconhecido.')
    except Exception as erro:  # noqa: BLE001
        _atualizar(rodando=False, erro=str(erro), mensagem=f'Falhou: {erro}')


def atualizar(limite: int = 25) -> dict:
    with _LOCK:
        if _ESTADO['rodando']:
            return {'iniciado': False, 'mensagem': 'Já estou lendo as tendências.'}
        _ESTADO.update(rodando=True, mensagem='Abrindo a página de inspiração…', erro=None)
    threading.Thread(target=_rodar, args=(limite,), daemon=True, name='tendencias').start()
    return {'iniciado': True, 'mensagem': 'Lendo as tendências do TikTok…'}


def lista() -> list[dict]:
    if not CACHE.is_file():
        return []
    try:
        return json.loads(CACHE.read_text(encoding='utf-8')).get('itens') or []
    except (OSError, ValueError):
        return []


if __name__ == '__main__':
    print(json.dumps(ler(10), ensure_ascii=False, indent=2))
