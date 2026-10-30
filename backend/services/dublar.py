"""Dublagem: transcreve o áudio do vídeo e recria a fala com voz sintética.

Fluxo:
  1. transcreve (Whisper local, mesma máquina — nada sai para a nuvem);
  2. tira o áudio original (o vídeo é copiado, sem reencodar);
  3. gera a fala de cada trecho no VoiceStudio;
  4. posiciona cada trecho no tempo original (adelay) e mistura;
  5. junta áudio novo + vídeo sem som.

A voz sai com o mesmo texto do áudio original, no mesmo idioma.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

BASE = Path(os.environ.get('APPDATA') or Path.home()) / 'TikClip'
SAIDA = BASE / 'dublados'
ENTRADA = BASE / 'dublar-entrada'
EXT = ('.mp4', '.mov', '.mkv', '.webm', '.avi')

_ESTADO: dict = {'rodando': False, 'etapa': 'parado', 'mensagem': 'Nada dublado ainda.',
                 'percent': 0, 'erro': None, 'resultado': None, 'transcricao': None}
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


def _duracao(caminho: Path) -> float:
    saida = subprocess.run([_ffmpeg(), '-i', str(caminho)], capture_output=True, timeout=180)
    achado = re.search(r'Duration:\s*(\d+):(\d+):(\d+\.\d+)',
                       saida.stderr.decode('utf-8', 'replace'))
    if achado:
        return int(achado.group(1)) * 3600 + int(achado.group(2)) * 60 + float(achado.group(3))
    return 0.0


def salvar_video(nome: str, dados: bytes) -> dict:
    sufixo = Path(nome or '').suffix.lower()
    if sufixo not in EXT:
        raise ValueError('Formato não suportado. Use MP4, MOV, MKV, WEBM ou AVI.')
    if len(dados) < 100_000:
        raise ValueError('O vídeo está vazio ou é curto demais.')
    ENTRADA.mkdir(parents=True, exist_ok=True)
    ident = time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:4] + sufixo
    (ENTRADA / ident).write_bytes(dados)
    return {'id': ident, 'nome': nome, 'tamanho_mb': round(len(dados) / 1_048_576, 1)}


def listar_entradas() -> list[dict]:
    if not ENTRADA.is_dir():
        return []
    itens = [{'id': a.name, 'nome': a.name,
              'tamanho_mb': round(a.stat().st_size / 1_048_576, 1),
              'quando': a.stat().st_mtime}
             for a in ENTRADA.iterdir() if a.suffix.lower() in EXT and a.is_file()]
    return sorted(itens, key=lambda d: d['quando'], reverse=True)


def caminho_entrada(ident: str) -> Path | None:
    if not ident or any(c in ident for c in '/:*?"<>|\\'):
        return None
    alvo = ENTRADA / ident
    return alvo if alvo.is_file() else None


# ------------------------------------------------------------------ whisper --
def _modelo_whisper(nome: str = 'base'):
    """Carrega o faster-whisper do runtime isolado que o app já usa."""
    from backend.services import whisper_runtime

    whisper_runtime.ensure_on_path()
    from faster_whisper import WhisperModel

    raiz = whisper_runtime.get_models_dir()
    raiz.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault('HF_HOME', str(raiz))
    for dispositivo, tipo in (('cuda', 'float16'), ('cpu', 'int8')):
        try:
            return WhisperModel(nome, device=dispositivo, compute_type=tipo,
                                download_root=str(raiz))
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError('não consegui carregar o Whisper (modelo ausente?)')


def transcrever(video: Path, modelo: str = 'base') -> list[dict]:
    w = _modelo_whisper(modelo)
    trechos, _info = w.transcribe(str(video), vad_filter=True, beam_size=1)
    saida = []
    for trecho in trechos:
        texto = (trecho.text or '').strip()
        if not texto:
            continue
        saida.append({'inicio': round(float(trecho.start), 3),
                      'fim': round(float(trecho.end), 3), 'texto': texto})
    return saida


# ------------------------------------------------------------------- áudio --
def _falar(texto: str, destino: Path, voz: str | None) -> Path:
    from backend.services.studio import generator
    return generator.gerar_voz(texto, destino, voz)


def _juntar_audios(trechos: list[dict], pasta: Path, total: float) -> Path:
    """Posiciona cada fala no tempo original e mistura tudo num wav só."""
    comandos = [_ffmpeg(), '-y']
    filtros = []
    for i, trecho in enumerate(trechos):
        comandos += ['-i', str(trecho['wav'])]
        atraso = int(trecho['inicio'] * 1000)
        filtros.append(f'[{i}:a]aresample=48000,adelay={atraso}|{atraso}[a{i}]')
    soma = ''.join(f'[a{i}]' for i in range(len(trechos)))
    filtros.append(f'{soma}amix=inputs={len(trechos)}:duration=longest:normalize=0,'
                   f'apad=whole_dur={total:.3f},atrim=0:{total:.3f}[saida]')
    destino = pasta / 'voz.wav'
    comandos += ['-filter_complex', ';'.join(filtros), '-map', '[saida]',
                 '-ac', '2', '-ar', '48000', str(destino)]
    resultado = subprocess.run(comandos, capture_output=True, timeout=1800)
    if resultado.returncode != 0:
        raise RuntimeError(resultado.stderr.decode('utf-8', 'replace')[-400:])
    return destino


def dublar(video: Path, pasta: Path, voz: str | None = None, modelo: str = 'base') -> dict:
    pasta.mkdir(parents=True, exist_ok=True)
    _atualizar(etapa='transcrevendo', percent=5,
               mensagem='Ouvindo o áudio e escrevendo o texto…')
    trechos = transcrever(video, modelo)
    if not trechos:
        raise RuntimeError('não encontrei fala no vídeo (talvez seja só música ou silêncio)')
    _atualizar(transcricao=[{'inicio': t['inicio'], 'fim': t['fim'], 'texto': t['texto']}
                            for t in trechos])

    _atualizar(etapa='sem-som', percent=15, mensagem='Tirando o som original…')
    mudo = pasta / 'mudo.mp4'
    copia = subprocess.run([_ffmpeg(), '-y', '-i', str(video), '-an', '-c:v', 'copy',
                            str(mudo)], capture_output=True, timeout=1800)
    if copia.returncode != 0:
        # alguns contêineres não aceitam cópia sem áudio: reencoda só o vídeo
        copia = subprocess.run([_ffmpeg(), '-y', '-i', str(video), '-an',
                                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
                                '-pix_fmt', 'yuv420p', str(mudo)],
                               capture_output=True, timeout=3600)
        if copia.returncode != 0:
            raise RuntimeError(copia.stderr.decode('utf-8', 'replace')[-400:])

    total = max(1.0, _duracao(video))
    for i, trecho in enumerate(trechos):
        _atualizar(etapa='falando', percent=20 + int(70 * i / max(1, len(trechos))),
                   mensagem=f'Recriando a fala {i + 1} de {len(trechos)}…')
        trecho['wav'] = str(_falar(trecho['texto'], pasta / f'fala_{i:03d}.wav', voz))

    _atualizar(etapa='mixando', percent=92, mensagem='Encaixando as falas no tempo…')
    audio = _juntar_audios(trechos, pasta, total)

    _atualizar(etapa='montando', percent=96, mensagem='Juntando com o vídeo…')
    destino = pasta / 'video.mp4'
    final = subprocess.run([_ffmpeg(), '-y', '-i', str(mudo), '-i', str(audio),
                            '-map', '0:v', '-map', '1:a', '-c:v', 'copy',
                            '-c:a', 'aac', '-b:a', '192k', '-shortest', str(destino)],
                           capture_output=True, timeout=1800)
    if final.returncode != 0:
        raise RuntimeError(final.stderr.decode('utf-8', 'replace')[-400:])

    texto = ' '.join(t['texto'] for t in trechos)
    meta = {
        'titulo': (texto[:80] + '…') if len(texto) > 80 else texto,
        'origem': video.name, 'voz': voz or 'padrão', 'modelo': modelo,
        'trechos': len(trechos), 'duracao': round(_duracao(destino), 1),
        'tamanho_mb': round(destino.stat().st_size / 1_048_576, 1),
        'texto': texto, 'quando': time.time(),
    }
    (pasta / 'dublado.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                        encoding='utf-8')
    return meta


def _rodar(origem: Path, ident: str, voz: str | None, modelo: str) -> None:
    pasta = SAIDA / ident
    try:
        meta = dublar(origem, pasta, voz, modelo)
        _atualizar(rodando=False, etapa='pronto', percent=100, erro=None, resultado=meta,
                   mensagem=f"Pronto: {meta['duracao']}s, {meta['trechos']} trecho(s).")
    except Exception as erro:  # noqa: BLE001
        try:
            if pasta.is_dir() and not (pasta / 'video.mp4').is_file():
                shutil.rmtree(pasta, ignore_errors=True)
        except OSError:
            pass
        _atualizar(rodando=False, etapa='erro', erro=str(erro),
                   mensagem=f'Falhou: {str(erro)[:200]}')


def iniciar(ident: str, voz: str | None = None, modelo: str = 'base') -> dict:
    origem = caminho_entrada(ident)
    if not origem:
        return {'iniciado': False, 'mensagem': 'Escolha um vídeo enviado.'}
    with _LOCK:
        if _ESTADO['rodando']:
            return {'iniciado': False, 'mensagem': 'Já existe uma dublagem em andamento.'}
        novo = time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:4]
        _ESTADO.update(rodando=True, etapa='preparando', percent=0, erro=None,
                       resultado=None, transcricao=None, mensagem='Preparando…')
    threading.Thread(target=_rodar, args=(origem, novo, voz, modelo), daemon=True,
                     name='dublar').start()
    return {'iniciado': True, 'id': novo, 'mensagem': 'Dublagem começou…'}


def lista() -> list[dict]:
    if not SAIDA.is_dir():
        return []
    itens = []
    for pasta in SAIDA.iterdir():
        meta, video = pasta / 'dublado.json', pasta / 'video.mp4'
        if not (meta.is_file() and video.is_file()):
            continue
        try:
            dados = json.loads(meta.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        dados.update({'id': pasta.name, 'video': f'/api/v1/ext/dublar/video/{pasta.name}'})
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

    video = SAIDA / ident / 'video.mp4'
    try:
        meta = json.loads((SAIDA / ident / 'dublado.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        meta = {}
    if not video.is_file():
        _atualizar_pub(rodando=False, ok=False, mensagem='Vídeo dublado não encontrado.')
        return
    titulo = str(meta.get('titulo') or '').strip() or 'vídeo dublado'
    # hashtags básicas: as do texto, se houver palavras longas; senão nenhuma
    tags = meta.get('hashtags') if isinstance(meta.get('hashtags'), list) else []
    try:
        _atualizar_pub(rodando=True, mensagem='Abrindo o TikTok Studio…', ok=None,
                       url=None, id=ident, conta=conta)
        resultado = ttl.publish_video(
            video, titulo[:90], '', tags,
            cover_path=_capa(video, SAIDA / ident / 'capa.jpg'),
            account=conta,
            progress=lambda texto: _atualizar_pub(mensagem=texto))
        _atualizar_pub(rodando=False, ok=True,
                       mensagem=resultado.get('message') or 'Publicado.',
                       url=resultado.get('url'))
    except Exception as erro:  # noqa: BLE001
        _atualizar_pub(rodando=False, ok=False, mensagem=f'Falhou: {str(erro)[:200]}')


def publicar(ident: str, conta: str | None = None) -> dict:
    if caminho_video(ident) is None:
        return {'iniciado': False, 'mensagem': 'Vídeo dublado não encontrado.'}
    with _LOCK_PUB:
        if _PUB['rodando']:
            return {'iniciado': False, 'mensagem': 'Já existe uma publicação em andamento.'}
        _PUB.update(rodando=True, mensagem='Preparando…', ok=None, url=None, id=ident,
                    conta=conta)
    threading.Thread(target=_publicar, args=(ident, conta), daemon=True,
                     name='publicar-dublado').start()
    return {'iniciado': True, 'mensagem': 'Publicando no TikTok…'}
