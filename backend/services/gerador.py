"""Fila do modo "Gerar do zero": dispara a geração em segundo plano e guarda a galeria.

Cada vídeo vive em `%APPDATA%\\TikClip\\gerados\\<id>\\` com `video.mp4` e `gerado.json`
(título, hashtags, assunto, duração). A galeria lê essa pasta — nada de banco.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

PASTA = Path(os.environ.get('APPDATA') or Path.home()) / 'TikClip' / 'gerados'
FUNDOS = Path(os.environ.get('APPDATA') or Path.home()) / 'TikClip' / 'fundos'

_ESTADO: dict = {
    'rodando': False, 'etapa': 'parado', 'mensagem': 'Nenhuma geração em andamento.',
    'cena': 0, 'total': 0, 'iniciado_em': None, 'terminado_em': None,
    'resultado': None, 'erro': None, 'id': None,
    'video': 0, 'videos': 1, 'feitos': [],
}
_LOCK = threading.Lock()


def _atualizar(**campos):
    with _LOCK:
        _ESTADO.update(campos)


# --------------------------------------------------------------------------
# vídeos de fundo enviados pelo usuário
# --------------------------------------------------------------------------
EXTENSOES = ('.mp4', '.mov', '.mkv', '.webm', '.avi')


def salvar_fundo(nome: str, dados: bytes) -> dict:
    """Guarda um vídeo de fundo; recusa formato desconhecido e arquivo vazio."""
    from pathlib import Path as _Path

    sufixo = _Path(nome or '').suffix.lower()
    if sufixo not in EXTENSOES:
        raise ValueError('Formato não suportado. Use MP4, MOV, MKV, WEBM ou AVI.')
    if not dados or len(dados) < 100_000:
        raise ValueError('O vídeo está vazio ou é curto demais.')
    FUNDOS.mkdir(parents=True, exist_ok=True)
    ident = time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:4] + sufixo
    (FUNDOS / ident).write_bytes(dados)
    return {'id': ident, 'nome': nome, 'tamanho_mb': round(len(dados) / 1_048_576, 1)}


def listar_fundos() -> list[dict]:
    if not FUNDOS.is_dir():
        return []
    itens = []
    for arquivo in FUNDOS.iterdir():
        if arquivo.suffix.lower() in EXTENSOES and arquivo.is_file():
            itens.append({'id': arquivo.name, 'nome': arquivo.name,
                          'tamanho_mb': round(arquivo.stat().st_size / 1_048_576, 1),
                          'quando': arquivo.stat().st_mtime})
    return sorted(itens, key=lambda d: d['quando'], reverse=True)


def caminho_fundo(ident: str):
    """Aceita só nome simples dentro da pasta de fundos."""
    if not ident or any(c in ident for c in '/:*?"<>|\\'):
        return None
    alvo = FUNDOS / ident
    return alvo if alvo.is_file() else None


def status() -> dict:
    with _LOCK:
        return dict(_ESTADO)


def _progresso(campos: dict) -> None:
    permitido = {k: v for k, v in campos.items()
                 if k in ('etapa', 'mensagem', 'cena', 'total', 'resultado')}
    if 'resultado' in permitido:
        permitido['resultado'] = {
            k: permitido['resultado'].get(k)
            for k in ('titulo', 'hashtags', 'duracao', 'arquivo', 'pasta', 'assunto')
        }
    _atualizar(**permitido)


def _rodar(assunto: str, duracao: float, voz: str | None, ident: str,
           quantidade: int = 1, fundo: str | None = None) -> None:
    from backend.services.studio import generator

    # o id do fundo precisa virar caminho absoluto antes de chegar no gerador
    caminho_do_fundo = caminho_fundo(fundo) if fundo else None
    if fundo and not caminho_do_fundo:
        _atualizar(rodando=False, etapa='erro', terminado_em=time.time(),
                   erro='vídeo de fundo não encontrado',
                   mensagem='Vídeo de fundo não encontrado: envie de novo.')
        return
    feitos: list[dict] = []
    for indice in range(max(1, quantidade)):
        sub = ident if quantidade == 1 else f'{ident}-{indice + 1}'
        destino = PASTA / sub
        _atualizar(video=indice + 1, videos=quantidade)
        try:
            plano = generator.gerar(assunto, duracao, pasta=destino, voz=voz,
                                    progresso=_progresso,
                                    fundo=str(caminho_do_fundo) if caminho_do_fundo else None)
        except Exception as erro:  # noqa: BLE001
            # pasta sem vídeo não serve para nada: some da galeria
            try:
                if destino.is_dir() and not (destino / 'video.mp4').is_file():
                    for restante in sorted(destino.rglob('*'), reverse=True):
                        restante.unlink() if restante.is_file() else restante.rmdir()
                    destino.rmdir()
            except OSError:
                pass
            _atualizar(rodando=False, etapa='erro', terminado_em=time.time(),
                       erro=str(erro),
                       mensagem=f'Falhou no vídeo {indice + 1} de {quantidade}: {erro}')
            return
        feitos.append({k: plano.get(k) for k in
                       ('titulo', 'hashtags', 'duracao', 'arquivo', 'pasta', 'assunto',
                        'fundo')})
        _atualizar(feitos=list(feitos))
    plano = plano  # último
    total = len(feitos)
    _atualizar(rodando=False, etapa='pronto', terminado_em=time.time(),
               mensagem=(f'{total} vídeo(s) pronto(s).' if total > 1
                         else f'Vídeo pronto ({plano["duracao"]:.1f}s).'),
               resultado={k: plano.get(k) for k in
                          ('titulo', 'hashtags', 'duracao', 'arquivo', 'pasta', 'assunto')},
               feitos=feitos)


def iniciar(assunto: str, duracao: float, voz: str | None = None,
            quantidade: int = 1, fundo: str | None = None) -> dict:
    with _LOCK:
        if _ESTADO['rodando']:
            return {'iniciado': False, 'mensagem': 'Já existe uma geração em andamento.'}
        ident = time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:4]
        quantidade = max(1, min(5, int(quantidade or 1)))
        _ESTADO.update(rodando=True, etapa='iniciando', cena=0, total=0,
                       mensagem='Começando…', iniciado_em=time.time(), terminado_em=None,
                       resultado=None, erro=None, id=ident, video=1, videos=quantidade,
                       feitos=[])
    PASTA.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_rodar,
                     args=(assunto, float(duracao), voz, ident, quantidade, fundo),
                     daemon=True, name='gerar-do-zero').start()
    return {'iniciado': True, 'id': ident, 'videos': quantidade,
            'mensagem': (f'Gerando {quantidade} vídeos: roteiro, imagens e narração.'
                         if quantidade > 1 else
                         'Gerando: roteiro, imagens e narração. Isso leva alguns minutos.')}


def lista() -> list[dict]:
    """Vídeos já gerados, do mais novo para o mais antigo."""
    if not PASTA.is_dir():
        return []
    itens = []
    for pasta in PASTA.iterdir():
        meta = pasta / 'gerado.json'
        video = pasta / 'video.mp4'
        if not (meta.is_file() and video.is_file()):
            continue
        try:
            dados = json.loads(meta.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        dados.update({'id': pasta.name,
                      'video': f'/api/v1/ext/gerar/video/{pasta.name}',
                      'tamanho_mb': round(video.stat().st_size / 1_048_576, 1),
                      'quando': (dados.get('criado_em') or pasta.stat().st_mtime)})
        itens.append(dados)
    return sorted(itens, key=lambda d: d.get('quando') or 0, reverse=True)


def caminho_video(ident: str) -> Path | None:
    """Só aceita id simples: evita sair da pasta da galeria."""
    if not ident.replace('-', '').replace('_', '').isalnum():
        return None
    alvo = PASTA / ident / 'video.mp4'
    return alvo if alvo.is_file() else None


if __name__ == '__main__':
    print('pasta:', PASTA)
    for item in lista():
        print(' -', item['id'], item.get('titulo'), item.get('duracao'), 's')

# --------------------------------------------------------------------------
# publicação de um vídeo gerado (direto, sem passar por projeto/variante)
# --------------------------------------------------------------------------
_PUBLICACAO: dict = {'rodando': False, 'mensagem': 'Nada publicado ainda.',
                     'ok': None, 'url': None, 'id': None}
_LOCK_PUB = threading.Lock()


def status_publicacao() -> dict:
    with _LOCK_PUB:
        return dict(_PUBLICACAO)


def capa_do_video(ident: str) -> Path | None:
    """Extrai (uma vez) o primeiro frame como capa do TikTok."""
    from backend.services.studio.generator import _ffmpeg, _rodar

    pasta = PASTA / ident
    video = pasta / 'video.mp4'
    if not video.is_file():
        return None
    capa = pasta / 'capa.jpg'
    if capa.is_file():
        return capa
    try:
        _rodar([_ffmpeg(), '-y', '-ss', '0.5', '-i', str(video), '-frames:v', '1',
                '-vf', 'scale=1080:1920', str(capa)], timeout=120)
    except Exception:  # noqa: BLE001 - capa é opcional
        return None
    return capa if capa.is_file() else None


def _publicar(ident: str, conta: str | None) -> None:
    from backend.services import tiktok_local_publisher as ttl

    try:
        meta = json.loads((PASTA / ident / 'gerado.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        meta = {}
    video = PASTA / ident / 'video.mp4'
    try:
        with _LOCK_PUB:
            _PUBLICACAO.update(rodando=True, mensagem='Abrindo o TikTok Studio…',
                               ok=None, url=None, id=ident)
        resultado = ttl.publish_video(
            video, meta.get('titulo') or ident, '', meta.get('hashtags') or [],
            cover_path=capa_do_video(ident), account=conta,
            progress=lambda texto: _atualizar_publicacao(mensagem=texto))
        with _LOCK_PUB:
            _PUBLICACAO.update(rodando=False, ok=True,
                               mensagem=resultado.get('message') or 'Publicado.',
                               url=resultado.get('url'))
    except Exception as erro:  # noqa: BLE001
        with _LOCK_PUB:
            _PUBLICACAO.update(rodando=False, ok=False, mensagem=f'Falhou: {erro}')


def _atualizar_publicacao(**campos):
    with _LOCK_PUB:
        _PUBLICACAO.update(campos)


def publicar(ident: str, conta: str | None = None) -> dict:
    caminho = caminho_video(ident)
    if not caminho:
        return {'iniciado': False, 'mensagem': 'Vídeo não encontrado na galeria.'}
    with _LOCK_PUB:
        if _PUBLICACAO['rodando']:
            return {'iniciado': False, 'mensagem': 'Já existe uma publicação em andamento.'}
        _PUBLICACAO.update(rodando=True, mensagem='Preparando…', ok=None, url=None, id=ident)
    threading.Thread(target=_publicar, args=(ident, conta), daemon=True,
                     name='publicar-gerado').start()
    return {'iniciado': True, 'mensagem': 'Publicando no TikTok…'}