"""Módulo de integração do TikTok Downloader com o TikClip Web.

Permite listar canais/criadores baixados na pasta do tiktok-downloader-web,
exibir os vídeos em cards com previews e metadados extraídos dos arquivos .info.txt,
e publicar vídeos diretamente nas contas do TikTok conectadas.
"""
from __future__ import annotations

import json
import os
import shutil
import re
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Diretório base dos downloads do projeto tiktok-downloader-web
def _get_tiktok_downloads_dir() -> Path:
    raw = os.getenv("TIKTOK_DOWNLOADS_DIR")
    if raw:
        return Path(raw)
    return _data_dir() / "tiktok_downloads"

def _get_tiktok_channels_json() -> Path:
    raw = os.getenv("TIKTOK_CHANNELS_JSON")
    if raw:
        return Path(raw)
    return _data_dir() / "tiktok_channels.json"

TIKTOK_DOWNLOADS_DIR = _get_tiktok_downloads_dir()
TIKTOK_CHANNELS_JSON = _get_tiktok_channels_json()


def _data_dir() -> Path:
    raw = os.getenv('TIKCLIP_DATA_DIR')
    if raw:
        return Path(raw)
    return Path(os.getenv('APPDATA', str(Path.home()))) / 'TikClip'


def _history_file() -> Path:
    return _data_dir() / 'tiktok-downloads-history.json'


def _channels_accounts_file() -> Path:
    return _data_dir() / 'tiktok-channels-accounts.json'


def load_channels_accounts() -> dict[str, str]:
    try:
        p = _channels_accounts_file()
        if p.exists():
            return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        pass
    return {}


def save_channel_account(folder: str, account_id: str) -> None:
    data = load_channels_accounts()
    clean_folder = folder.strip()
    data[clean_folder] = account_id.strip()
    clean_id = clean_folder.lstrip('@').lower()
    data[clean_id] = account_id.strip()
    try:
        _channels_accounts_file().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass


def _load_history() -> dict[str, Any]:
    try:
        p = _history_file()
        if p.exists():
            return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        pass
    return {}


def _save_history(history: dict[str, Any]) -> None:
    try:
        _history_file().write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass


