"""Serviço de Tradução, Dublagem e Legendagem de Vídeos do TikClip.

Fluxo completo:
1. Extração do áudio original (16kHz mono).
2. Transcrição com timestamp preciso usando faster-whisper local.
3. Tradução do conteúdo para Português (PT-BR) preservando contexto.
4. Geração de falas dubladas com VoiceStudio (OmniVoice porta 3900) ou voz neural brasileira (Edge-TTS).
5. Mixagem das falas com atraso temporal exato (adelay) e ducking do áudio original de fundo.
6. Geração e queima de legendas estilizadas em português no vídeo final (estilo TikTok/Shorts).
7. Registro do vídeo dublado para visualização e publicação direta no TikTok e YouTube.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("tikclip.video_translator")

# Diretórios base
DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "TikClip"
DUBLADOS_DIR = DATA_DIR / "dublados"
DUBLADOS_DIR.mkdir(parents=True, exist_ok=True)

TIKTOK_DOWNLOADS_DIR = Path(r"C:\Users\Uilian\.gemini\antigravity\scratch\tiktok-downloader-web\downloads")

VOICESTUDIO_URL = os.getenv("TIKCLIP_VOZ", "http://127.0.0.1:3900/v1/audio/speech")

_JOB_LOCK = threading.Lock()
_CURRENT_JOB: dict[str, Any] = {
    "running": False,
    "job_id": None,
    "percent": 0,
    "step": "idle",
    "message": "Nenhuma dublagem em andamento.",
    "error": None,
    "result": None,
}


def _ffmpeg_path() -> str:
    from youtube_local_publisher import _get_ffmpeg_paths
    ffmpeg, _ = _get_ffmpeg_paths()
    return ffmpeg


def _ensure_whisper() -> Path:
    rt = DATA_DIR / "whisper-runtime"
    if str(rt) not in sys.path:
        sys.path.insert(0, str(rt))
    models_dir = DATA_DIR / "whisper-models"
    models_dir.mkdir(parents=True, exist_ok=True)
    return models_dir


def is_voicestudio_online() -> bool:
    """Verifica se o VoiceStudio (porta 3900) está rodando e pronto."""
    try:
        base_url = VOICESTUDIO_URL.split("/v1/")[0]
        req = urllib.request.Request(f"{base_url}/system/info", headers={"User-Agent": "TikClip"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def list_available_voices() -> list[dict[str, Any]]:
    """Lista vozes disponíveis para dublagem em português."""
    voices = []
    # 1. VoiceStudio
    vs_online = is_voicestudio_online()
    voices.append({
        "id": "voicestudio",
        "name": "VoiceStudio (Local OmniVoice)",
        "engine": "voicestudio",
        "gender": "Customizável",
        "description": "Motor local do VoiceStudio na porta 3900",
        "available": vs_online,
        "is_default": vs_online,
    })

    # 2. Vozes Neurais Brasileiras (Microsoft / Edge-TTS)
    voices.extend([
        {
            "id": "pt-BR-AntonioNeural",
            "name": "Antonio (Brasileiro - Masculino Natural)",
            "engine": "edge_tts",
            "gender": "Masculino",
            "description": "Voz masculina dinâmica e enérgica, ideal para cortes e games",
            "available": True,
            "is_default": not vs_online,
        },
        {
            "id": "pt-BR-FranciscaNeural",
            "name": "Francisca (Brasileira - Feminina Clara)",
            "engine": "edge_tts",
            "gender": "Feminino",
            "description": "Voz feminina clara e profissional, ótima para tutoriais e notícias",
            "available": True,
            "is_default": False,
        },
        {
            "id": "pt-BR-ThalitaNeural",
            "name": "Thalita (Brasileira - Jovem & Espontânea)",
            "engine": "edge_tts",
            "gender": "Feminino",
            "description": "Voz jovem e espontânea, ideal para vlogs e estilo TikTok",
            "available": True,
            "is_default": False,
        },
        {
            "id": "pt-BR-ValerioNeural",
            "name": "Valério (Brasileiro - Grave & Confiante)",
            "engine": "edge_tts",
            "gender": "Masculino",
            "description": "Voz grave e firme, estilo narrador de documentário",
            "available": True,
            "is_default": False,
        },
    ])
    return voices


def get_current_job_status() -> dict[str, Any]:
    with _JOB_LOCK:
        return dict(_CURRENT_JOB)


def _update_job(**kwargs):
    with _JOB_LOCK:
        _CURRENT_JOB.update(kwargs)
    if "message" in kwargs:
        logger.info("[Video Translator] %s", kwargs["message"])


def translate_text(text: str) -> str:
    """Traduz texto de qualquer idioma para Português (PT-BR) via API rápida."""
    clean = text.strip()
    if not clean:
        return ""
    try:
        url = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=pt&dt=t&q=" + urllib.parse.quote(clean)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            translated = "".join([part[0] for part in data[0] if part[0]])
            return translated.strip() or clean
    except Exception as exc:
        logger.warning("Falha ao traduzir trecho (%s): %s", clean[:30], exc)
        return clean


def _generate_audio_segment(text: str, output_path: Path, voice_id: str) -> Path:
    """Gera o arquivo de áudio de uma frase usando VoiceStudio ou Edge-TTS."""
    # Se for VoiceStudio
    if voice_id == "voicestudio" and is_voicestudio_online():
        try:
            body = {
                "model": "omnivoice",
                "input": text,
                "voice": "default",
                "response_format": "wav",
            }
            req = urllib.request.Request(
                VOICESTUDIO_URL,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
            if len(data) >= 1000:
                output_path.write_bytes(data)
                return output_path
        except Exception as exc:
            logger.warning("VoiceStudio falhou, usando voz neural alternativa: %s", exc)

    # Edge-TTS (Garantido e 100% gratuito)
    import edge_tts
    actual_voice = voice_id if voice_id.startswith("pt-BR") else "pt-BR-AntonioNeural"

    async def _run():
        tts = edge_tts.Communicate(text, actual_voice)
        await tts.save(str(output_path))

    asyncio.run(_run())
    return output_path


def dub_and_translate_video(
    video_path: Path | str,
    voice_id: str = "pt-BR-AntonioNeural",
    ducking_volume: float = 0.15,
    burn_subtitles: bool = True,
    custom_title: str | None = None,
    progress_callback: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    """Executa a dublagem e legendagem completa de um vídeo baixado.
    
    Retorna metadados do vídeo dublado e o caminho final.
    """
    v_path = Path(video_path).resolve()
    if not v_path.is_file():
        raise FileNotFoundError(f"Vídeo de entrada não encontrado: {video_path}")

    ffmpeg = _ffmpeg_path()
    models_dir = _ensure_whisper()
    from faster_whisper import WhisperModel

    job_id = f"dub_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    work_dir = DATA_DIR / "temp_dubbing" / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    def _prog(pct: int, msg: str):
        _update_job(percent=pct, message=msg)
        if progress_callback:
            try:
                progress_callback(pct, msg)
            except Exception:
                pass

    _update_job(running=True, job_id=job_id, percent=5, step="starting", message="Iniciando processo de dublagem...")

    try:
        # 1. Extração de áudio
        _prog(10, "1/6 Extraindo faixa de áudio original do vídeo...")
        wav_orig = work_dir / "audio_orig.wav"
        res_ext = subprocess.run(
            [ffmpeg, "-y", "-i", str(v_path), "-vn", "-ar", "16000", "-ac", "1", str(wav_orig)],
            capture_output=True,
            text=True,
        )
        if res_ext.returncode != 0 or not wav_orig.is_file():
            raise RuntimeError(f"Falha ao extrair áudio: {res_ext.stderr[-300:]}")

        # 2. Transcrição com faster-whisper
        _prog(20, "2/6 Ouvindo falas e identificando timestamps com Whisper...")
        model = WhisperModel("base", device="cpu", compute_type="int8", download_root=str(models_dir))
        segments, info = model.transcribe(str(wav_orig), beam_size=2)
        segments_list = list(segments)

        if not segments_list:
            raise RuntimeError("Nenhuma fala detectada no vídeo para dublar.")

        _prog(35, f"3/6 Idioma '{info.language}' detectado ({len(segments_list)} frases). Traduzindo para Português...")
        translated_segments = []
        for idx, s in enumerate(segments_list):
            orig = s.text.strip()
            trans = translate_text(orig)
            translated_segments.append({
                "id": idx,
                "start": float(s.start),
                "end": float(s.end),
                "orig": orig,
                "trans": trans,
            })

        # 3. Geração de falas dubladas
        total_segs = len(translated_segments)
        _prog(45, f"4/6 Gerando dublagem com {voice_id} (0/{total_segs})...")
        for idx, item in enumerate(translated_segments):
            pct = 45 + int(25 * (idx / max(1, total_segs)))
            _prog(pct, f"4/6 Sintetizando fala em português ({idx+1}/{total_segs}): \"{item['trans'][:30]}...\"")
            audio_part = work_dir / f"fala_{idx:03d}.mp3"
            _generate_audio_segment(item["trans"], audio_part, voice_id)
            item["audio_file"] = str(audio_part)

        # 4. Mixagem das falas com atraso temporal exato e ducking
        _prog(75, "5/6 Mixando trilha de voz dublada com fundo sonoro...")
        cmd_mix = [ffmpeg, "-y"]
        filters = []
        for i, item in enumerate(translated_segments):
            cmd_mix += ["-i", item["audio_file"]]
            delay_ms = int(item["start"] * 1000)
            filters.append(f"[{i}:a]aresample=48000,adelay={delay_ms}|{delay_ms}[a{i}]")

        soma = "".join(f"[a{i}]" for i in range(total_segs))
        filters.append(f"{soma}amix=inputs={total_segs}:duration=longest:normalize=0[dub_voice]")

        # Mixagem com áudio original (ducking)
        if ducking_volume > 0.01:
            cmd_mix += ["-i", str(v_path)]
            orig_idx = total_segs
            filters.append(f"[{orig_idx}:a]volume={ducking_volume:.2f}[orig_low]")
            filters.append(f"[orig_low][dub_voice]amix=inputs=2:duration=first:normalize=0[final_audio]")
        else:
            filters.append(f"[dub_voice]anull[final_audio]")

        dubbed_audio = work_dir / "audio_final.wav"
        cmd_mix += [
            "-filter_complex", ";".join(filters),
            "-map", "[final_audio]",
            "-ac", "2",
            "-ar", "48000",
            str(dubbed_audio)
        ]
        res_mix = subprocess.run(cmd_mix, capture_output=True, text=True)
        if res_mix.returncode != 0:
            raise RuntimeError(f"Erro na mixagem de áudio: {res_mix.stderr[-300:]}")

        # 5. Geração de arquivo de legendas .srt
        srt_file = work_dir / "legendas.srt"
        with open(srt_file, "w", encoding="utf-8") as f:
            for item in translated_segments:
                def _fmt(t):
                    h = int(t // 3600)
                    m = int((t % 3600) // 60)
                    s = int(t % 60)
                    ms = int((t - int(t)) * 1000)
                    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
                f.write(f"{item['id']+1}\n{_fmt(item['start'])} --> {_fmt(item['end'])}\n{item['trans']}\n\n")

        # 6. Renderização do vídeo final
        _prog(85, "6/6 Renderizando vídeo final com voz dublada e legendas...")
        dest_filename = f"dublado_{v_path.stem}.mp4"
        dest_video = DUBLADOS_DIR / dest_filename

        cmd_final = [
            ffmpeg, "-y",
            "-i", str(v_path),
            "-i", str(dubbed_audio),
        ]

        if burn_subtitles:
            srt_escaped = str(srt_file).replace("\\", "/").replace(":", "\\:")
            # Estilo clássico TikTok / Shorts: texto branco com borda preta sólida, centralizado embaixo
            vf = f"subtitles='{srt_escaped}':force_style='FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=3,Outline=2,Shadow=1,MarginV=60,Alignment=2'"
            cmd_final += ["-vf", vf]

        cmd_final += [
            "-map", "0:v",
            "-map", "1:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            str(dest_video)
        ]
        res_final = subprocess.run(cmd_final, capture_output=True, text=True)
        if res_final.returncode != 0 or not dest_video.is_file():
            raise RuntimeError(f"Erro ao renderizar vídeo: {res_final.stderr[-300:]}")

        # Também salvamos uma cópia na pasta do criador em downloads/@dublados para aparecer nas listas
        pub_folder = TIKTOK_DOWNLOADS_DIR / "@dublados"
        pub_folder.mkdir(parents=True, exist_ok=True)
        pub_video = pub_folder / dest_filename
        shutil.copy2(dest_video, pub_video)

        # Copia a capa original se existir
        orig_covers = list(v_path.parent.glob(f"{v_path.stem.split('_')[0]}*.jpg"))
        if orig_covers:
            shutil.copy2(orig_covers[0], pub_folder / f"dublado_{v_path.stem}.jpg")

        # Gera .info.txt em português
        full_trans = " ".join(item["trans"] for item in translated_segments)
        title_pt = custom_title or (translated_segments[0]["trans"] if translated_segments else v_path.stem)
        info_lines = [
            "=" * 50,
            f"VÍDEO DUBLADO EM PORTUGUÊS",
            f"VOZ: {voice_id}",
            f"VÍDEO ORIGINAL: {v_path.name}",
            "=" * 50,
            "",
            "--- DESCRIÇÃO DO VÍDEO (TRADUZIDA) ---",
            title_pt,
            "",
            full_trans,
            "",
            "--- HASHTAGS EXTRAÍDAS ---",
            "#Shorts #dublado #brasil #viral #foryou",
            "",
            "=" * 50,
        ]
        (pub_folder / f"dublado_{v_path.stem}.info.txt").write_text("\n".join(info_lines), encoding="utf-8")

        # Limpeza dos arquivos temporários
        shutil.rmtree(work_dir, ignore_errors=True)

        result_payload = {
            "ok": True,
            "status": "concluido",
            "video_path": str(dest_video),
            "pub_video_path": str(pub_video),
            "filename": dest_filename,
            "title": title_pt,
            "duration_s": round(translated_segments[-1]["end"], 1) if translated_segments else 0,
            "size_mb": round(dest_video.stat().st_size / (1024 * 1024), 2),
            "video_url": f"/media/tiktok-downloads/%40dublados/{urllib.parse.quote(dest_filename)}",
            "translated_segments": translated_segments,
        }

        _update_job(
            running=False,
            percent=100,
            step="completed",
            message=f"Vídeo dublado com sucesso! Arquivo salvo ({result_payload['size_mb']} MB) [OK]",
            result=result_payload,
        )
        return result_payload

    except Exception as exc:
        err_msg = str(exc)
        logger.exception("Falha no processo de dublagem: %s", exc)
        _update_job(running=False, percent=0, step="error", error=err_msg, message=f"Erro na dublagem: {err_msg}")
        # Limpeza em caso de falha
        shutil.rmtree(work_dir, ignore_errors=True)
        raise exc


def list_dubbed_videos() -> list[dict[str, Any]]:
    """Lista todos os vídeos que já foram dublados e traduzidos."""
    results = []
    pub_folder = TIKTOK_DOWNLOADS_DIR / "@dublados"
    if not pub_folder.is_dir():
        return []

    for mp4 in sorted(pub_folder.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        st = mp4.stat()
        info_file = mp4.with_suffix(".info.txt")
        desc = ""
        if info_file.is_file():
            try:
                txt = info_file.read_text(encoding="utf-8", errors="ignore")
                spl = re.split(r"--- DESCRI[ÇC][ÃA]O DO V[ÍI]DEO.*---", txt)
                if len(spl) > 1:
                    desc = spl[1].split("--- HASHTAGS")[0].strip()
            except Exception:
                pass

        cover = None
        for ext in (".jpg", ".jpeg", ".png"):
            cov = mp4.with_suffix(ext)
            if cov.is_file():
                cover = f"/media/tiktok-downloads/%40dublados/{urllib.parse.quote(cov.name)}"
                break

        results.append({
            "filename": mp4.name,
            "video_path": str(mp4.resolve()),
            "video_url": f"/media/tiktok-downloads/%40dublados/{urllib.parse.quote(mp4.name)}",
            "cover_url": cover,
            "title": desc or mp4.stem.replace("dublado_", ""),
            "size_mb": round(st.st_size / (1024 * 1024), 1),
            "created_at": time.strftime("%d/%m/%Y %H:%M", time.localtime(st.st_mtime)),
        })
    return results
