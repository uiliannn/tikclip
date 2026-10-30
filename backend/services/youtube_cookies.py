"""Exporta os cookies do navegador embutido do YT Saver (CEF) para formato Netscape.

Por que existe: o motor do YT Saver passa do bloqueio anti-robo porque usa os cookies
da sessao Google logada dentro do navegador dele. O CEF cifra os valores com DPAPI
(o mesmo usuario descriptografa) e guarda a chave em `Local State`.

Uso (dentro do app): ler_cookies_cef() -> grava cookies.txt -> yt-dlp usa cookiefile.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

CRYPTO = 'DPAPI'


class _Blob(ctypes.Structure):
    _fields_ = [('cbData', wt.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]


def _dpapi(blob: bytes) -> bytes:
    """CryptUnprotectData na esfera do usuario atual."""
    cripto = ctypes.windll.crypt32
    kernel = ctypes.windll.kernel32
    entrada = _Blob(len(blob), ctypes.cast(ctypes.create_string_buffer(blob),
                                           ctypes.POINTER(ctypes.c_char)))
    saida = _Blob()
    if not cripto.CryptUnprotectData(ctypes.byref(entrada), None, None, None, None, 0,
                                     ctypes.byref(saida)):
        raise OSError('CryptUnprotectData falhou')
    try:
        return ctypes.string_at(saida.pbData, saida.cbData)
    finally:
        kernel.LocalFree(saida.pbData)


def _chave_mestra(local_state: Path) -> bytes:
    dados = json.loads(local_state.read_text(encoding='utf-8'))
    chave = base64.b64decode(dados['os_crypt']['encrypted_key'])
    if chave.startswith(CRYPTO.encode()):
        chave = chave[len(CRYPTO):]
    return _dpapi(chave)


def _decifrar(valor: bytes, chave: bytes) -> str:
    """v10 = AES-256-GCM (nonce 12 + tag 16). Versoes novas prefixam hash do dominio."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not valor:
        return ''
    if valor[:3] in (b'v10', b'v11'):
        nonce, corpo = valor[3:15], valor[15:]
        texto = AESGCM(chave).decrypt(nonce, corpo, None)
        return texto[32:].decode('utf-8', 'replace') if len(texto) > 32 else texto.decode('utf-8', 'replace')
    # legado: DPAPI direto
    return _dpapi(valor).decode('utf-8', 'replace')


def _abrir_banco(banco: Path) -> list[tuple]:
    """Le as linhas de cookies mesmo com o YT Saver aberto.

    O CEF mantem o arquivo aberto; copiar falha com WinError 32. SQLite permite
    leitura concorrente, entao tentamos abrir direto em modo somente-leitura e,
    se o escritor estiver no meio de uma transacao, repetimos algumas vezes.
    """
    import time

    tentativas = [
        f'file:{banco.as_posix()}?mode=ro',
        f'file:{banco.as_posix()}?mode=ro&nolock=1',
    ]
    ultimo = None
    for tentativa in tentativas:
        for espera in (0, 0.3, 0.8, 1.5):
            if espera:
                time.sleep(espera)
            try:
                con = sqlite3.connect(tentativa, uri=True, timeout=5)
                try:
                    return con.execute(
                        'select host_key, path, is_secure, is_httponly, expires_utc, name, '
                        'value, encrypted_value from cookies').fetchall()
                finally:
                    con.close()
            except sqlite3.Error as erro:
                ultimo = erro
                continue
    # ultimo recurso: copia (funciona quando o CEF solta o arquivo)
    temp = Path(tempfile.mkdtemp(prefix='cef_cookies_'))
    copia = temp / 'Cookies'
    try:
        shutil.copy2(banco, copia)
        con = sqlite3.connect(f'file:{copia.as_posix()}?mode=ro', uri=True, timeout=5)
        try:
            return con.execute(
                'select host_key, path, is_secure, is_httponly, expires_utc, name, '
                'value, encrypted_value from cookies').fetchall()
        finally:
            con.close()
    except Exception as erro:  # noqa: BLE001
        raise OSError(f'nao consegui ler os cookies do CEF ({erro}); ultimo erro: {ultimo}') from erro
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def ler_cookies_cef(perfil: Path | None = None) -> list[dict]:
    """Le o banco de cookies do CEF (o YT Saver pode estar aberto)."""
    perfil = Path(perfil) if perfil else Path(os.environ['LOCALAPPDATA'], 'YT Saver', 'CefCache')
    banco = perfil / 'Default' / 'Network' / 'Cookies'
    local_state = perfil / 'Local State'
    if not banco.is_file():
        raise FileNotFoundError(f'banco de cookies do CEF nao encontrado: {banco}')
    chave = _chave_mestra(local_state) if local_state.is_file() else b''
    linhas = _abrir_banco(banco)

    cookies = []
    for host, caminho, seguro, httponly, expira, nome, valor, cifrado in linhas:
        if not valor and cifrado:
            try:
                valor = _decifrar(cifrado, chave)
            except Exception:  # noqa: BLE001 - cookie ilegivel: ignora
                continue
        if not valor:
            continue
        # expires_utc do Chromium: microssegundos desde 1601
        expira_s = int(expira / 1_000_000 - 11644473600) if expira else 0
        cookies.append({'domain': host, 'path': caminho or '/', 'secure': bool(seguro),
                        'http_only': bool(httponly), 'expires': max(expira_s, 0),
                        'name': nome, 'value': valor})
    return cookies


