"""Aba Baixar: traz vídeos do TikTok para servir de material em outra ferramenta.

Usa o yt-dlp que já vem com o app. Guarda em `%APPDATA%\\TikClip\\baixados` com
metadados ao lado. Não republica nada — o arquivo fica disponível para você usar
como fonte (por exemplo, como vídeo de fundo no modo Gerar).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

PASTA = Path(os.environ.get('APPDATA') or Path.home()) / 'TikClip' / 'baixados'

_ESTADO: dict = {'rodando': False, 'mensagem': 'Nada baixado ainda.', 'percent': 0,
                 'velocidade': '', 'url': None, 'arquivo': None, 'erro': None,
                 'titulo': None, 'quando': None}
_LOCK = threading.Lock()
LIMITE_SEGUNDOS = 180


def _atualizar(**campos):
    with _LOCK:
        _ESTADO.update(campos)


def status() -> dict:
    with _LOCK:
        return dict(_ESTADO)


def url_valida(url: str) -> bool:
    try:
        partes = urlparse(url)
    except ValueError:
        return False
    host = (partes.hostname or '').lower()
    if partes.scheme != 'https':
        return False
    if host in ('vm.tiktok.com', 'vt.tiktok.com'):
        return True   # link curto: só dá para saber depois de resolver
    if not (host == 'tiktok.com' or host.endswith('.tiktok.com')):
        return False
    # perfil/favoritos não baixam: precisa apontar para um vídeo
    return '/video/' in (partes.path or '') or '/v/' in (partes.path or '')


def _limpar(nome: str) -> str:
    nome = re.sub(r'[^\w\- .]+', '', nome or '').strip()
    return (nome[:80] or 'video').strip('. ')


def _baixar(url: str, ident: str) -> None:
    import yt_dlp

    destino = PASTA / ident
    destino.mkdir(parents=True, exist_ok=True)
    template = str(destino / 'video.%(ext)s')

    def gancho(dados):
        if dados.get('status') == 'downloading':
            baixado = dados.get('downloaded_bytes') or 0
            total = dados.get('total_bytes') or dados.get('total_bytes_estimate') or 0
            percent = int(baixado / total * 100) if total else 0
            velocidade = dados.get('_speed_str') or ''
            _atualizar(percent=percent, velocidade=velocidade.strip(),
                       mensagem=f'Baixando… {percent}%')
        elif dados.get('status') == 'finished':
            _atualizar(percent=95, mensagem='gravando o arquivo…')

    opcoes = {
        'quiet': True, 'no_warnings': True, 'noplaylist': True,
        'outtmpl': template, 'progress_hooks': [gancho],
        'format': 'mp4/bestvideo*+bestaudio/best',
        'merge_output_format': 'mp4',
        'socket_timeout': 30, 'retries': 3,
    }
    try:
        caixa: dict = {}

        def executar():
            try:
                with yt_dlp.YoutubeDL(opcoes) as ydl:
                    caixa['info'] = ydl.extract_info(url, download=True)
            except Exception as falha:  # noqa: BLE001
                caixa['erro'] = falha

        trabalho = threading.Thread(target=executar, daemon=True)
        trabalho.start()
        trabalho.join(timeout=LIMITE_SEGUNDOS)
        if trabalho.is_alive():
            raise RuntimeError(f'o TikTok não respondeu em {LIMITE_SEGUNDOS}s')
        if caixa.get('erro'):
            raise caixa['erro']
        info = caixa.get('info') or {}
        arquivos = [p for p in destino.glob('video.*') if p.suffix.lower() in
                    ('.mp4', '.mkv', '.webm', '.mov')]
        if not arquivos:
            raise RuntimeError('o TikTok não devolveu arquivo de vídeo')
        video = arquivos[0]
        meta = {
            'titulo': str(info.get('title') or info.get('description') or ident)[:120],
            'autor': str(info.get('uploader') or info.get('uploader_id') or ''),
            'duracao': round(float(info.get('duration') or 0), 1),
            'url': url, 'quando': time.time(),
            'arquivo': video.name,
            'tamanho_mb': round(video.stat().st_size / 1_048_576, 1),
        }
        (destino / 'baixado.json').write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
        _atualizar(rodando=False, percent=100, arquivo=str(video), erro=None,
                   titulo=meta['titulo'], quando=time.time(),
                   mensagem=f"Pronto: {meta['titulo'][:60]} ({meta['tamanho_mb']} MB)")
    except Exception as erro:  # noqa: BLE001
        _atualizar(rodando=False, erro=str(erro),
                   mensagem=f'Falhou: {_explicar(erro)}')


def _explicar(erro: Exception) -> str:
    texto = str(erro)
    if 'Unsupported URL' in texto:
        return 'esse link não parece ser de vídeo do TikTok'
    if 'Private' in texto or 'login' in texto.lower():
        return 'o vídeo é privado ou exige login'
    if '404' in texto or 'Not Found' in texto:
        return 'vídeo não encontrado (pode ter sido removido)'
    return texto[:180]


def iniciar(url: str) -> dict:
    url = (url or '').strip()
    if not url_valida(url):
        return {'iniciado': False,
                'mensagem': 'Cole um link https de vídeo do TikTok.'}
    with _LOCK:
        if _ESTADO['rodando']:
            return {'iniciado': False, 'mensagem': 'Já existe um download em andamento.'}
        ident = time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:4]
        _ESTADO.update(rodando=True, percent=0, velocidade='', erro=None, url=url,
                       mensagem='Começando o download…', titulo=None, arquivo=None,
                       quando=None)
    PASTA.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_baixar, args=(url, ident), daemon=True,
                     name='baixar-tiktok').start()
    return {'iniciado': True, 'id': ident, 'mensagem': 'Baixando do TikTok…'}


def lista() -> list[dict]:
    if not PASTA.is_dir():
        return []
    itens = []
    for pasta in PASTA.iterdir():
        meta = pasta / 'baixado.json'
        if not meta.is_file():
            continue
        try:
            dados = json.loads(meta.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        video = pasta / (dados.get('arquivo') or 'video.mp4')
        if not video.is_file():
            continue
        dados.update({'id': pasta.name, 'video': f'/api/v1/ext/baixar/video/{pasta.name}'})
        itens.append(dados)
    return sorted(itens, key=lambda d: d.get('quando') or 0, reverse=True)


def caminho_video(ident: str) -> Path | None:
    if not ident.replace('-', '').isalnum():
        return None
    pasta = PASTA / ident
    if not pasta.is_dir():
        return None
    for arquivo in pasta.glob('video.*'):
        if arquivo.suffix.lower() in ('.mp4', '.mkv', '.webm', '.mov'):
            return arquivo
    return None

def cancelar() -> dict:
    """Destrava a fila quando o TikTok não responde (o download em curso é abandonado)."""
    _atualizar(rodando=False, percent=0, mensagem='Download cancelado.',
               velocidade='', erro='cancelado')
    return {'ok': True, 'mensagem': 'Fila destravada.'}
