"""Módulo de Monitoramento e Automação de Canais do YouTube para TikClip Web.

Permite:
- Cadastrar canais do YouTube por @handle ou link (ex: @tiagotcar)
- Vincular CADA canal a uma conta ESPECÍFICA do TikTok (para jamais postar na conta errada)
- Configurar hashtags padrão do canal
- Detectar automaticamente novos vídeos postados no canal via yt-dlp
- Importar e gerar o projeto com cortes verticais (9:16) automaticamente
- Opcionalmente, publicar os cortes prontos automaticamente na conta vinculada
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("tikclip.channel_monitor")

YTDLP_PATH = Path(r"C:\Users\Uilian\Desktop\tikclip-studio\runtime\python\Scripts\yt-dlp.exe")


def _data_dir() -> Path:
    raw = os.getenv("TIKCLIP_DATA_DIR")
    if raw:
        return Path(raw)
    return Path(os.getenv("APPDATA", str(Path.home()))) / "TikClip"


def _channels_file() -> Path:
    return _data_dir() / "youtube_channels.json"


def _project_channels_file() -> Path:
    return _data_dir() / "project_channels.json"


_MONITOR_LOCK = threading.RLock()


def load_channels_data() -> dict[str, Any]:
    with _MONITOR_LOCK:
        path = _channels_file()
        default = {
            "channels": [],
            "settings": {
                "auto_check_enabled": True,
                "check_interval_minutes": 15,
                "last_global_check": 0.0,
            },
        }
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    if "channels" in data and isinstance(data["channels"], list):
                        default["channels"] = data["channels"]
                    if "settings" in data and isinstance(data["settings"], dict):
                        default["settings"].update(data["settings"])
            except Exception as exc:
                logger.warning(f"Erro ao ler youtube_channels.json: {exc}")
        return default


def save_channels_data(data: dict[str, Any]) -> None:
    with _MONITOR_LOCK:
        path = _channels_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


def load_project_channels() -> dict[str, Any]:
    with _MONITOR_LOCK:
        path = _project_channels_file()
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}


def save_project_channel_meta(project_id: str, meta: dict[str, Any]) -> None:
    with _MONITOR_LOCK:
        data = load_project_channels()
        data[project_id] = meta
        path = _project_channels_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


def get_project_channel_meta(project_id: str) -> Optional[dict[str, Any]]:
    with _MONITOR_LOCK:
        data = load_project_channels()
        return data.get(project_id)


def resolve_channel_metadata(url_or_handle: str) -> dict[str, Any]:
    """Usa yt-dlp para resolver informações do canal e obter a lista dos vídeos mais recentes."""
    raw = (url_or_handle or "").strip()
    if not raw:
        raise ValueError("URL ou handle do canal não informado.")

    # Normalizar handle ou URL
    if raw.startswith("@"):
        target_url = f"https://www.youtube.com/{raw}/videos"
    elif not raw.startswith("http://") and not raw.startswith("https://"):
        target_url = f"https://www.youtube.com/@{raw.lstrip('@')}/videos"
    else:
        # É uma URL completa
        if "/watch?v=" in raw:
            target_url = raw
        elif "/videos" not in raw and not raw.endswith("/streams") and not raw.endswith("/shorts"):
            target_url = raw.rstrip("/") + "/videos"
        else:
            target_url = raw

    ytdlp_exe = str(YTDLP_PATH)
    if not os.path.isfile(ytdlp_exe):
        ytdlp_exe = "yt-dlp"

    sem_janela = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
    cmd = [
        ytdlp_exe,
        "--flat-playlist",
        "--playlist-end",
        "5",
        "-J",
        "--no-warnings",
        "--extractor-args",
        "youtube:lang=pt",
        "--add-header",
        "Accept-Language:pt-BR,pt;q=0.9",
        target_url,
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            creationflags=sem_janela,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError("O yt-dlp demorou muito para responder sobre este canal.")

    if proc.returncode != 0 or not proc.stdout.strip():
        err_msg = proc.stderr.strip() if proc.stderr else "Canal não encontrado ou indisponível"
        raise RuntimeError(f"Erro ao buscar canal: {err_msg[:200]}")

    try:
        data = json.loads(proc.stdout)
    except Exception as exc:
        raise ValueError(f"Resposta inválida do yt-dlp: {exc}")

    # Extrair avatar com melhor resolução
    avatar_url = ""
    for thumb in data.get("thumbnails") or []:
        url = thumb.get("url", "")
        if "avatar" in thumb.get("id", "") or "yt3.googleusercontent.com" in url:
            avatar_url = url
            break
    if not avatar_url and data.get("thumbnails"):
        avatar_url = data["thumbnails"][-1].get("url", "")

    channel_name = data.get("channel") or data.get("uploader") or data.get("title") or "Canal YouTube"
    channel_name = channel_name.replace(" - Videos", "").strip()
    uploader_id = data.get("uploader_id") or ""
    if uploader_id and not uploader_id.startswith("@"):
        uploader_id = "@" + uploader_id

    channel_id = data.get("channel_id") or data.get("id") or uploader_id
    channel_url = data.get("channel_url") or data.get("uploader_url") or target_url

    # Extrair vídeos recentes da playlist
    entries = []
    for item in data.get("entries") or []:
        vid = item.get("id")
        title = item.get("title")
        if not vid or not title:
            continue
        vurl = item.get("url") or f"https://www.youtube.com/watch?v={vid}"
        duration = item.get("duration") or 0
        thumb = ""
        if item.get("thumbnails"):
            thumb = item["thumbnails"][0].get("url", "")
        entries.append({
            "id": vid,
            "title": title,
            "url": vurl,
            "duration": duration,
            "thumbnail": thumb,
        })

    return {
        "channel_id": channel_id,
        "name": channel_name,
        "handle": uploader_id,
        "channel_url": channel_url,
        "avatar_url": avatar_url,
        "latest_videos": entries,
    }


def add_channel(payload: dict[str, Any], server_port: int = 8585) -> dict[str, Any]:
    """Cadastra um novo canal no monitoramento com a conta do TikTok vinculada."""
    url_or_handle = payload.get("url", "").strip()
    tiktok_account = payload.get("tiktok_account", "").strip()
    tiktok_account_name = payload.get("tiktok_account_name", "").strip()
    hashtags = payload.get("hashtags", [])
    auto_publish = bool(payload.get("auto_publish", False))
    interval_min = int(payload.get("interval_min", 30))
    import_latest_now = bool(payload.get("import_latest_now", False))

    if not tiktok_account:
        raise ValueError("É obrigatório escolher a conta do TikTok para publicação deste canal.")

    meta = resolve_channel_metadata(url_or_handle)

    channels_data = load_channels_data()
    channels = channels_data["channels"]

    # Verificar se já existe cadastrado
    existing = next((c for c in channels if c.get("channel_id") == meta["channel_id"] or (meta["handle"] and c.get("handle") == meta["handle"])), None)
    
    cid = existing["id"] if existing else f"ch_{uuid.uuid4().hex[:10]}"
    seen_ids = existing.get("seen_video_ids", []) if existing else []

    # Se há vídeos mais recentes
    latest = meta["latest_videos"][0] if meta["latest_videos"] else None
    
    if not existing:
        # No cadastro inicial: se NÃO marcou para importar agora, marca os vídeos atuais como já vistos
        if not import_latest_now and latest:
            seen_ids = [v["id"] for v in meta["latest_videos"]]

    channel_entry = {
        "id": cid,
        "channel_id": meta["channel_id"],
        "name": meta["name"],
        "handle": meta["handle"],
        "channel_url": meta["channel_url"],
        "avatar_url": meta["avatar_url"],
        "tiktok_account": tiktok_account,
        "tiktok_account_name": tiktok_account_name or tiktok_account,
        "hashtags": hashtags if isinstance(hashtags, list) else [t.strip() for t in str(hashtags).replace(",", " ").split() if t.strip()],
        "auto_publish": auto_publish,
        "interval_min": max(1, interval_min),
        "active": True,
        "created_at": existing.get("created_at") if existing else time.time(),
        "last_checked_at": time.time(),
        "last_video_id": latest["id"] if latest else "",
        "last_video_title": latest["title"] if latest else "",
        "last_video_url": latest["url"] if latest else "",
        "seen_video_ids": seen_ids,
        "history": existing.get("history", []) if existing else [],
    }

    if existing:
        channels[channels.index(existing)] = channel_entry
    else:
        channels.append(channel_entry)

    save_channels_data(channels_data)

    # Se marcou para importar o mais recente agora mesmo
    imported_result = None
    if import_latest_now and latest:
        try:
            imported_result = import_channel_video(channel_entry, latest, server_port=server_port)
        except Exception as exc:
            logger.error(f"Erro ao importar vídeo recente no cadastro: {exc}")

    return {
        "channel": channel_entry,
        "imported_now": imported_result,
    }


def update_channel(channel_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    channels_data = load_channels_data()
    ch = next((c for c in channels_data["channels"] if c.get("id") == channel_id), None)
    if not ch:
        raise KeyError("Canal não encontrado.")

    for field in ("tiktok_account", "tiktok_account_name", "auto_publish", "interval_min", "active"):
        if field in updates:
            ch[field] = updates[field]

    if "hashtags" in updates:
        val = updates["hashtags"]
        ch["hashtags"] = val if isinstance(val, list) else [t.strip() for t in str(val).replace(",", " ").split() if t.strip()]

    save_channels_data(channels_data)
    return ch


def delete_channel(channel_id: str) -> bool:
    channels_data = load_channels_data()
    orig_len = len(channels_data["channels"])
    channels_data["channels"] = [c for c in channels_data["channels"] if c.get("id") != channel_id]
    if len(channels_data["channels"]) != orig_len:
        save_channels_data(channels_data)
        return True
    return False


def save_project_hashtags(project_id: str, tags: list[str]) -> None:
    if not tags:
        return
    proj_dir = _data_dir() / 'projects' / project_id
    ht_path = proj_dir / 'metadata' / 'hashtags-ext.json'
    ht_path.parent.mkdir(parents=True, exist_ok=True)
    clean_tags = [t.lstrip('#').strip().lower() for t in tags if t.strip()]
    ht_path.write_text(json.dumps(clean_tags, ensure_ascii=False), encoding='utf-8')


def import_channel_video(channel: dict[str, Any], video: dict[str, Any], server_port: int = 8585) -> dict[str, Any]:
    """Cria o projeto no TikClip Studio via HTTP import, anexa hashtags e vincula a conta do TikTok."""
    import httpx

    vid = video["id"]
    title = video.get("title") or "Vídeo Automático"
    vurl = video.get("url") or f"https://www.youtube.com/watch?v={vid}"

    logger.info(f"[Auto-Canal] Importando vídeo do canal {channel['name']}: {title} ({vurl})")

    # Nome legível do projeto
    proj_name = f"{channel['name']} - {title}"[:180]

    # POST /api/v1/studio/import
    import_url = f"http://127.0.0.1:{server_port}/api/v1/studio/import"
    data = {
        "goal": "auto",
        "language": "source",
        "aspect": "portrait",
        "duration": "75",
        "platforms": "tiktok",
        "auto_start": "true",
        "brand_outro_enabled": "false",
        "name": proj_name,
        "url": vurl,
    }

    try:
        resp = httpx.post(import_url, data=data, timeout=45.0)
        resp.raise_for_status()
        res_json = resp.json()
        pid = res_json.get("project_id")
    except Exception as exc:
        raise RuntimeError(f"Falha ao chamar API de importação: {exc}")

    if not pid:
        raise RuntimeError("A API de importação não retornou um ID de projeto.")

    # 1. Salvar hashtags padrão do canal no projeto
    if channel.get("hashtags"):
        try:
            save_project_hashtags(pid, channel["hashtags"])
        except Exception as exc:
            logger.warning(f"Erro ao salvar hashtags para {pid}: {exc}")

    # 2. Vincular a conta do TikTok exclusivamente neste projeto
    meta = {
        "channel_id": channel["id"],
        "channel_name": channel["name"],
        "channel_handle": channel.get("handle", ""),
        "tiktok_account": channel["tiktok_account"],
        "tiktok_account_name": channel.get("tiktok_account_name", channel["tiktok_account"]),
        "auto_publish": channel.get("auto_publish", False),
        "interval_min": channel.get("interval_min", 30),
        "hashtags": channel.get("hashtags", []),
        "video_id": vid,
        "video_title": title,
        "video_url": vurl,
        "imported_at": time.time(),
        "status": "processing",
    }
    save_project_channel_meta(pid, meta)

    # 3. Atualizar histórico do canal
    channels_data = load_channels_data()
    for ch in channels_data["channels"]:
        if ch.get("id") == channel["id"]:
            if vid not in ch.get("seen_video_ids", []):
                ch.setdefault("seen_video_ids", []).append(vid)
            ch["last_video_id"] = vid
            ch["last_video_title"] = title
            ch["last_video_url"] = vurl
            ch.setdefault("history", []).insert(0, {
                "video_id": vid,
                "video_title": title,
                "video_url": vurl,
                "project_id": pid,
                "imported_at": time.time(),
                "status": "processing",
                "tiktok_account": channel["tiktok_account"],
            })
            # Manter histórico em até 20 itens
            ch["history"] = ch["history"][:20]
            break
    save_channels_data(channels_data)

    # Log no terminal do publicador
    try:
        import api_ext
        api_ext._pub_log(f"📺 [Canal {channel['name']}] Novo vídeo importado: \"{title[:45]}\" (Projeto {pid[:8]}) ➔ Conta vinculada: {channel['tiktok_account_name']}")
    except Exception:
        pass

    return {
        "ok": True,
        "project_id": pid,
        "video_id": vid,
        "title": title,
        "tiktok_account": channel["tiktok_account"],
    }


def check_channel_for_new(channel_id: str, server_port: int = 8585) -> dict[str, Any]:
    """Varre um canal específico e importa qualquer vídeo novo que ainda não foi visto."""
    channels_data = load_channels_data()
    ch = next((c for c in channels_data["channels"] if c.get("id") == channel_id), None)
    if not ch:
        raise KeyError("Canal não encontrado.")

    ch["last_checked_at"] = time.time()
    save_channels_data(channels_data)

    meta = resolve_channel_metadata(ch.get("channel_url") or ch.get("handle") or ch.get("channel_id"))
    latest_videos = meta.get("latest_videos") or []

    seen = set(ch.get("seen_video_ids") or [])
    imported = []

    for video in latest_videos:
        vid = video["id"]
        if vid not in seen:
            logger.info(f"[Auto-Canal] Novo vídeo encontrado em {ch['name']}: {video['title']} ({vid})")
            try:
                res = import_channel_video(ch, video, server_port=server_port)
                imported.append(res)
                seen.add(vid)
                # Para evitar flood de imports em lote antigo, importa o primeiro vídeo mais novo
                break
            except Exception as exc:
                logger.error(f"Erro ao importar vídeo {vid}: {exc}")

    # Atualizar dados do canal
    channels_data = load_channels_data()
    for c in channels_data["channels"]:
        if c.get("id") == channel_id:
            c["last_checked_at"] = time.time()
            if latest_videos:
                c["last_video_id"] = latest_videos[0]["id"]
                c["last_video_title"] = latest_videos[0]["title"]
                c["last_video_url"] = latest_videos[0]["url"]
            break
    save_channels_data(channels_data)

    return {
        "channel_id": channel_id,
        "checked": True,
        "new_videos_count": len(imported),
        "imported": imported,
    }


def check_all_channels(server_port: int = 8585) -> dict[str, Any]:
    """Varre todos os canais ativos em busca de novos vídeos."""
    channels_data = load_channels_data()
    channels = [c for c in channels_data.get("channels", []) if c.get("active", True)]
    
    channels_data["settings"]["last_global_check"] = time.time()
    save_channels_data(channels_data)

    total_imported = []
    for ch in channels:
        try:
            res = check_channel_for_new(ch["id"], server_port=server_port)
            if res.get("imported"):
                total_imported.extend(res["imported"])
        except Exception as exc:
            logger.warning(f"Erro ao verificar canal {ch.get('name')}: {exc}")

    return {
        "checked_channels_count": len(channels),
        "new_videos_imported": total_imported,
    }


def _verificar_auto_publicacoes(server_port: int = 8585) -> None:
    """Verifica projetos que foram importados com auto_publish=True e, se estiverem prontos, publica no TikTok."""
    import api_ext
    proj_meta = load_project_channels()
    if not proj_meta:
        return

    # Verificar se o publicador já está ocupado
    with api_ext._PUB_LOCK:
        if api_ext._PUB.get("running"):
            return

    for pid, meta in list(proj_meta.items()):
        if not meta.get("auto_publish"):
            continue
        if meta.get("status") in ("published", "publishing", "failed"):
            continue

        # Ler status do projeto
        try:
            studio_state = api_ext._read_studio(pid)
        except Exception:
            continue

        # Verificar se tem variantes renderizadas com vídeo pronto
        variants = studio_state.get("output_variants", [])
        ready_variants = []
        for v in variants:
            vid = v.get("id")
            if not vid:
                continue
            payload = api_ext._variant_payload(pid, vid)
            if payload and payload.get("has_video"):
                ready_variants.append(vid)

        # Se encontrou variantes prontas e o pipeline terminou
        jobs = studio_state.get("jobs", [])
        render_jobs = [j for j in jobs if j.get("type") in ("render", "export")]
        all_done = render_jobs and all(j.get("status") in ("completed", "failed") for j in render_jobs)

        if ready_variants and all_done:
            # Iniciar publicação automática EXCLUSIVAMENTE na conta vinculada ao canal!
            account_id = meta.get("tiktok_account")
            account_name = meta.get("tiktok_account_name", account_id)
            interval_min = meta.get("interval_min", 30)

            api_ext._pub_log(f"🚀 [Automação] Projeto \"{studio_state.get('name', pid[:8])}\" concluído! Disparando envio de {len(ready_variants)} cortes na conta \"{account_name}\"")
            
            meta["status"] = "publishing"
            save_project_channel_meta(pid, meta)

            try:
                api_ext._iniciar_publicacao_lote(
                    pid=pid,
                    vids=ready_variants,
                    account=account_id,
                    interval=interval_min,
                    render_wait=False,
                )
                meta["status"] = "published"
                save_project_channel_meta(pid, meta)
                break  # Processa um projeto por vez
            except Exception as exc:
                logger.error(f"Erro ao disparar auto-publicação para {pid}: {exc}")
                meta["status"] = "failed"
                save_project_channel_meta(pid, meta)


_BACKGROUND_THREAD: Optional[threading.Thread] = None


def start_channel_monitor_thread(server_port: int = 8585) -> None:
    """Inicia thread em segundo plano para varredura periódica e publicação automática."""
    global _BACKGROUND_THREAD
    with _MONITOR_LOCK:
        if _BACKGROUND_THREAD and _BACKGROUND_THREAD.is_alive():
            return

        def _worker():
            logger.info("Monitor de Canais do YouTube iniciado em segundo plano.")
            # Aguardar 10s para o servidor terminar de subir
            time.sleep(10)
            while True:
                try:
                    channels_data = load_channels_data()
                    settings = channels_data.get("settings", {})
                    auto_check = settings.get("auto_check_enabled", True)
                    interval_min = max(5, settings.get("check_interval_minutes", 15))
                    last_check = settings.get("last_global_check", 0.0)

                    # 1. Verificar novos vídeos se o tempo de intervalo expirou
                    if auto_check and (time.time() - last_check >= interval_min * 60):
                        check_all_channels(server_port=server_port)

                    # 2. Verificar se há projetos prontos aguardando auto-publicação
                    _verificar_auto_publicacoes(server_port=server_port)

                except Exception as exc:
                    logger.warning(f"[Monitor Canais] Exceção no ciclo: {exc}")

                time.sleep(30)

        _BACKGROUND_THREAD = threading.Thread(target=_worker, daemon=True, name="YouTubeChannelMonitor")
        _BACKGROUND_THREAD.start()
