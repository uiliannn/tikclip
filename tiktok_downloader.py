"""Módulo de Download de Vídeos e Canais do TikTok para o TikClip.

Suporta:
- Links de vídeos individuais (ex: tiktok.com/@user/video/123456... ou vm.tiktok.com/...)
- Perfis/Canais de usuários (ex: @podeperguntar62 ou tiktok.com/@username)
- Tópicos de inspiração / Trending Topics
- Fallback automático via TikWM API caso o yt-dlp enfrente desafios de extração
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("tikclip.tiktok_downloader")

# Diretório padrão de downloads do TikTok
BASE_DOWNLOADS_DIR = Path(r"C:\Users\Uilian\.gemini\antigravity\scratch\tiktok-downloader-web\downloads")
BASE_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

_DOWNLOAD_LOCK = threading.Lock()
_CURRENT_TASK: Dict[str, Any] = {
    "running": False,
    "task_id": None,
    "target": "",
    "status": "idle",
    "percent": 0.0,
    "message": "Nenhum download em andamento.",
    "speed": "",
    "eta": "",
    "logs": [],
    "downloaded_files": [],
    "creator_folder": None,
    "error": None,
}


def get_download_status() -> Dict[str, Any]:
    with _DOWNLOAD_LOCK:
        return dict(_CURRENT_TASK)


def _update_task(**kwargs):
    with _DOWNLOAD_LOCK:
        _CURRENT_TASK.update(kwargs)
    if "message" in kwargs:
        logger.info("[TikTok Downloader] %s", kwargs["message"])


def extract_hashtags(text: str) -> List[str]:
    if not text:
        return []
    tags = re.findall(r'#[\w\d_À-ÿ]+', text)
    seen = set()
    res = []
    for t in tags:
        tl = t.lower()
        if tl not in seen:
            seen.add(tl)
            res.append(t)
    return res


def save_video_metadata(filepath: Path, info: Dict[str, Any]) -> Path:
    title = info.get("title", "")
    description = info.get("description", "") or title
    webpage_url = info.get("webpage_url", "")
    uploader = info.get("uploader", "")
    upload_date = info.get("upload_date", "")

    raw_tags = info.get("tags") or []
    extracted = extract_hashtags(description)
    all_hashtags = list(dict.fromkeys(extracted + [f"#{t}" if not t.startswith("#") else t for t in raw_tags]))

    meta_file = filepath.with_suffix(".info.txt")
    content = [
        "=" * 50,
        f"CANAL: @{uploader}",
        f"DATA DE PUBLICAÇÃO: {upload_date}",
        f"LINK ORIGINAL: {webpage_url}",
        "=" * 50,
        "",
        "--- DESCRIÇÃO DO VÍDEO ---",
        description.strip(),
        "",
        "--- HASHTAGS EXTRAÍDAS ---",
        " ".join(all_hashtags) if all_hashtags else "Nenhuma hashtag encontrada.",
        "",
        "=" * 50
    ]
    with open(meta_file, "w", encoding="utf-8") as f:
        f.write("\n".join(content))
    return meta_file


def _download_via_tikwm_video(video_url: str, output_folder: Path) -> Optional[Path]:
    """Fallback usando API pública TikWM para vídeos individuais."""
    try:
        api_url = f"https://www.tikwm.com/api/?url={urllib.parse.quote(video_url)}"
        req = urllib.request.Request(api_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if data.get("code") != 0 or not data.get("data"):
            return None

        vdata = data["data"]
        vid_id = vdata.get("id") or str(int(time.time()))
        play_url = vdata.get("play") or vdata.get("wmplay")
        title = vdata.get("title") or "video_tiktok"
        clean_title = re.sub(r'[\\/*?:"<>|]', "", title)[:40].strip() or "video"
        author = (vdata.get("author") or {}).get("unique_id") or "tiktok"

        target_folder = output_folder if output_folder else BASE_DOWNLOADS_DIR / f"@{author}"
        target_folder.mkdir(parents=True, exist_ok=True)

        vid_path = target_folder / f"{vid_id}_{clean_title}.mp4"

        # Baixa vídeo
        req_v = urllib.request.Request(play_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req_v, timeout=60) as resp_v:
            vid_path.write_bytes(resp_v.read())

        # Baixa capa se disponível
        cover_url = vdata.get("cover")
        if cover_url:
            try:
                req_c = urllib.request.Request(cover_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req_c, timeout=20) as resp_c:
                    (target_folder / f"{vid_id}_{clean_title}.jpg").write_bytes(resp_c.read())
            except Exception:
                pass

        # Salva info.txt
        save_video_metadata(vid_path, {
            "title": title,
            "description": title,
            "webpage_url": video_url,
            "uploader": author,
            "upload_date": datetime_fmt(vdata.get("create_time")),
        })
        return vid_path
    except Exception as exc:
        logger.warning("Falha no download via TikWM: %s", exc)
        return None


def datetime_fmt(ts) -> str:
    if not ts:
        return ""
    try:
        from datetime import datetime
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def start_download(
    target: str,
    max_videos: int = 5,
    folder_name: Optional[str] = None
) -> str:
    """Inicia uma tarefa de download do TikTok em background."""
    raw = target.strip()
    if not raw:
        raise ValueError("Informe um link ou nome de usuário do TikTok.")

    task_id = f"tt_dl_{int(time.time())}"
    _update_task(
        running=True,
        task_id=task_id,
        target=raw,
        status="iniciando",
        percent=5.0,
        message=f"Iniciando download para: {raw}...",
        speed="",
        eta="",
        logs=[f"Tarefa {task_id} iniciada para {raw}"],
        downloaded_files=[],
        creator_folder=None,
        error=None,
    )

    thread = threading.Thread(
        target=_run_download_worker,
        args=(task_id, raw, max_videos, folder_name),
        daemon=True
    )
    thread.start()
    return task_id


def _run_download_worker(task_id: str, target: str, max_videos: int, folder_override: Optional[str]):
    try:
        import yt_dlp

        # Normaliza target
        target_url = target
        channel_name = folder_override

        # Detecta se é link direto de vídeo
        is_single_video = bool(re.search(r'/video/\d+|vm\.tiktok\.com/|vt\.tiktok\.com/', target))

        if not target.startswith("http://") and not target.startswith("https://"):
            user = target.lstrip("@").strip().split("/")[0]
            target_url = f"https://www.tiktok.com/@{user}"
            if not channel_name:
                channel_name = f"@{user}"
        elif not channel_name:
            if "@" in target:
                u_part = target.split("@")[-1].split("/")[0].split("?")[0]
                channel_name = f"@{u_part}"
            else:
                channel_name = "@tiktok_videos"

        out_folder = BASE_DOWNLOADS_DIR / channel_name
        out_folder.mkdir(parents=True, exist_ok=True)
        _update_task(creator_folder=channel_name)

        _update_task(percent=15.0, status="baixando", message=f"Conectando ao TikTok e buscando vídeos...")

        downloaded_list = []

        def progress_hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes", 0)
                pct = round((done / total) * 100, 1) if total > 0 else 50.0
                speed = d.get("speed")
                sp_str = f"{speed / 1024 / 1024:.1f} MB/s" if speed else ""
                _update_task(
                    percent=max(15.0, min(95.0, pct)),
                    speed=sp_str,
                    message=f"Baixando: {pct:.0f}% ({sp_str})"
                )
            elif d.get("status") == "finished":
                fn = d.get("filename")
                if fn:
                    p = Path(fn)
                    downloaded_list.append(p.name)
                    # Gera metadados .info.txt
                    info_dict = d.get("info_dict") or {}
                    try:
                        save_video_metadata(p, info_dict)
                    except Exception:
                        pass
                    _update_task(
                        downloaded_files=list(downloaded_list),
                        message=f"Vídeo baixado: {p.name[:35]}..."
                    )

        ydl_opts = {
            "outtmpl": str(out_folder / "%(id)s_%(title).40s.%(ext)s"),
            "playlistend": max_videos if not is_single_video else 1,
            "ignoreerrors": True,
            "no_warnings": True,
            "quiet": True,
            "progress_hooks": [progress_hook],
            "format": "bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "writethumbnail": True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([target_url])
        except Exception as yt_err:
            logger.warning("yt-dlp falhou (%s), tentando fallback via TikWM...", yt_err)
            if is_single_video:
                f = _download_via_tikwm_video(target_url, out_folder)
                if f and f.is_file():
                    downloaded_list.append(f.name)

        if not downloaded_list:
            # Tenta fallback TikWM se nenhum arquivo foi baixado
            if is_single_video:
                f = _download_via_tikwm_video(target_url, out_folder)
                if f and f.is_file():
                    downloaded_list.append(f.name)

        if downloaded_list:
            _update_task(
                running=False,
                status="concluido",
                percent=100.0,
                message=f"Download finalizado com sucesso! {len(downloaded_list)} vídeo(s) salvo(s).",
                downloaded_files=downloaded_list,
                creator_folder=channel_name,
            )
        else:
            raise RuntimeError("Não foi possível extrair os vídeos do link informado. Verifique se o perfil ou vídeo é público.")

    except Exception as exc:
        logger.error("Erro no download do TikTok: %s", exc)
        _update_task(
            running=False,
            status="erro",
            percent=0.0,
            message=f"Erro ao baixar: {exc}",
            error=str(exc)
        )
