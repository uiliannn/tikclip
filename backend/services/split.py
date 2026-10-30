"""Mini editor de tela dividida: conteúdo em cima, vídeo de foco embaixo.

Formato clássico do TikTok: metade mostra o que se explica, a outra metade roda um
vídeo que prende a atenção (gameplay, ASMR, loop satisfatório).

Entradas vêm das pastas que o app já gerencia:
- `baixados/`  (aba Baixar)
- `gerados/`   (aba Gerar)
- `fundos/`    (vídeos enviados para fundo)
- `splits/`    (resultados anteriores)

Nada é reenviado para fora: tudo local.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

BASE = Path(os.environ.get('APPDATA') or Path.home()) / 'TikClip'
SAIDA = BASE / 'splits'
EXT = ('.mp4', '.mov', '.mkv', '.webm', '.avi')
LARGURA, ALTURA, FPS = 1080, 1920, 30

_ESTADO: dict = {'rodando': False, 'mensagem': 'Nada montado ainda.', 'etapa': 'parado',
                 'erro': None, 'resultado': None}
_LOCK = threading.Lock()


def _atualizar(**campos):
    with _LOCK:
        _ESTADO.update(campos)


def status() -> dict:
    with _LOCK:
        return dict(_ESTADO)


def _ffmpeg() -> str:
    from backend.utils.ffmpeg_utils import get_ffmpeg_path
    return get_ffmpeg_path()


def _ffprobe() -> str | None:
    candidatos = []
    try:
        candidatos += [Path(_ffmpeg()).with_name('ffprobe.exe'),
                       Path(_ffmpeg()).with_name('ffprobe')]
    except Exception:  # noqa: BLE001
        pass
    raiz = Path(__file__).resolve().parents[3]
    candidatos.append(raiz / 'runtime' / 'ffmpeg' / 'ffprobe.exe')
    for caminho in candidatos:
        try:
            if caminho.is_file():
                return str(caminho)
        except OSError:
            continue
    return None


def _duracao(caminho: Path) -> float:
    sonda = _ffprobe()
    if sonda:
        saida = subprocess.run(
            [sonda, '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=nw=1:nk=1', str(caminho)], capture_output=True, timeout=120)
        try:
            return float(saida.stdout.decode().strip())
        except ValueError:
            pass
    # fallback: o próprio ffmpeg imprime Duration no stderr
    saida = subprocess.run([_ffmpeg(), '-i', str(caminho)], capture_output=True, timeout=120)
    import re as _re
    achado = _re.search(r'Duration:\s*(\d+):(\d+):(\d+\.\d+)',
                        saida.stderr.decode('utf-8', 'replace'))
    if achado:
        return int(achado.group(1)) * 3600 + int(achado.group(2)) * 60 + float(achado.group(3))
    return 0.0


def fontes() -> list[dict]:
    """Vídeos disponíveis para usar como conteúdo ou como foco."""
    itens: list[dict] = []
    pastas = [('baixado', BASE / 'baixados'), ('gerado', BASE / 'gerados'),
              ('fundo', BASE / 'fundos'), ('split', SAIDA)]
    for tipo, pasta in pastas:
        if not pasta.is_dir():
            continue
        for arquivo in pasta.rglob('*'):
            if not arquivo.is_file() or arquivo.suffix.lower() not in EXT:
                continue
            if 'cena_' in arquivo.name or arquivo.name.startswith('bruto'):
                continue
            rotulo = arquivo.parent.name if pasta.name != 'fundos' else arquivo.name
            itens.append({
                'id': f'{tipo}:{arquivo.relative_to(pasta).as_posix()}',
                'tipo': tipo,
                'nome': f'{tipo} · {rotulo}',
                'tamanho_mb': round(arquivo.stat().st_size / 1_048_576, 1),
                'quando': arquivo.stat().st_mtime,
            })
    return sorted(itens, key=lambda d: d['quando'], reverse=True)


def _resolver(ident: str) -> Path | None:
    """'baixado:abc/video.mp4' -> caminho dentro da pasta correspondente."""
    if not ident or ':' not in ident:
        return None
    tipo, resto = ident.split(':', 1)
    raiz = {'baixado': BASE / 'baixados', 'gerado': BASE / 'gerados',
            'fundo': BASE / 'fundos', 'split': SAIDA}.get(tipo)
    if raiz is None:
        return None
    alvo = (raiz / resto).resolve()
    try:
        alvo.relative_to(raiz.resolve())
    except ValueError:
        return None
    return alvo if alvo.is_file() else None


def montar(principal: Path, foco: Path, destino: Path, divisao: float = 0.5,
           audio: str = 'principal', titulo: str = '') -> Path:
    """Empilha os dois vídeos em 1080x1920 e mantém só o áudio escolhido."""
    altura_principal = max(240, int(round(ALTURA * divisao / 2)) * 2)
    altura_foco = ALTURA - altura_principal
    duracao = max(1.0, _duracao(principal))
    instrucoes = (
        f"[0:v]scale={LARGURA}:{altura_principal}:force_original_aspect_ratio=increase,"
        f"crop={LARGURA}:{altura_principal},setsar=1[a];"
        f"[1:v]scale={LARGURA}:{altura_foco}:force_original_aspect_ratio=increase,"
        f"crop={LARGURA}:{altura_foco},setsar=1[b];"
        f"[a][b]vstack=inputs=2[v]"
    )
    mapa_audio = '0:a' if audio != 'foco' else '1:a'
    comando = [_ffmpeg(), '-y',
               '-i', str(principal),
               '-stream_loop', '-1', '-i', str(foco),
               '-filter_complex', instrucoes,
               '-map', '[v]', '-map', mapa_audio,
               '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
               '-r', str(FPS), '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-ac', '2',
               '-t', f'{duracao:.3f}', str(destino)]
    resultado = subprocess.run(comando, capture_output=True, timeout=1800)
    if resultado.returncode != 0:
        raise RuntimeError(resultado.stderr.decode('utf-8', 'replace')[-500:])
    return destino


def _rodar(principal_id: str, foco_id: str, divisao: float, audio: str, titulo: str) -> None:
    try:
        principal = _resolver(principal_id)
        foco = _resolver(foco_id)
        if not principal or not foco:
            raise RuntimeError('escolha dois vídeos válidos')
        ident = time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:4]
        destino = SAIDA / ident
        destino.mkdir(parents=True, exist_ok=True)
        _atualizar(etapa='montando', mensagem='Juntando os dois vídeos…')
        video = montar(principal, foco, destino / 'video.mp4', divisao, audio, titulo)
        meta = {
            'titulo': titulo or f'split {ident}',
            'data': titulo,
            'principal': principal_id, 'foco': foco_id,
            'divisao': divisao, 'audio': audio,
            'duracao': round(_duracao(video), 1),
            'tamanho_mb': round(video.stat().st_size / 1_048_576, 1),
            'quando': time.time(),
        }
        (destino / 'split.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                            encoding='utf-8')
        _atualizar(rodando=False, etapa='pronto', erro=None, resultado=meta,
                   mensagem=f"Pronto: {meta['duracao']}s ({meta['tamanho_mb']} MB)")
    except Exception as erro:  # noqa: BLE001
        _atualizar(rodando=False, etapa='erro', erro=str(erro),
                   mensagem=f'Falhou: {str(erro)[:180]}')


def iniciar(principal_id: str, foco_id: str, divisao: float = 0.5,
            audio: str = 'principal', titulo: str = '') -> dict:
    with _LOCK:
        if _ESTADO['rodando']:
            return {'iniciado': False, 'mensagem': 'Já existe uma montagem em andamento.'}
        _ESTADO.update(rodando=True, etapa='preparando', erro=None, resultado=None,
                       mensagem='Preparando…')
    divisao = max(0.3, min(0.75, float(divisao or 0.5)))
    threading.Thread(target=_rodar, args=(principal_id, foco_id, divisao, audio, titulo),
                     daemon=True, name='split').start()
    return {'iniciado': True, 'mensagem': 'Montando a tela dividida…'}


def lista() -> list[dict]:
    if not SAIDA.is_dir():
        return []
    itens = []
    for pasta in SAIDA.iterdir():
        meta = pasta / 'split.json'
        video = pasta / 'video.mp4'
        if not (meta.is_file() and video.is_file()):
            continue
        try:
            dados = json.loads(meta.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        dados.update({'id': pasta.name, 'video': f'/api/v1/ext/split/video/{pasta.name}'})
        itens.append(dados)
    return sorted(itens, key=lambda d: d.get('quando') or 0, reverse=True)


def caminho_video(ident: str) -> Path | None:
    if not ident.replace('-', '').isalnum():
        return None
    alvo = SAIDA / ident / 'video.mp4'
    return alvo if alvo.is_file() else None

# ------------------------------------------------------------- publicação ---
_PUB: dict = {'rodando': False, 'mensagem': 'Nada publicado.', 'ok': None, 'url': None,
              'id': None, 'conta': None}
_LOCK_PUB = threading.Lock()


def _atualizar_pub(**campos):
    with _LOCK_PUB:
        _PUB.update(campos)


def status_publicacao() -> dict:
    with _LOCK_PUB:
        return dict(_PUB)


def _capa(video: Path, destino: Path) -> Path | None:
    try:
        subprocess.run([_ffmpeg(), '-y', '-ss', '1', '-i', str(video), '-frames:v', '1',
                        str(destino)], capture_output=True, timeout=180)
        return destino if destino.is_file() else None
    except (OSError, subprocess.SubprocessError):
        return None


def _publicar(ident: str, conta: str | None) -> None:
    from backend.services import tiktok_local_publisher as ttl

    pasta = SAIDA / ident
    video = pasta / 'video.mp4'
    try:
        meta = json.loads((pasta / 'split.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        meta = {}
    if not video.is_file():
        _atualizar_pub(rodando=False, ok=False, mensagem='Vídeo montado não encontrado.')
        return
    titulo = str(meta.get('data') or meta.get('titulo') or '').strip() or 'vídeo'
    try:
        _atualizar_pub(rodando=True, mensagem='Abrindo o TikTok Studio…', ok=None,
                       url=None, id=ident, conta=conta)
        resultado = ttl.publish_video(
            video, titulo[:90], '', [],
            cover_path=_capa(video, pasta / 'capa.jpg'), account=conta,
            progress=lambda texto: _atualizar_pub(mensagem=texto))
        _atualizar_pub(rodando=False, ok=True,
                       mensagem=resultado.get('message') or 'Publicado.',
                       url=resultado.get('url'))
    except Exception as erro:  # noqa: BLE001
        _atualizar_pub(rodando=False, ok=False, mensagem=f'Falhou: {str(erro)[:200]}')


def publicar(ident: str, conta: str | None = None) -> dict:
    if caminho_video(ident) is None:
        return {'iniciado': False, 'mensagem': 'Vídeo montado não encontrado.'}
    with _LOCK_PUB:
        if _PUB['rodando']:
            return {'iniciado': False, 'mensagem': 'Já existe uma publicação em andamento.'}
        _PUB.update(rodando=True, mensagem='Preparando…', ok=None, url=None, id=ident,
                    conta=conta)
    threading.Thread(target=_publicar, args=(ident, conta), daemon=True,
                     name='publicar-split').start()
    return {'iniciado': True, 'mensagem': 'Publicando no TikTok…'}