def mark_video_published(video_id: str, account_id: str, status: str = 'completed') -> None:
    hist = _load_history()
    hist[video_id] = {
        'account_id': account_id,
        'status': status,
        'published_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }
    _save_history(hist)


def parse_info_file(info_path: Path) -> dict[str, Any]:
    """Extrai canal, data, link original, descrição e hashtags de um arquivo .info.txt."""
    if not info_path.is_file():
        return {}

    try:
        text = info_path.read_text(encoding='utf-8', errors='ignore')
    except Exception:
        return {}

    channel = ""
    upload_date = ""
    original_url = ""
    description = ""
    hashtags: list[str] = []

    m_channel = re.search(r'CANAL:\s*(@?\w+)', text)
    if m_channel:
        channel = m_channel.group(1).strip()

    m_date = re.search(r'DATA DE PUBLICA[ÇC][ÃA]O:\s*(\w+)', text)
    if m_date:
        upload_date = m_date.group(1).strip()

    m_link = re.search(r'LINK ORIGINAL:\s*(https?://[^\s\r\n]+)', text)
    if m_link:
        original_url = m_link.group(1).strip()

    if '--- DESCRIÇÃO DO VÍDEO ---' in text or '--- DESCRI' in text:
        spl = re.split(r'--- DESCRI[ÇC][ÃA]O DO V[ÍI]DEO ---', text)
        if len(spl) > 1:
            part = spl[1]
            if '--- HASHTAGS' in part:
                p_desc, p_tags = re.split(r'--- HASHTAGS.*---', part, maxsplit=1)
                description = p_desc.strip()
                # Extrai tags
                tags_part = p_tags.split('=' * 10)[0].strip()
                hashtags = [t for t in tags_part.split() if t.startswith('#')]
            else:
                description = part.split('=' * 10)[0].strip()

    if not hashtags and description:
        tags = re.findall(r'#[\w\d_\u00c0-\u00ff]+', description)
        seen = set()
        for t in tags:
            tl = t.lower()
            if tl not in seen:
                seen.add(tl)
                hashtags.append(t)

    return {
        'channel': channel,
        'upload_date': upload_date,
        'original_url': original_url,
        'description': description,
        'hashtags': hashtags,
    }


def list_tiktok_creators() -> list[dict[str, Any]]:
    """Lista todos os canais/criadores com vídeos baixados."""
    if not TIKTOK_DOWNLOADS_DIR.is_dir():
        return []

    channels_meta: dict[str, dict[str, Any]] = {}
    if TIKTOK_CHANNELS_JSON.is_file():
        try:
            data = json.loads(TIKTOK_CHANNELS_JSON.read_text(encoding='utf-8'))
            for item in data:
                cid = item.get('id') or item.get('username', '').lstrip('@').lower()
                if cid:
                    channels_meta[cid] = item
        except Exception:
            pass

    history = _load_history()
    channel_accounts = load_channels_accounts()
    creators: list[dict[str, Any]] = []

    for folder in sorted(TIKTOK_DOWNLOADS_DIR.iterdir()):
        if not folder.is_dir() or folder.name.startswith('.'):
            continue

        raw_name = folder.name
        clean_id = raw_name.lstrip('@').lower()
        meta = channels_meta.get(clean_id, {})

        mp4_files = list(folder.glob('*.mp4'))
        if not mp4_files:
            continue

        total_bytes = sum(f.stat().st_size for f in mp4_files if f.is_file())
        latest_mtime = max(f.stat().st_mtime for f in mp4_files if f.is_file())
        last_updated_str = datetime.fromtimestamp(latest_mtime).strftime('%d/%m/%Y %H:%M')

        # Contagem de vídeos publicados dessa pasta
        published_count = 0
        for mp4 in mp4_files:
            vid_id = mp4.name.split('_')[0]
            if vid_id in history:
                published_count += 1

        target_acc = channel_accounts.get(raw_name) or channel_accounts.get(clean_id) or 'conta_meuscortes'

        creators.append({
            'id': clean_id,
            'folder': raw_name,
            'username': f'@{clean_id}',
            'display_name': meta.get('username') or f'@{clean_id}',
            'url': meta.get('url') or f'https://www.tiktok.com/@{clean_id}',
            'status': meta.get('status', 'ativo'),
            'target_account_id': target_acc,
            'video_count': len(mp4_files),
            'published_count': published_count,
            'pending_count': max(0, len(mp4_files) - published_count),
            'total_size_mb': round(total_bytes / (1024 * 1024), 1),
            'last_updated': last_updated_str,
            'last_updated_ts': latest_mtime,
        })

    creators.sort(key=lambda c: c['last_updated_ts'], reverse=True)
    return creators


def list_creator_videos(folder_name: str) -> list[dict[str, Any]]:
    """Lista todos os vídeos de um criador específico com seus metadados."""
    creator_dir = TIKTOK_DOWNLOADS_DIR / folder_name
    if not creator_dir.is_dir():
        # Tenta com @ ou sem @
        if folder_name.startswith('@'):
            alt = TIKTOK_DOWNLOADS_DIR / folder_name.lstrip('@')
        else:
            alt = TIKTOK_DOWNLOADS_DIR / f'@{folder_name}'
        if alt.is_dir():
            creator_dir = alt
        else:
            return []

    history = _load_history()
    videos: list[dict[str, Any]] = []

    # Mapeia arquivos .info.txt por id do vídeo
    info_map: dict[str, Path] = {}
    for inf in creator_dir.glob('*.info.txt'):
        prefix = inf.name.split('_')[0]
        if prefix not in info_map:
            info_map[prefix] = inf

    for mp4 in sorted(creator_dir.glob('*.mp4'), key=lambda p: p.stat().st_mtime, reverse=True):
        if not mp4.is_file():
            continue

        stat = mp4.stat()
        vid_id = mp4.name.split('_')[0]
        size_mb = round(stat.st_size / (1024 * 1024), 1)
        created_at_str = datetime.fromtimestamp(stat.st_mtime).strftime('%d/%m/%Y %H:%M')

        # Nome limpo (remove id e sufixo de extensão)
        raw_title = mp4.stem
        if '_' in raw_title:
            title = raw_title.split('_', 1)[1].strip()
        else:
            title = raw_title

        # Metadados do .info.txt
        info_file = info_map.get(vid_id)
        parsed = parse_info_file(info_file) if info_file else {}

        description = parsed.get('description') or title
        hashtags = parsed.get('hashtags') or []
        original_url = parsed.get('original_url') or f'https://www.tiktok.com/{folder_name}/video/{vid_id}'
        upload_date = parsed.get('upload_date')

        # Status de publicação
        hist_entry = history.get(vid_id)
        is_published = bool(hist_entry)
        published_account = hist_entry.get('account_id') if hist_entry else None
        published_at = hist_entry.get('published_at') if hist_entry else None

        # URL codificada para streaming estático no FastAPI
        encoded_folder = urllib.parse.quote(creator_dir.name)
        encoded_file = urllib.parse.quote(mp4.name)
        video_url = f'/media/tiktok-downloads/{encoded_folder}/{encoded_file}'

        cover_url = None
        for ext in ('.jpg', '.jpeg', '.webp', '.png'):
            cands = list(creator_dir.glob(f'{vid_id}*{ext}'))
            if cands:
                cover_url = f'/media/tiktok-downloads/{encoded_folder}/{urllib.parse.quote(cands[0].name)}'
                break

        videos.append({
            'id': vid_id,
            'folder': creator_dir.name,
            'filename': mp4.name,
            'video_url': video_url,
            'cover_url': cover_url,
            'title': title,
            'raw_title': raw_title,
            'description': description,
            'hashtags': hashtags,
            'size_mb': size_mb,
            'size_bytes': stat.st_size,
            'created_at': created_at_str,
            'original_url': original_url,
            'upload_date': upload_date,
            'is_published': is_published,
            'published_account': published_account,
            'published_at': published_at,
        })

    return videos


def extract_video_cover(video_path: Path, second: float = 1.5) -> Path | None:
    """Extrai um frame nítido do vídeo em alta qualidade para servir de capa oficial no TikTok."""
    if not video_path.is_file():
        return None

    vid_id = video_path.name.split('_')[0]
    cover_file = video_path.parent / f"{vid_id}_cover.jpg"

    ffmpeg_bin = os.environ.get('TIKCLIP_FFMPEG_PATH')
    if not ffmpeg_bin or not Path(ffmpeg_bin).is_file():
        cand = Path()
        if cand.is_file():
            ffmpeg_bin = str(cand)
        else:
            ffmpeg_bin = 'ffmpeg'

    cmd = [
        ffmpeg_bin, '-y',
        '-ss', f'{second:.2f}',
        '-i', str(video_path),
        '-frames:v', '1',
        '-q:v', '2',
        str(cover_file)
    ]
    sem_janela = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000) if os.name == 'nt' else 0
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, creationflags=sem_janela)
        if proc.returncode == 0 and cover_file.is_file() and cover_file.stat().st_size > 0:
            return cover_file
    except Exception as exc:
        print(f"[aviso] Falha ao extrair capa do vídeo com FFmpeg: {exc}")
    return None


