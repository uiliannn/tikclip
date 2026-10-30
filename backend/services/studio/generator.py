"""Gerar do zero: assunto -> roteiro -> imagens -> narração -> vídeo 1080x1920.

Peças locais, já validadas nesta máquina:
- imagens: ComfyUI em 127.0.0.1:8188, Z-Image Turbo (safetensors), 720x1280, 8 steps
- voz:     VoiceStudio em 127.0.0.1:3900/v1/audio/speech (compatível com a OpenAI)
- montagem: ffmpeg do próprio app (Ken Burns + legenda queimada + título)

Uso rápido (CLI):
    python -m backend.services.studio.generator "curiosidades do espaço" --duracao 40
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

COMFY = os.getenv('TIKCLIP_COMFY', 'http://127.0.0.1:8188')
VOZ_URL = os.getenv('TIKCLIP_VOZ', 'http://127.0.0.1:3900/v1/audio/speech')

MODELO = os.getenv('TIKCLIP_COMFY_MODELO', 'z_image_turbo_bf16.safetensors')
CLIP = os.getenv('TIKCLIP_COMFY_CLIP', 'qwen_3_4b.safetensors')
VAE = os.getenv('TIKCLIP_COMFY_VAE', 'ae.safetensors')
CLIP_TIPO = os.getenv('TIKCLIP_COMFY_CLIP_TIPO', 'qwen_image')
VOZ_MODELO = os.getenv('TIKCLIP_VOZ_MODELO', 'omnivoice')
VOZ = os.getenv('TIKCLIP_VOZ_ID', 'default')

LARGURA, ALTURA, FPS = 1080, 1920, 30
SEG_POR_CENA = 6.0

PROMPT_ROTEIRO = (
    'Você escreve roteiros para vídeos curtos de TikTok em português do Brasil. '
    'Faça um roteiro com exatamente {cenas} cenas para o assunto "{assunto}", '
    'totalizando cerca de {duracao} segundos de narração. '
    'Regras: cada cena tem cerca de {palavras_cena} palavras de narração (nem menos, nem '
    'muito mais), falada em ritmo natural, sem '
    'marcações, sem emojis e sem "cena 1"; e um prompt de imagem em inglês, visual e '
    'concreto e LITERAL: a imagem precisa mostrar exatamente o sujeito que a narração '
    'cita naquela mesma cena (se a fala é sobre um animal, a imagem é daquele animal), '
    'com sujeito, ambiente, luz e enquadramento vertical, sem texto '
    'dentro da imagem, sem logotipos. A primeira cena precisa fisgar em 3 segundos. '
    'Inclua também 5 hashtags em pt-BR, sem #, específicas do assunto (nada de viral, '
    'shorts ou video). '
    'Retorne APENAS JSON no formato: '
    '{{"titulo": "título curto em pt-BR", "hashtags": ["...", "..."], '
    '"cenas": [{{"narracao": "...", "prompt": "..."}}]}}'
)


# --------------------------------------------------------------------------
# utilitários
# --------------------------------------------------------------------------
def _ffmpeg() -> str:
    from backend.utils.ffmpeg_utils import get_ffmpeg_path
    return get_ffmpeg_path()


def _ffprobe() -> str | None:
    """Acha o ffprobe perto do ffmpeg, no runtime do app ou no PATH.

    O caminho de get_ffmpeg_path() nem sempre tem o ffprobe ao lado (a instalação
    antiga do AutoClip, por exemplo), então validamos antes de usar.
    """
    candidatos: list[Path] = []
    if os.getenv('TIKCLIP_FFPROBE_PATH'):
        candidatos.append(Path(os.environ['TIKCLIP_FFPROBE_PATH']))
    try:
        ff = Path(_ffmpeg())
        candidatos += [ff.with_name('ffprobe.exe'), ff.with_name('ffprobe')]
    except Exception:  # noqa: BLE001
        pass
    raiz = Path(__file__).resolve().parents[3]
    candidatos.append(raiz / 'runtime' / 'ffmpeg' / 'ffprobe.exe')
    achado = shutil.which('ffprobe')
    if achado:
        candidatos.append(Path(achado))
    for caminho in candidatos:
        try:
            if caminho.is_file():
                return str(caminho)
        except OSError:
            continue
    return None


def _rodar(comando: list[str], timeout: int = 600, cwd: Path | None = None) -> None:
    resultado = subprocess.run(comando, capture_output=True, timeout=timeout,
                               cwd=str(cwd) if cwd else None)
    if resultado.returncode != 0:
        erro = resultado.stderr.decode('utf-8', 'replace')[-1200:]
        raise RuntimeError(f'ffmpeg falhou: {erro}')


def _duracao(caminho: Path) -> float:
    sonda = _ffprobe()
    if sonda:
        saida = subprocess.run(
            [sonda, '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=nw=1:nk=1', str(caminho)],
            capture_output=True, timeout=120)
        try:
            return float(saida.stdout.decode().strip())
        except ValueError:
            pass
    # fallback: o próprio ffmpeg imprime "Duration: 00:00:03.45" no stderr
    saida = subprocess.run([_ffmpeg(), '-i', str(caminho)],
                           capture_output=True, timeout=120)
    texto = saida.stderr.decode('utf-8', 'replace')
    achado = re.search(r'Duration:\s*(\d+):(\d+):(\d+\.\d+)', texto)
    if achado:
        return int(achado.group(1)) * 3600 + int(achado.group(2)) * 60 + float(achado.group(3))
    return 0.0


def _http_json(url: str, corpo: dict | None = None, timeout: int = 120):
    dados = None if corpo is None else json.dumps(corpo).encode()
    pedido = urllib.request.Request(
        url, data=dados, method='POST' if dados else 'GET',
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(pedido, timeout=timeout) as resposta:
        return json.load(resposta)


# --------------------------------------------------------------------------
# 1. roteiro
# --------------------------------------------------------------------------
def cenas_por_duracao(duracao: float) -> int:
    return max(3, min(12, round(duracao / SEG_POR_CENA)))


def roteiro(assunto: str, duracao: float, cenas: int | None = None) -> dict:
    from backend.services.studio.intelligence import text_json
    n = cenas or cenas_por_duracao(duracao)
    # o OmniVoice fala ~3,1 palavras/s: pedir o total não funcionava (o modelo
    # escrevia "uma frase curta" por cena e o vídeo saía 25% mais curto)
    palavras_cena = max(8, round(duracao * 3.1 / n))
    prompt = PROMPT_ROTEIRO.format(cenas=n, assunto=assunto, duracao=int(duracao),
                                   palavras_cena=palavras_cena)
    resposta = text_json(prompt, {'assunto': assunto, 'duracao': duracao, 'cenas': n,
                                  'palavras_por_cena': palavras_cena})
    lista = [c for c in (resposta or {}).get('cenas') or [] if c.get('narracao')]
    if not lista:
        raise RuntimeError('A IA não devolveu cenas.')
    tags = []
    for tag in (resposta or {}).get('hashtags') or []:
        limpa = re.sub(r'[^0-9A-Za-zÀ-ÿ_]', '', str(tag)).strip()
        if 2 <= len(limpa) <= 30 and limpa.lower() not in {
                'viral', 'shorts', 'video', 'fyp', 'paravoce', 'brasil'}:
            tags.append(limpa)
    return {'titulo': str(resposta.get('titulo') or assunto)[:90],
            'hashtags': list(dict.fromkeys(tags))[:6],
            'cenas': lista[:n]}


# --------------------------------------------------------------------------
# 2. imagens (ComfyUI)
# --------------------------------------------------------------------------
def gerar_imagem(prompt: str, destino: Path, seed: int | None = None) -> Path:
    seed = seed if seed is not None else uuid.uuid4().int % 2 ** 31
    grafo = {
        '1': {'class_type': 'UNETLoader', 'inputs': {'unet_name': MODELO, 'weight_dtype': 'default'}},
        '2': {'class_type': 'CLIPLoader', 'inputs': {'clip_name': CLIP, 'type': CLIP_TIPO}},
        '3': {'class_type': 'VAELoader', 'inputs': {'vae_name': VAE}},
        '5': {'class_type': 'CLIPTextEncode', 'inputs': {'clip': ['2', 0], 'text': prompt}},
        '7': {'class_type': 'CLIPTextEncode',
              'inputs': {'clip': ['2', 0], 'text': 'texto, letras, marca d agua, deformado'}},
        '8': {'class_type': 'EmptyLatentImage',
              'inputs': {'width': 720, 'height': 1280, 'batch_size': 1}},
        '9': {'class_type': 'KSampler',
              'inputs': {'model': ['1', 0], 'positive': ['5', 0], 'negative': ['7', 0],
                         'latent_image': ['8', 0], 'seed': seed, 'steps': 8, 'cfg': 1.0,
                         'sampler_name': 'euler', 'scheduler': 'simple', 'denoise': 1.0}},
        '10': {'class_type': 'VAEDecode', 'inputs': {'samples': ['9', 0], 'vae': ['3', 0]}},
        '11': {'class_type': 'SaveImage',
               'inputs': {'images': ['10', 0], 'filename_prefix': 'tikclip/cena'}},
    }
    pedido = _http_json(COMFY + '/prompt', {'prompt': grafo}, timeout=60)
    ident = pedido.get('prompt_id')
    if not ident:
        raise RuntimeError(f'ComfyUI recusou o pedido: {pedido}')
    limite = time.time() + 300
    while time.time() < limite:
        time.sleep(1.5)
        try:
            historico = _http_json(f'{COMFY}/history/{ident}', timeout=30)
        except Exception:  # noqa: BLE001
            continue
        if ident not in historico:
            continue
        saidas = historico[ident].get('outputs') or {}
        imagens = [i for v in saidas.values() for i in (v.get('images') or [])]
        if not imagens:
            continue
        arq = imagens[0]
        rota = (f"{COMFY}/view?filename={arq['filename']}"
                f"&subfolder={arq.get('subfolder', '')}&type={arq.get('type', 'output')}")
        with urllib.request.urlopen(rota, timeout=120) as resposta:
            destino.write_bytes(resposta.read())
        return destino
    raise RuntimeError('ComfyUI não devolveu a imagem no tempo previsto')


# --------------------------------------------------------------------------
# 3. voz (VoiceStudio)
# --------------------------------------------------------------------------
def gerar_voz(texto: str, destino: Path, voz: str | None = None) -> Path:
    corpo = {'model': VOZ_MODELO, 'input': texto, 'voice': voz or VOZ,
             'response_format': 'wav'}
    pedido = urllib.request.Request(
        VOZ_URL, data=json.dumps(corpo).encode(), method='POST',
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(pedido, timeout=300) as resposta:
        dados = resposta.read()
    if len(dados) < 1000:
        raise RuntimeError('VoiceStudio devolveu áudio vazio')
    destino.write_bytes(dados)
    return destino


# --------------------------------------------------------------------------
# 4. montagem
# --------------------------------------------------------------------------
def _ass(cenas: list[dict], total: float, titulo: str) -> str:
    """Legenda por cena (2 linhas) + título no topo nos primeiros segundos."""
    def cor(hexa: str) -> str:
        hexa = hexa.lstrip('#')
        return f'&H{hexa[4:6]}{hexa[2:4]}{hexa[0:2]}&'

    cabecalho = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {LARGURA}
PlayResY: {ALTURA}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Legenda,Arial,50,{cor('#FFFFFF')},{cor('#000000')},{cor('#000000')},1,0,1,4,2,2,80,80,180,1
Style: Titulo,Arial Black,96,{cor('#FFE14D')},{cor('#000000')},{cor('#000000')},1,0,1,7,3,8,70,70,120,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    def tempo(s: float) -> str:
        s = max(0.0, s)
        return f'{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:05.2f}'

    linhas = []
    if titulo:
        linhas.append(f'Dialogue: 0,{tempo(0)},{tempo(4.5)},Titulo,,0,0,0,,{titulo.upper()}')
    for cena in cenas:
        texto = re.sub(r'\s+', ' ', str(cena.get('narracao') or '')).strip()
        if not texto:
            continue
        # quebra em blocos de até 78 caracteres e distribui o tempo da cena
        # proporcionalmente: linha longa estourava as laterais do vídeo
        palavras = texto.split()
        blocos, atual = [], ''
        for palavra in palavras:
            if len(atual) + len(palavra) + 1 <= 58:
                atual = f'{atual} {palavra}'.strip()
            else:
                if atual:
                    blocos.append(atual)
                atual = palavra
        if atual:
            blocos.append(atual)
        total_chars = sum(len(b) for b in blocos) or 1
        cursor = cena['inicio']
        duracao_cena = max(0.4, cena['fim'] - cena['inicio'])
        for bloco in blocos:
            fatia = duracao_cena * len(bloco) / total_chars
            linhas.append(f'Dialogue: 0,{tempo(cursor)},{tempo(cursor + fatia)},'
                          f'Legenda,,0,0,0,,{bloco}')
            cursor += fatia
    return cabecalho + '\n'.join(linhas) + '\n'


def _cena_video(imagem: Path, audio: Path, destino: Path, indice: int) -> float:
    """Imagem com Ken Burns pelo tempo do áudio; devolve a duração resultante."""
    dur = max(1.0, _duracao(audio) + 0.35)
    quadros = max(1, int(round(dur * FPS)))
    # alterna aproximar e afastar para o corte não ficar monótono
    if indice % 2 == 0:
        zoom = "min(zoom+0.0012,1.18)"
    else:
        zoom = "if(lte(zoom,1.0),1.18,max(1.001,zoom-0.0012))"
    filtro = (
        f"scale={LARGURA * 2}:{ALTURA * 2}:force_original_aspect_ratio=increase,"
        f"crop={LARGURA * 2}:{ALTURA * 2},"
        f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={quadros}:s={LARGURA}x{ALTURA}:fps={FPS},"
        f"setsar=1"
    )
    _rodar([_ffmpeg(), '-y', '-loop', '1', '-i', str(imagem), '-i', str(audio),
            '-filter_complex', f'[0:v]{filtro}[v]',
            '-map', '[v]', '-map', '1:a',
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-ac', '2',
            '-t', f'{dur:.3f}', '-shortest', str(destino)])
    return dur


def _cena_video_fundo(fundo: Path, audio: Path, destino: Path, indice: int,
                      total: int, duracao_total: float) -> float:
    """Recorta o vídeo de fundo pelo tempo da narração, em 9:16, com áudio da voz.

    Cada cena pega um pedaço diferente do fundo (offset distribuído), para o vídeo
    não repetir o mesmo trecho em todas as cenas.
    """
    dur = max(1.0, _duracao(audio) + 0.35)
    fundo_dur = max(1.0, _duracao(fundo))
    # distribui os inícios ao longo do fundo; se ele for curto, o -stream_loop cobre
    passo = max(0.0, (fundo_dur - dur) / max(1, total))
    inicio = min(max(0.0, indice * passo), max(0.0, fundo_dur - dur))
    filtro = (
        f"scale={LARGURA}:{ALTURA}:force_original_aspect_ratio=increase,"
        f"crop={LARGURA}:{ALTURA},setsar=1"
    )
    _rodar([_ffmpeg(), '-y',
            '-ss', f'{inicio:.3f}', '-stream_loop', '-1', '-i', str(fundo),
            '-i', str(audio),
            '-filter_complex', f'[0:v]{filtro}[v]',
            '-map', '[v]', '-map', '1:a',
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
            '-r', str(FPS), '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-ac', '2',
            '-t', f'{dur:.3f}', '-shortest', str(destino)])
    return dur


def montar(cenas: list[dict], pasta: Path, destino: Path, titulo: str = '',
           fundo: Path | None = None) -> Path:
    pasta = Path(pasta)
    destino = Path(destino)
    partes = []
    total = 0.0
    inicio = 0.0
    soma_fundo = sum(max(1.0, float(c.get('duracao') or 1)) for c in cenas) if fundo else 0.0
    for i, cena in enumerate(cenas):
        if fundo:
            tempo_cena = _cena_video_fundo(Path(fundo), Path(cena['audio']),
                                           pasta / f'cena_{i:02d}.mp4', i, len(cenas),
                                           soma_fundo)
        else:
            tempo_cena = _cena_video(Path(cena['imagem']), Path(cena['audio']),
                                     pasta / f'cena_{i:02d}.mp4', i)
        cena['inicio'] = inicio
        cena['fim'] = inicio + tempo_cena
        inicio += tempo_cena
        total += tempo_cena
        partes.append(pasta / f'cena_{i:02d}.mp4')

    lista = pasta / 'partes.txt'
    lista.write_text(''.join(f"file '{p.as_posix()}'\n" for p in partes), encoding='utf-8')
    bruto = pasta / 'bruto.mp4'
    _rodar([_ffmpeg(), '-y', '-f', 'concat', '-safe', '0', '-i', str(lista),
            '-c', 'copy', str(bruto)])

    legenda = pasta / 'legenda.ass'
    legenda.write_text(_ass(cenas, total, titulo), encoding='utf-8')
    # roda de dentro da pasta com nome relativo: no filtro do ffmpeg o "C:" de um
    # caminho absoluto é lido como separador de opção e o ass falha
    _rodar([_ffmpeg(), '-y', '-i', bruto.name, '-vf', f'ass={legenda.name}',
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
            '-c:a', 'copy', destino.name], cwd=pasta)
    return destino


# --------------------------------------------------------------------------
# 5. pipeline
# --------------------------------------------------------------------------
def servico_no_ar(url: str, rota: str) -> bool:
    import urllib.error

    try:
        with urllib.request.urlopen(url + rota, timeout=3) as resposta:
            return resposta.status < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


def exigir_servicos() -> None:
    """Confere ComfyUI e VoiceStudio; erro claro em vez de 'sem conexão'."""
    faltando = []
    if not servico_no_ar(COMFY, '/system_stats'):
        faltando.append(f'ComfyUI (porta {COMFY.rsplit(":", 1)[-1]})')
    if not servico_no_ar(VOZ_URL.split('/v1/')[0], '/system/info'):
        faltando.append('VoiceStudio (porta 3900)')
    if faltando:
        raise RuntimeError(
            'Ferramenta fechada: ' + ', '.join(faltando) +
            '. Abra e tente de novo — sem ela não dá para gerar imagem/voz.')


def gerar(assunto: str, duracao: float, pasta: Path | None = None,
          cenas: int | None = None, voz: str | None = None,
          progresso=None, fundo: str | None = None) -> dict:
    """Roda o pipeline inteiro. `progresso` recebe dicts com etapa/cena/mensagem."""
    def avisar(**campos):
        if progresso:
            try:
                progresso(campos)
            except Exception:  # noqa: BLE001 - progresso nunca derruba a geração
                pass

    exigir_servicos()
    pasta = Path(pasta) if pasta else Path(os.environ['TEMP']) / f'tikclip_gerar_{int(time.time())}'
    pasta.mkdir(parents=True, exist_ok=True)
    inicio = time.time()

    avisar(etapa='roteiro', mensagem='Escrevendo o roteiro…')
    plano = roteiro(assunto, duracao, cenas)
    total = len(plano['cenas'])
    avisar(etapa='cenas', total=total, mensagem=f'{total} cenas: "{plano["titulo"]}"')
    print(f'  roteiro: {total} cenas | "{plano["titulo"]}"', flush=True)

    caminho_fundo = Path(fundo) if fundo else None
    if caminho_fundo and not caminho_fundo.is_file():
        raise RuntimeError(f'Vídeo de fundo não encontrado: {fundo}')

    for i, cena in enumerate(plano['cenas']):
        if not caminho_fundo:
            avisar(etapa='cena', cena=i + 1, total=total,
                   mensagem=f'Cena {i + 1} de {total}: imagem')
            imagem = gerar_imagem(str(cena.get('prompt') or assunto), pasta / f'img_{i:02d}.png')
            cena['imagem'] = str(imagem)
        avisar(etapa='cena', cena=i + 1, total=total,
               mensagem=f'Cena {i + 1} de {total}: narração')
        audio = gerar_voz(str(cena['narracao']), pasta / f'voz_{i:02d}.wav', voz)
        cena['audio'] = str(audio)
        cena['duracao'] = round(_duracao(audio), 2)
        print(f'    cena {i + 1}: {"vídeo de fundo" if caminho_fundo else "imagem ok"} | '
              f'voz {cena["duracao"]}s', flush=True)

    avisar(etapa='montagem',
           mensagem='Montando o vídeo (fundo, legenda, título)…' if caminho_fundo
           else 'Montando o vídeo (Ken Burns, legenda, título)…')
    saida = pasta / 'video.mp4'
    montar(plano['cenas'], pasta, saida, plano['titulo'], caminho_fundo)
    final = _duracao(saida)
    plano.update({'arquivo': str(saida), 'duracao': round(final, 2),
                  'pasta': str(pasta), 'segundos_gastos': round(time.time() - inicio, 1),
                  'assunto': assunto, 'criado_em': time.time(),
                  'fundo': caminho_fundo.name if caminho_fundo else None})
    # metadados ao lado do vídeo: a galeria do app lê daqui
    (pasta / 'gerado.json').write_text(
        json.dumps({k: plano.get(k) for k in ('titulo', 'hashtags', 'assunto', 'duracao',
                                              'segundos_gastos', 'criado_em', 'fundo')},
                   ensure_ascii=False, indent=2),
        encoding='utf-8')
    avisar(etapa='pronto', mensagem=f'Vídeo pronto: {final:.1f}s', resultado=plano)
    print(f'  vídeo: {saida} | {final:.1f}s | {plano["segundos_gastos"]}s de processamento',
          flush=True)
    return plano


if __name__ == '__main__':
    assunto = sys.argv[1] if len(sys.argv) > 1 else 'curiosidades do espaço'
    dur = float(sys.argv[2]) if len(sys.argv) > 2 else 24.0
    gerar(assunto, dur)