def para_netscape(cookies: list[dict]) -> str:
    linhas = ['# Netscape HTTP Cookie File', '# gerado pelo TikClip a partir do CEF']
    for c in cookies:
        linhas.append('\t'.join([
            c['domain'], 'TRUE' if c['domain'].startswith('.') else 'FALSE', c['path'],
            'TRUE' if c['secure'] else 'FALSE', str(c['expires']), c['name'], c['value'],
        ]))
    return '\n'.join(linhas) + '\n'


def exportar(destino: Path | None = None, perfil: Path | None = None) -> tuple[Path, int, list[str]]:
    """Grava cookies.txt e devolve (caminho, quantidade, nomes importantes encontrados)."""
    cookies = ler_cookies_cef(perfil)
    texto = para_netscape(cookies)
    destino = Path(destino) if destino else Path(os.environ['APPDATA'], 'TikClip', 'cookies-cef.txt')
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(texto, encoding='utf-8')
    importantes = [n for n in ('SAPISID', '__Secure-1PSID', '__Secure-3PSID', 'LOGIN_INFO',
                               'SID', 'HSID', 'SSID', 'APISID')
                   if any(c['name'] == n for c in cookies)]
    return destino, len(cookies), importantes


def status() -> dict:
    """Situacao dos cookies usados no download, sem expor valores."""
    destino = Path(os.environ['APPDATA'], 'TikClip', 'cookies-cef.txt')
    if not destino.is_file():
        return {'tem_arquivo': False, 'caminho': str(destino), 'total': 0,
                'sessao_google': [], 'atualizado_em': None}
    linhas = [l for l in destino.read_text(encoding='utf-8', errors='replace').splitlines()
              if l and not l.startswith('#')]
    nomes = {l.split('\t')[5] for l in linhas if len(l.split('\t')) >= 7}
    sessao = [n for n in ('SAPISID', '__Secure-1PSID', '__Secure-3PSID', 'LOGIN_INFO')
              if n in nomes]
    return {'tem_arquivo': True, 'caminho': str(destino), 'total': len(linhas),
            'sessao_google': sessao,
            'atualizado_em': destino.stat().st_mtime}


def renovar(auto: bool = True) -> dict:
    """Tenta reexportar do CEF. Se o YT Saver estiver aberto, devolve motivo claro."""
    try:
        caminho, total, importantes = exportar()
    except OSError as erro:
        texto = str(erro)
        travado = '32' in texto or 'usado por outro processo' in texto or 'unable to open' in texto
        return {'ok': False, 'travado': travado, 'total': 0,
                'mensagem': ('O YT Saver está aberto e prende o arquivo de cookies. '
                             'Feche-o por alguns segundos e tente de novo.'
                             if travado else f'Não consegui ler os cookies: {erro}')}
    except FileNotFoundError as erro:
        return {'ok': False, 'travado': False, 'total': 0,
                'mensagem': f'YT Saver não encontrado nesta máquina ({erro})'}
    return {'ok': True, 'travado': False, 'total': total,
            'sessao_google': importantes, 'caminho': str(caminho),
            'mensagem': f'{total} cookies importados' +
                        (' (sessão Google)' if importantes else ' (sem sessão Google)')}


if __name__ == '__main__':
    caminho, total, importantes = exportar()
    print(f'  gravado : {caminho}')
    print(f'  cookies : {total}')
    print(f'  sessao Google: {importantes}')