def publish_tiktok_download_single(
    folder: str,
    filename: str,
    account_id: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[list[str]] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """Publica um vídeo baixado específico diretamente no TikTok."""
    video_path = TIKTOK_DOWNLOADS_DIR / folder / filename
    if not video_path.is_file():
        raise FileNotFoundError(f'Vídeo não encontrado: {video_path}')

    vid_id = filename.split('_')[0]

    # Se metadados não fornecidos, busca do .info.txt
    if not description or tags is None:
        matching_infos = list((TIKTOK_DOWNLOADS_DIR / folder).glob(f'{vid_id}*.info.txt'))
        if matching_infos:
            parsed = parse_info_file(matching_infos[0])
            if not description:
                description = parsed.get('description') or title or filename
            if tags is None:
                tags = parsed.get('hashtags') or []

    final_title = title or (description[:80] if description else filename)
    final_desc = description or final_title
    final_tags = tags or []

    try:
        from backend.services import tiktok_local_publisher as ttl
    except ImportError as e:
        raise RuntimeError(f'Módulo de publicação do TikTok não disponível: {e}')

    def _prog(msg: str):
        if progress_callback:
            try:
                progress_callback(msg)
            except Exception:
                pass

    # Tenta localizar uma imagem de capa se existir (.jpg, .webp, .png)
    cover_file = None
    for ext in ('.jpg', '.jpeg', '.webp', '.png'):
        cand = list((TIKTOK_DOWNLOADS_DIR / folder).glob(f'{vid_id}*{ext}'))
        if cand:
            cover_file = cand[0]
            break

    # Se não tiver capa, extrai automaticamente um frame nítido com FFmpeg!
    if not cover_file or not cover_file.is_file():
        _prog('Extraindo capa personalizada do vídeo com FFmpeg...')
        cover_file = extract_video_cover(video_path, second=1.5)
        if cover_file:
            _prog(f'Capa gerada com sucesso: {cover_file.name}')

    _prog(f'Iniciando envio de "{final_title}" para conta "{account_id}"...')
    result = ttl.publish_video(
        video_path,
        final_title,
        final_desc,
        final_tags,
        cover_path=cover_file,
        account=account_id,
        progress=_prog,
    )

    status = (result or {}).get('status', 'unknown')
    mark_video_published(vid_id, account_id, status=status)
    return {
        'ok': True,
        'video_id': vid_id,
        'status': status,
        'account_id': account_id,
        'result': result,
    }


def publish_tiktok_downloads_batch(
    items: list[dict[str, Any]],
    account_id: str,
    interval_min: int = 10,
) -> None:
    """Enfileira e publica uma lista de vídeos baixados em segundo plano,

    integrado ao painel do Publicador TikTok oficial com logs em tempo real.
    """
    import api_ext

    def _worker():
        total = len(items)
        published = 0
        failed = 0

        with api_ext._PUB_LOCK:
            api_ext._PUB.update(
                running=True,
                stop=False,
                skip_wait=False,
                total=total,
                index=0,
                project_id=f'TikTok Downloads ({account_id})',
                account_id=account_id,
                message=f'Publicando vídeos do TikTok ({total} itens)...',
            )

        api_ext._pub_log(f'=== Iniciando publicação em lote: {total} vídeos baixados do TikTok ===')
        api_ext._pub_log(f'Conta de destino: {account_id} | Intervalo entre posts: {interval_min} min')

        for idx, item in enumerate(items, 1):
            with api_ext._PUB_LOCK:
                if api_ext._PUB.get('stop'):
                    api_ext._pub_log('Publicação interrompida pelo usuário.')
                    break
                api_ext._PUB['index'] = idx

            folder = item.get('folder')
            filename = item.get('filename')
            title = item.get('title')
            desc = item.get('description')
            tags = item.get('hashtags') or []

            vid_id = filename.split('_')[0] if filename else ''
            api_ext._pub_log(f'[{idx}/{total}] Preparando "{title or filename}"...')

            def progress(msg: str, _idx=idx, _t=total):
                if msg:
                    api_ext._pub_log(f'[{_idx}/{_t}] {msg}')

            try:
                res = publish_tiktok_download_single(
                    folder=folder,
                    filename=filename,
                    account_id=account_id,
                    title=title,
                    description=desc,
                    tags=tags,
                    progress_callback=progress,
                )
                status = res.get('status')
                if status == 'completed':
                    published += 1
                    api_ext._pub_log(f'[{idx}/{total}] Publicado com sucesso no TikTok ✔')
                else:
                    api_ext._pub_log(f'[{idx}/{total}] Enviado ({status}) — verifique rascunhos no TikTok')
            except Exception as exc:
                failed += 1
                api_ext._pub_log(f'[{idx}/{total}] Falha ao publicar: {exc}')

            if idx >= total or not interval_min:
                continue

            wait_s = int(interval_min * 60)
            api_ext._pub_log(f'Aguardando intervalo de {interval_min} min antes do próximo vídeo...')

            end = time.time() + wait_s
            while time.time() < end:
                with api_ext._PUB_LOCK:
                    if api_ext._PUB.get('stop') or api_ext._PUB.get('skip_wait'):
                        api_ext._PUB['skip_wait'] = False
                        break
                time.sleep(1)

        with api_ext._PUB_LOCK:
            api_ext._PUB.update(
                running=False,
                message=f'Concluído: {published} publicados, {failed} falhas',
            )
        api_ext._pub_log(f'=== Finalizado: {published} publicados com sucesso, {failed} com falha ===')

    th = threading.Thread(target=_worker, daemon=True)
    with api_ext._PUB_LOCK:
        api_ext._PUB_THREAD = th
    th.start()