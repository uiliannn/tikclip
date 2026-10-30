"""Camada HTTP do TikClip.

A GUI antiga (Tkinter) chamava o Python do TikClip diretamente. Como a nova
interface é web (WebView2), tudo o que ela precisa é exposto aqui como HTTP,
reusando o mesmo motor que o publicador original usava:

- `backend.services.studio.store`      -> estado do projeto (drafts, jobs, variants)
- `backend.services.upload_post_publisher.list_records` -> quais cortes já saíram
- `backend.services.tiktok_local_publisher`             -> contas e publicação
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from backend.services import tiktok_local_publisher as ttl
from backend.services.studio import store
from backend.services.upload_post_publisher import LOCAL_USER, _write_record, list_records

router = APIRouter()

# Porta do próprio servidor, preenchida pelo launcher. Usada para acionar as
# rotas de renderização do Studio em processo (produzir/repetir um corte).
SELF_PORT: int | None = None


# --------------------------------------------------------------------------- #
# caminhos e leitura de estado
# --------------------------------------------------------------------------- #
def data_dir() -> Path:
    raw = os.getenv('TIKCLIP_DATA_DIR')
    if raw:
        return Path(raw)
    return Path(os.getenv('APPDATA', str(Path.home()))) / 'TikClip'


def projects_dir() -> Path:
    return data_dir() / 'projects'


def _db() -> Path:
    return data_dir() / 'tikclip.db'


def _project_rows() -> dict[str, tuple[str | None, str | None]]:
    """id -> (nome, thumbnail base64) direto do banco do TikClip."""
    rows: dict[str, tuple[str | None, str | None]] = {}
    try:
        con = sqlite3.connect(str(_db()))
        try:
            for pid, name, thumb in con.execute('SELECT id, name, thumbnail FROM projects'):
                rows[pid] = (name, thumb)
        finally:
            con.close()
    except Exception:
        pass
    return rows


def _read_studio(pid: str) -> dict[str, Any]:
    meta = projects_dir() / pid / 'metadata' / 'studio.json'
    if not meta.is_file():
        return {}
    try:
        return json.loads(meta.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _published_keys(pid: str) -> dict[str, dict[str, Any]]:
    keys: dict[str, dict[str, Any]] = {}
    try:
        for rec in list_records(pid):
            if rec.get('status') == 'completed':
                key = rec.get('output_variant_id') or rec.get('clip_id')
                if key:
                    keys[key] = rec
    except Exception:
        pass
    return keys


def _clean_title(raw: str) -> str:
    return re.sub(r'^\[PUBLICADO[^\]]*\]\s*', '', raw or '').strip()


def _video_path(pid: str, job: dict[str, Any] | None) -> Path | None:
    if not job:
        return None
    candidate = projects_dir() / pid / 'output' / 'studio' / f"{job.get('job_id')}.mp4"
    return candidate if candidate.is_file() else None


def _cover_path(pid: str, job_id: str | None, variant_id: str) -> Path | None:
    base = projects_dir() / pid / 'output' / 'covers'
    names = ('douyin.jpg', 'frame-tiktok.jpg', 'kit-tiktok.jpg')
    for key in (f'studio-{job_id}' if job_id else None, job_id, variant_id):
        if not key:
            continue
        for name in names:
            candidate = base / str(key) / name
            if candidate.is_file():
                return candidate
    return None


def project_clips(pid: str) -> list[dict[str, Any]]:
    """Monta a lista de cortes com o mesmo critério de status da GUI original."""
    state = _read_studio(pid)
    jobs = {job['job_id']: job for job in state.get('jobs', []) if job.get('job_id')}
    drafts = {d['id']: d for d in state.get('drafts', []) if d.get('id')}
    published = _published_keys(pid)
    personalizadas = ler_hashtags_projeto(pid)
    items: list[dict[str, Any]] = []

    for variant in state.get('output_variants', []):
        vid = variant.get('id')
        if not vid:
            continue
        job = jobs.get(variant.get('render_job_id'))
        job_id = job.get('job_id') if job else None
        status = variant.get('status')

        video = _video_path(pid, job) if status == 'completed' else None
        ready = video is not None

        rec = published.get(vid) or (published.get(f'studio-{job_id}') if job_id else None)
        is_published = bool(ready and rec)

        if is_published:
            kind = 'publicado'
        elif ready:
            kind = 'pronto'
        elif status in ('queued', 'preparing', 'running'):
            kind = 'renderizando'
        elif status == 'failed':
            kind = 'falhou'
        else:
            kind = 'pendente'

        post = variant.get('post') or {}
        draft = drafts.get(variant.get('draft_id'), {})
        raw_title = (
            post.get('title')
            or draft.get('title')
            or (f"Corte {job_id[:6]}" if job_id else f"Corte {vid[:6]}")
        )

        duration = 0.0
        if job:
            try:
                duration = float((job.get('result') or {}).get('duration') or 0.0)
            except Exception:
                duration = 0.0

        cover = _cover_path(pid, job_id, vid)
        items.append({
            'id': vid,
            'title': _clean_title(raw_title),
            'description': post.get('description') or '',
            'tags': _tags_com_personalizadas(post.get('tags'),
                                             variant.get('strategy_id'), personalizadas),
            'status': kind,
            'variant_status': status,
            'duration': duration,
            'duration_label': (
                f'{int(duration // 60):02d}:{int(duration % 60):02d}' if duration > 0 else ''
            ),
            'has_video': ready,
            'published_at': (rec or {}).get('created_at') if rec else None,
            'cover_url': f'/api/v1/ext/projects/{pid}/clips/{vid}/cover' if cover else None,
            'video_url': f'/api/v1/ext/projects/{pid}/clips/{vid}/video' if ready else None,
        })
    return items


def project_summary(pid: str) -> dict[str, Any]:
    clips = project_clips(pid)
    total = len(clips)
    published = sum(1 for c in clips if c['status'] == 'publicado')
    ready = sum(1 for c in clips if c['status'] in ('pronto', 'publicado'))
    return {
        'id': pid,
        'clips_total': total,
        'clips_ready': ready,
        'clips_published': published,
        'clips_pending': total - published,
    }


# --------------------------------------------------------------------------- #
# rotas
# --------------------------------------------------------------------------- #
@router.get('/status')
def ext_status() -> dict[str, Any]:
    base = data_dir()
    accounts: list[dict[str, Any]] = []
    try:
        accounts = ttl.list_accounts()
    except Exception:
        pass
    return {
        'ok': True,
        'data_dir': str(base),
        'projects_dir_exists': projects_dir().is_dir(),
        'db_exists': _db().is_file(),
        'accounts': accounts,
        'self_port': SELF_PORT,
    }


@router.get('/projects')
def ext_projects() -> dict[str, Any]:
    root = projects_dir()
    if not root.is_dir():
        return {'items': []}

    rows = _project_rows()
    items: list[dict[str, Any]] = []

    for entry in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not entry.is_dir():
            continue
        pid = entry.name
        meta = entry / 'metadata' / 'studio.json'
        if not meta.is_file():
            continue

        state = _read_studio(pid)
        name = (rows.get(pid) or (None, None))[0]
        source_title = (state.get('source_meta') or {}).get('title')
        if not source_title:
            drafts = state.get('drafts') or []
            source_title = drafts[0].get('title') if drafts else None

        summary = project_summary(pid)
        items.append({
            **summary,
            'name': name or source_title or f'Projeto {pid[:8]}',
            'title': source_title or name or f'Projeto {pid[:8]}',
            'updated_at': entry.stat().st_mtime,
            'thumb_url': f'/api/v1/ext/projects/{pid}/thumb',
            'has_thumb': bool((rows.get(pid) or (None, None))[1]) or bool(_cover_path_any(pid)),
        })
    return {'items': items}


def _cover_path_any(pid: str) -> Path | None:
    base = projects_dir() / pid / 'output' / 'covers'
    if not base.is_dir():
        return None
    for sub in base.iterdir():
        for name in ('douyin.jpg', 'frame-tiktok.jpg', 'kit-tiktok.jpg'):
            candidate = sub / name
            if candidate.is_file():
                return candidate
    return None


@router.get('/projects/{pid}/thumb')
def ext_project_thumb(pid: str) -> Response:
    row = _project_rows().get(pid)
    raw = row[1] if row else None
    if raw and len(raw) > 50:
        payload = raw.split(',', 1)[1] if ',' in raw else raw
        try:
            return Response(content=base64.b64decode(payload), media_type='image/jpeg')
        except Exception:
            pass
    cover = _cover_path_any(pid)
    if cover:
        return Response(content=cover.read_bytes(), media_type='image/jpeg')
    raise HTTPException(status_code=404, detail='Sem miniatura')


@router.get('/projects/{pid}/clips')
def ext_project_clips(pid: str) -> dict[str, Any]:
    if not (projects_dir() / pid).is_dir():
        raise HTTPException(status_code=404, detail='Projeto não encontrado')
    clips = project_clips(pid)
    return {
        'project_id': pid,
        'items': clips,
        'total': len(clips),
        'published': sum(1 for c in clips if c['status'] == 'publicado'),
        'ready': sum(1 for c in clips if c['status'] in ('pronto', 'publicado')),
    }


@router.get('/projects/{pid}/clips/{vid}/cover')
def ext_clip_cover(pid: str, vid: str) -> Response:
    state = _read_studio(pid)
    jobs = {j['job_id']: j for j in state.get('jobs', []) if j.get('job_id')}
    variant = next((v for v in state.get('output_variants', []) if v.get('id') == vid), None)
    job = jobs.get(variant.get('render_job_id')) if variant else None
    cover = _cover_path(pid, job.get('job_id') if job else None, vid)
    if not cover:
        raise HTTPException(status_code=404, detail='Sem capa')
    return Response(content=cover.read_bytes(), media_type='image/jpeg')


@router.get('/projects/{pid}/clips/{vid}/video')
def ext_clip_video(pid: str, vid: str) -> Response:
    state = _read_studio(pid)
    jobs = {j['job_id']: j for j in state.get('jobs', []) if j.get('job_id')}
    variant = next((v for v in state.get('output_variants', []) if v.get('id') == vid), None)
    job = jobs.get(variant.get('render_job_id')) if variant else None
    video = _video_path(pid, job)
    if not video:
        raise HTTPException(status_code=404, detail='Vídeo não renderizado')
    return Response(content=video.read_bytes(), media_type='video/mp4')


# --------------------------------------------------------------------------- #
# contas do TikTok
# --------------------------------------------------------------------------- #
@router.get('/accounts')
def ext_accounts() -> dict[str, Any]:
    try:
        accounts = ttl.list_accounts()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f'Falha ao listar contas: {exc}') from exc

    out = []
    for acc in accounts:
        info: dict[str, Any] = {'id': acc.get('id'), 'name': acc.get('name')}
        try:
            st = ttl.status(acc.get('id'))
            info['logged_in_before'] = bool(st.get('logged_in_before'))
            info['profile'] = st.get('profile')
        except Exception:
            info['logged_in_before'] = False
        out.append(info)
    return {'items': out}


@router.post('/accounts')
def ext_add_account(payload: dict[str, Any]) -> dict[str, Any]:
    name = (payload or {}).get('name', '')
    try:
        return ttl.add_account(name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete('/accounts/{account_id}')
def ext_del_account(account_id: str) -> dict[str, Any]:
    try:
        return {'removed': ttl.remove_account(account_id)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post('/accounts/{account_id}/connect')
def ext_connect(account_id: str) -> dict[str, Any]:
    """Abre o TikTok Studio para o login único da conta (janela do Playwright)."""
    def worker() -> None:
        try:
            ttl.login_session(account=account_id)
        except Exception:
            pass
        # guarda o IP de onde a sessão nasceu: publicar de outro IP derruba o login
        ip = _ip_publico()
        if ip:
            _gravar_sessao_ip(account_id, ip)

    threading.Thread(target=worker, daemon=True).start()
    return {'started': True, 'account': account_id}


# --------------------------------------------------------------------------- #
# renderização dos cortes pendentes
# --------------------------------------------------------------------------- #
def _post_self(path: str, timeout: float = 8.0) -> bool:
    if not SELF_PORT:
        return False
    url = f'http://127.0.0.1:{SELF_PORT}{path}'
    try:
        req = urllib.request.Request(url, data=b'', headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 201, 202)
    except Exception:
        return False


@router.post('/render/pending')
def ext_render_pending(payload: dict[str, Any]) -> dict[str, Any]:
    pid = (payload or {}).get('project_id')
    if not pid:
        raise HTTPException(status_code=400, detail='project_id obrigatório')

    state = _read_studio(pid)
    pending = [
        v for v in state.get('output_variants', [])
        if v.get('status') in ('on_demand', 'failed')
    ]

    def worker() -> None:
        for variant in pending:
            vid = variant.get('id')
            if not vid:
                continue
            action = 'retry' if variant.get('status') == 'failed' else 'produce'
            _post_self(f'/api/v1/studio/{pid}/output-variants/{vid}/{action}')
            time.sleep(0.3)

    threading.Thread(target=worker, daemon=True).start()
    return {'queued': len(pending)}


# --------------------------------------------------------------------------- #
# publicação em lote (com gerador em segundo plano no intervalo)
# --------------------------------------------------------------------------- #
_PUB_LOCK = threading.RLock()
_PUB: dict[str, Any] = {
    'running': False,
    'stop': False,
    'index': 0,
    'total': 0,
    'message': '',
    'log': [],
    'account': None,
    'project_id': None,
}


def _pub_log(msg: str) -> None:
    stamp = time.strftime('%H:%M:%S')
    with _PUB_LOCK:
        _PUB['message'] = msg
        _PUB['log'].append(f'{stamp}  {msg}')
        if len(_PUB['log']) > 400:
            del _PUB['log'][:100]


def _tags_de_reserva(strategy_id: str | None) -> list[str]:
    """Hashtags mínimos quando o corte não tem nenhum (cortes gerados antes do ajuste)."""
    try:
        from backend.services.studio.post_copy import _fallback_tags
        return _fallback_tags('tiktok' if not strategy_id else strategy_id)
    except Exception:  # noqa: BLE001
        return ['paravoce', 'viral', 'fyp']


# --------------------------------------------------------------------------- #
# hashtags personalizadas por projeto (ficheiro próprio, fora do studio.json)
# --------------------------------------------------------------------------- #
def _hashtags_path(pid: str) -> Path:
    return projects_dir() / pid / 'metadata' / 'hashtags-ext.json'


def ler_hashtags_projeto(pid: str) -> list[str]:
    try:
        dados = json.loads(_hashtags_path(pid).read_text(encoding='utf-8'))
    except Exception:  # noqa: BLE001
        return []
    tags = dados.get('tags') if isinstance(dados, dict) else None
    return [str(t) for t in tags][:12] if isinstance(tags, list) else []


def _tags_com_personalizadas(proprias: Any, strategy_id: str | None,
                             extras: list[str]) -> list[str]:
    """Hashtags do projeto primeiro (escolha do usuário), depois as do corte."""
    saida, vistos = [], set()
    for tag in list(extras or []) + [str(t) for t in (proprias or []) if str(t).strip()]:
        chave = tag.lower()
        if tag and chave not in vistos:
            saida.append(tag)
            vistos.add(chave)
    if not saida:
        saida = _tags_de_reserva(strategy_id)
    return saida[:12]


def _limpar_hashtags(texto: Any) -> list[str]:
    """Aceita 'a b', '#a, #b' ou lista; devolve sem # e sem repetição."""
    if isinstance(texto, list):
        pedacos = [str(p) for p in texto]
    else:
        pedacos = re.split(r'[\s,;]+', str(texto or ''))
    vistos, saida = set(), []
    for pedaco in pedacos:
        tag = re.sub(r'[^0-9A-Za-zÀ-ÿ_]', '', str(pedaco)).strip()
        if not tag or len(tag) > 30:
            continue
        if tag.lower() in vistos:
            continue
        vistos.add(tag.lower())
        saida.append(tag)
        if len(saida) >= 12:
            break
    return saida


def _variant_payload(pid: str, vid: str) -> dict[str, Any] | None:
    for clip in project_clips(pid):
        if clip['id'] == vid:
            return clip
    return None


def _registrar_publicacao(
    pid: str,
    clip: dict[str, Any],
    job: dict[str, Any] | None,
    variant: dict[str, Any] | None,
    video: Path,
    outcome: dict[str, Any] | None,
    account: str | None,
) -> Path:
    """Grava o histórico oficial — é o que faz o TikClip marcar [PUBLICADO].

    Mesmo formato usado pelo publicador original; sem isso o corte continuaria
    aparecendo como "pronto" depois de publicado.
    """
    resultado = outcome or {}
    situacao = resultado.get('status') or 'completed'
    job_id = (job or {}).get('job_id')

    record = {
        'request_id': f'local-{uuid.uuid4()}',
        'job_id': None,
        'provider': 'tiktok_local',
        'project_id': pid,
        'clip_id': f'studio-{job_id}' if job_id else None,
        'platforms': ['tiktok'],
        'user': LOCAL_USER,
        'title': clip.get('title') or '',
        'preset': 'tiktok',
        'path': str(video),
        'output_variant_id': clip.get('id'),
        'strategy_id': (variant or {}).get('strategy_id', 'tiktok'),
        'studio_job_id': job_id,
        'revision': (job or {}).get('revision', 1),
        'scheduled_date': None,
        'submitted_at': datetime.now(timezone.utc).isoformat(),
        'http_status': 200,
        'status': 'completed' if situacao == 'completed' else situacao,
        'account': account,
        'results': [{
            'platform': 'tiktok',
            'success': situacao == 'completed',
            'status': situacao,
            'url': resultado.get('url'),
            'message': resultado.get('message') or 'Publicado no TikTok',
            'error': None,
            'skipped': False,
            'fallback_to_inbox': situacao != 'completed',
        }],
        'response': resultado,
    }
    return _write_record(pid, record)


_PUB_THREAD: threading.Thread | None = None


def _publish_loop(pid: str, account: str, vids: list[str], interval_min: int, render_during_wait: bool) -> None:
    total = len(vids)
    with _PUB_LOCK:
        _PUB.update(running=True, stop=False, index=0, total=total, log=[], account=account, project_id=pid)

    published = 0
    failed = 0
    try:
        published, failed = _publicar_itens(pid, account, vids, interval_min,
                                            render_during_wait, total)
    except Exception as erro:  # noqa: BLE001
        # nada aqui pode deixar a fila travada: o botão ficaria bloqueado até reiniciar
        _pub_log(f'Erro inesperado na publicação: {erro}')
    finally:
        with _PUB_LOCK:
            _PUB['running'] = False
            _PUB['stop'] = False
        _pub_log(f'Concluído: {published} publicado(s), {failed} falha(s).')


def _publicar_itens(pid: str, account: str, vids: list[str], interval_min: int,
                    render_during_wait: bool, total: int) -> tuple[int, int]:
    """Corpo da publicação; devolve (publicados, falhas)."""
    published = 0
    failed = 0
    for idx, vid in enumerate(vids, start=1):
        with _PUB_LOCK:
            interrompido = _PUB['stop']
            if not interrompido:
                _PUB['index'] = idx
        if interrompido:
            _pub_log('Publicação interrompida pelo usuário.')
            break

        clip = _variant_payload(pid, vid)
        if not clip:
            _pub_log(f'[{idx}/{total}] corte {vid[:8]} não encontrado, pulando')
            failed += 1
            continue

        if clip['status'] == 'publicado':
            _pub_log(f'[{idx}/{total}] "{clip["title"]}" já publicado, pulando')
            continue
        if not clip['has_video']:
            _pub_log(f'[{idx}/{total}] "{clip["title"]}" ainda não renderizado, pulando')
            failed += 1
            continue

        state = _read_studio(pid)
        jobs = {j['job_id']: j for j in state.get('jobs', []) if j.get('job_id')}
        variant = next((v for v in state.get('output_variants', []) if v.get('id') == vid), {})
        job = jobs.get(variant.get('render_job_id'))
        video = _video_path(pid, job)
        if not video:
            failed += 1
            continue

        cover = _cover_path(pid, job.get('job_id') if job else None, vid)
        _pub_log(f'[{idx}/{total}] enviando "{clip["title"]}"...')

        def progress(msg: str, _idx=idx, _t=total) -> None:
            if msg:
                _pub_log(f'[{_idx}/{_t}] {msg}')

        try:
            result = ttl.publish_video(
                video,
                clip['title'],
                clip['description'],
                clip['tags'],
                cover,
                account=account,
                progress=progress,
            )
            status = (result or {}).get('status')

            # grava o histórico oficial: sem isso o corte continua "pronto"
            try:
                registro = _registrar_publicacao(pid, clip, job, variant, video, result, account)
                _pub_log(f'[{idx}/{total}] histórico: {registro.name}')
            except Exception as exc:  # noqa: BLE001
                _pub_log(f'[{idx}/{total}] aviso: histórico não gravado ({exc})')

            if status == 'completed':
                published += 1
                _pub_log(f'[{idx}/{total}] publicado com sucesso ✔')
            else:
                _pub_log(f'[{idx}/{total}] enviado ({status}) — confira o rascunho no TikTok')
        except Exception as exc:  # noqa: BLE001
            failed += 1
            _pub_log(f'[{idx}/{total}] falhou: {exc}')

        last = idx >= total
        if last or not interval_min:
            continue

        wait_s = int(interval_min * 60)
        _pub_log(f'aguardando {interval_min} min antes do próximo')

        if render_during_wait:
            _pub_log('aproveitando o intervalo para gerar cortes pendentes')
            try:
                ext_render_pending({'project_id': pid})
            except Exception as erro:  # noqa: BLE001
                _pub_log(f'aviso: não consegui gerar pendentes ({erro})')

        end = time.time() + wait_s
        while time.time() < end:
            with _PUB_LOCK:
                if _PUB['stop']:
                    break
            time.sleep(1)

    return published, failed


# --------------------------------------------------------------------------- #
# IP público: guarda de sessão do TikTok
# --------------------------------------------------------------------------- #
_IP_CACHE = {'ip': None, 'quando': 0.0}


def _ip_publico() -> str | None:
    agora = time.time()
    if _IP_CACHE['ip'] and (agora - _IP_CACHE['quando']) < 300:
        return _IP_CACHE['ip']
    for servico in ('https://api.ipify.org', 'https://ifconfig.me/ip'):
        try:
            with urllib.request.urlopen(servico, timeout=6) as resposta:
                ip = resposta.read(60).decode('utf-8', 'replace').strip()
            if ip:
                _IP_CACHE.update(ip=ip, quando=agora)
                return ip
        except Exception:
            continue
    return None


def _sessao_ip_path() -> Path:
    return data_dir() / 'sessao-ip.json'


def _ler_sessao_ip() -> dict[str, Any]:
    try:
        return json.loads(_sessao_ip_path().read_text(encoding='utf-8'))
    except Exception:
        return {}


def _gravar_sessao_ip(conta: str, ip: str) -> None:
    dados = _ler_sessao_ip()
    dados[conta] = {'ip': ip, 'quando': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    try:
        _sessao_ip_path().write_text(
            json.dumps(dados, ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError:
        pass


@router.post('/publish/batch')
def ext_publish_batch(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload or {}
    pid = data.get('project_id')
    account = data.get('account_id') or 'default'
    vids = data.get('variant_ids') or []
    interval = int(data.get('interval_min') or 0)
    render_wait = bool(data.get('render_during_wait', True))

    if not pid:
        raise HTTPException(status_code=400, detail='project_id obrigatório')
    if not vids:
        raise HTTPException(status_code=400, detail='selecione ao menos um corte')

    # guarda de sessão: publicar de um IP diferente do login pode derrubar a conta
    confirmado = bool(data.get('confirmado'))
    ip_atual = _ip_publico()
    registro = _ler_sessao_ip().get(account) or {}
    ip_sessao = registro.get('ip')
    if ip_atual and ip_sessao and ip_atual != ip_sessao and not confirmado:
        raise HTTPException(status_code=409, detail=(
            f'A sessão desta conta foi criada noutro IP ({ip_sessao}) e agora você está em '
            f'{ip_atual}. Publicar assim pode fazer o TikTok pedir verificação e derrubar '
            'o login. Confirme para continuar mesmo assim.'))
    if ip_atual and (not ip_sessao or confirmado):
        _gravar_sessao_ip(account, ip_atual)

    with _PUB_LOCK:
        if _PUB['running']:
            raise HTTPException(status_code=409, detail='Já existe uma publicação em andamento')

    global _PUB_THREAD
    _PUB_THREAD = threading.Thread(
        target=_publish_loop,
        args=(pid, account, list(vids), interval, render_wait),
        daemon=True,
    )
    _PUB_THREAD.start()
    return {'started': True, 'total': len(vids)}


@router.get('/publish/state')
def ext_publish_state() -> dict[str, Any]:
    # rede de segurança: processo que morreu no meio não pode travar o botão
    with _PUB_LOCK:
        vivo = _PUB_THREAD is not None and _PUB_THREAD.is_alive()
        if _PUB.get('running') and not vivo:
            _PUB['running'] = False
            _PUB['stop'] = False
        return {k: (list(v) if isinstance(v, list) else v) for k, v in _PUB.items()}


@router.post('/publish/stop')
def ext_publish_stop() -> dict[str, Any]:
    with _PUB_LOCK:
        _PUB['stop'] = True
    return {'stopping': True}


# --------------------------------------------------------------------------- #
# login do YouTube (cookies para o yt-dlp)
# --------------------------------------------------------------------------- #
_YT_LOCK = threading.Lock()
_YT: dict[str, Any] = {'running': False, 'message': '', 'erro': None, 'ok': None, 'logado': None}


def _yt_log(msg: str) -> None:
    with _YT_LOCK:
        _YT['message'] = msg


@router.get('/youtube/status')
def ext_youtube_status() -> dict[str, Any]:
    import pot_provider
    import youtube_login

    with _YT_LOCK:
        estado = dict(_YT)
    info = youtube_login.status()
    info['estado'] = estado
    info['provedor'] = pot_provider.status()
    try:
        from backend.services.studio import jobs as studio_jobs
        info['bloqueio'] = studio_jobs.estado_bloqueio()
    except Exception:
        info['bloqueio'] = {'bloqueado': False, 'ha_minutos': None}
    return info


@router.post('/youtube/diagnostico')
def ext_youtube_diagnostico() -> dict[str, Any]:
    import youtube_login

    return youtube_login.diagnostico()


@router.get('/youtube/check')
def ext_youtube_check(url: str) -> dict[str, Any]:
    """Confere o link pelo oembed antes de criar projeto.

    O endereço do YouTube diferencia maiúsculas de minúsculas: um id com a caixa
    trocada responde 404, e o yt-dlp mascara isso como bloqueio anti-bot.
    """
    import json as _json
    import urllib.error
    import urllib.request
    from urllib.parse import urlparse

    from backend.services.studio.jobs import _id_do_youtube

    # o oembed só existe para YouTube; Bilibili é aceito pelo import, então
    # não pode ser recusado aqui
    host = (urlparse(url).hostname or '').lower()
    if any(host == d or host.endswith('.' + d) for d in ('bilibili.com', 'b23.tv')):
        return {'ok': True, 'id': None, 'aviso': 'Link do Bilibili: não dá para conferir antes de baixar.'}

    ident = _id_do_youtube(url)
    if not ident:
        return {
            'ok': False,
            'motivo': 'link',
            'mensagem': 'Link do YouTube inválido. Use o formato '
                        'https://www.youtube.com/watch?v=XXXXXXXXXXX',
        }

    consulta = f'https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={ident}&format=json'
    try:
        with urllib.request.urlopen(consulta, timeout=12) as resposta:
            dados = _json.load(resposta)
        return {
            'ok': True,
            'id': ident,
            'titulo': dados.get('title'),
            'canal': dados.get('author_name'),
        }
    except urllib.error.HTTPError as erro:
        if erro.code in (401, 403, 404):
            return {
                'ok': False,
                'motivo': 'indisponivel',
                'mensagem': f'Este vídeo não está acessível publicamente (id {ident}). '
                            'Confira se o link abre no navegador — o endereço do YouTube '
                            'diferencia maiúsculas de minúsculas.',
            }
        return {'ok': False, 'motivo': 'http', 'mensagem': f'O YouTube respondeu HTTP {erro.code}.'}
    except Exception:
        # sem rede para conferir: deixa o download tentar
        return {'ok': True, 'id': ident, 'aviso': 'Não deu para conferir o link agora.'}


@router.post('/youtube/connect')
def ext_youtube_connect(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    import youtube_login

    with _YT_LOCK:
        if _YT['running']:
            raise HTTPException(status_code=409, detail='O login já está aberto')
        _YT.update(running=True, message='Preparando o navegador...', erro=None, ok=None, logado=None)

    timeout_s = int((payload or {}).get('timeout_s') or 600)

    def worker() -> None:
        try:
            resultado = youtube_login.login_e_exportar(timeout_s=timeout_s, progress=_yt_log)
            with _YT_LOCK:
                _YT.update(ok=True, logado=resultado.get('logado'),
                           message=f"{resultado.get('cookies')} cookies exportados.")
        except Exception as exc:  # noqa: BLE001
            with _YT_LOCK:
                _YT.update(ok=False, erro=str(exc), message='Falhou.')
        finally:
            with _YT_LOCK:
                _YT['running'] = False

    threading.Thread(target=worker, daemon=True).start()
    return {'started': True, 'timeout_s': timeout_s}


@router.post('/youtube/test')
def ext_youtube_test() -> dict[str, Any]:
    import youtube_login

    return youtube_login.testar()


# --------------------------------------------------------------------------- #
# vídeos já baixados: listar e importar sem passar pelo YouTube
# --------------------------------------------------------------------------- #
PASTAS_BAIXADOS: tuple[Path, ...] = (
    Path.home() / 'YT Saver' / 'Download',
    Path.home() / 'Downloads',
)
EXT_VIDEO = {'.mp4', '.mov', '.mkv', '.webm', '.avi'}
_REGISTRO_BAIXADOS: dict[str, Path] = {}


def _normalizar_nome(texto: str) -> str:
    """Compara nomes de arquivo e de projeto sem depender de acento/pontuação."""
    return re.sub(r'[^0-9a-zà-ÿ]+', '', (texto or '').casefold())


@router.get('/downloads')
def ext_downloads(limite: int = 10) -> dict[str, Any]:
    """Lista os vídeos mais recentes nas pastas de download (YT Saver e Downloads).

    Marca os que já viraram projeto, para não refazer o fluxo sem querer.
    """
    achados: list[Path] = []
    for pasta in PASTAS_BAIXADOS:
        if not pasta.is_dir():
            continue
        try:
            for arq in pasta.iterdir():
                if arq.is_file() and arq.suffix.lower() in EXT_VIDEO:
                    achados.append(arq)
        except OSError:
            continue

    # nome do projeto -> id, para reconhecer o que já foi importado
    try:
        por_nome = {
            _normalizar_nome((valores or (None,))[0]): pid
            for pid, valores in _project_rows().items()
            if valores and valores[0]
        }
    except Exception:
        por_nome = {}

    achados.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    itens = []
    for arq in achados[:max(1, limite)]:
        try:
            tamanho = arq.stat().st_size
            quando = arq.stat().st_mtime
        except OSError:
            continue
        ident = hashlib.sha1(str(arq).encode('utf-8')).hexdigest()[:12]
        _REGISTRO_BAIXADOS[ident] = arq
        legenda = arq.with_suffix('.srt')

        projeto = None
        pid = por_nome.get(_normalizar_nome(arq.stem))
        if pid:
            try:
                resumo = project_summary(pid)
                projeto = {'project_id': pid, 'cortes': resumo['clips_total'],
                           'publicados': resumo['clips_published']}
            except Exception:
                projeto = {'project_id': pid, 'cortes': None, 'publicados': None}

        itens.append({
            'id': ident,
            'nome': arq.name,
            'mb': round(tamanho / 1048576, 1),
            'quando': datetime.fromtimestamp(quando).strftime('%d/%m %H:%M'),
            'legenda': legenda.is_file() and legenda.stat().st_size > 200,
            'pasta': arq.parent.name,
            'projeto': projeto,
        })
    return {'items': itens}


@router.post('/downloads/import')
def ext_downloads_import(body: dict[str, Any]) -> dict[str, Any]:
    """Importa um arquivo já baixado, reusando a rota normal do Studio."""
    ident = str(body.get('id') or '')
    arquivo = _REGISTRO_BAIXADOS.get(ident)
    if not arquivo or not arquivo.is_file():
        raise HTTPException(status_code=404, detail='Arquivo não encontrado. Atualize a lista.')

    import requests

    porta = SELF_PORT or int(os.getenv('AC_PORTA', '8765'))
    legenda = arquivo.with_suffix('.srt')
    com_legenda = legenda.is_file() and legenda.stat().st_size > 200
    dados = {
        'goal': 'auto', 'language': 'source', 'aspect': 'portrait',
        'platforms': 'tiktok', 'auto_start': 'true' if body.get('auto', True) else 'false',
        'brand_outro_enabled': 'false', 'name': arquivo.stem[:200],
    }
    # duração: quando o usuário escolhe, vale a escolha; sem ela, a IA decide
    duracao = body.get('duracao')
    if isinstance(duracao, (int, float)) and 10 <= float(duracao) <= 120:
        dados['duration'] = str(int(duracao))

    with arquivo.open('rb') as video:
        arquivos = {'video': (arquivo.name, video, 'video/mp4')}
        lado = None
        if com_legenda:
            lado = legenda.open('rb')
            arquivos['subtitle'] = (legenda.name, lado, 'text/plain')
        try:
            resposta = requests.post(
                f'http://127.0.0.1:{porta}/api/v1/studio/import',
                data=dados, files=arquivos, timeout=1800,
            )
        finally:
            if lado is not None:
                lado.close()

    if resposta.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f'A importação recusou o arquivo: {resposta.text[:200]}',
        )
    return {'project_id': resposta.json().get('project_id'), 'legenda': com_legenda}


# --------------------------------------------------------------------------- #
# configurações de IA (chave, modelo, modo e transcrição)
# --------------------------------------------------------------------------- #
@router.get('/ai/config')
def ext_ai_config() -> dict[str, Any]:
    """Mostra a configuração efetiva de IA (chave mascarada), sem expor segredo."""
    from backend.services import ai_model_settings as ams

    pub = ams.public(ams.for_editing())
    conexoes = []
    for conexao in pub.get('connections', []):
        conexoes.append({
            'id': conexao.get('id'),
            'name': conexao.get('name'),
            'provider': conexao.get('provider'),
            'base_url': conexao.get('base_url') or '',
            'tem_chave': bool(conexao.get('has_key')),
            'chave_mascarada': conexao.get('api_key_masked') or '',
            'em_uso': conexao.get('id') == (pub.get('analysis') or {}).get('connection_id'),
        })
    return {
        'salvo': bool(pub.get('saved')),
        'modo_analise': pub.get('analysis_mode'),
        'conexoes': conexoes,
        'analise': pub.get('analysis') or {},
        'visao': pub.get('vision'),
        'transcricao': pub.get('transcription') or {},
        'avisos': pub.get('migration_warnings') or [],
    }


# presets de provedor mostrados na tela (o núcleo já conhece todos estes)
_PROVEDORES = [
    {'id': 'gemini', 'nome': 'Google Gemini', 'base_url': '', 'modelo': 'gemini-3.5-flash-lite'},
    {'id': 'deepseek', 'nome': 'DeepSeek', 'base_url': 'https://api.deepseek.com',
     'modelo': 'deepseek-chat'},
    {'id': 'openai', 'nome': 'OpenAI', 'base_url': 'https://api.openai.com/v1',
     'modelo': 'gpt-4o-mini'},
    {'id': 'dashscope', 'nome': 'Alibaba DashScope (Qwen)', 'base_url': '', 'modelo': 'qwen-plus'},
    {'id': 'kimi', 'nome': 'Moonshot Kimi', 'base_url': 'https://api.moonshot.cn/v1',
     'modelo': 'kimi-k2.6'},
    {'id': 'glm', 'nome': 'Zhipu GLM', 'base_url': 'https://open.bigmodel.cn/api/paas/v4',
     'modelo': 'glm-5.3'},
    {'id': 'grok', 'nome': 'xAI Grok', 'base_url': 'https://api.x.ai/v1', 'modelo': 'grok-4.6'},
    {'id': 'compatible', 'nome': 'Outro (compatível com OpenAI)', 'base_url': '',
     'modelo': ''},
]


@router.get('/ai/provedores')
def ext_ai_provedores() -> dict[str, Any]:
    """Lista de provedores que o motor aceita, com endereço e modelo sugeridos."""
    return {'itens': _PROVEDORES}


@router.post('/ai/config')
def ext_ai_salvar(body: dict[str, Any]) -> dict[str, Any]:
    """Grava as alterações; o resto da configuração é preservado como está."""
    from backend.services import ai_model_settings as ams

    atual = ams.for_editing()
    dados = atual.model_dump()

    conexoes = dados.get('connections') or []
    alvo = body.get('conexao_id') or (conexoes[0]['id'] if conexoes else None)
    chave = (body.get('api_key') or '').strip()
    url = (body.get('base_url') or '').strip()
    modelo = (body.get('modelo') or '').strip()

    provider = (body.get('provider') or '').strip()
    nome = (body.get('nome') or '').strip()
    for conexao in conexoes:
        if conexao.get('id') != alvo:
            continue
        if chave:
            conexao['api_key'] = chave
        if provider:
            conexao['provider'] = provider
            # provedor trocado: a URL antiga (do Gemini, por exemplo) não vale mais
            conexao['base_url'] = url or next(
                (p['base_url'] for p in _PROVEDORES if p['id'] == provider), '')
        elif url:
            conexao['base_url'] = url
        if nome:
            conexao['name'] = nome

    if modelo and dados.get('analysis'):
        dados['analysis']['model'] = modelo

    modo = body.get('modo_analise')
    if modo in ('auto', 'visual', 'subtitle'):
        dados['analysis_mode'] = modo

    whisper = (body.get('whisper_model') or '').strip()
    if whisper:
        dados.setdefault('transcription', {})
        dados['transcription']['model'] = whisper

    try:
        novo = ams.ModelSettings.model_validate(dados)
        ams.save(novo)
    except Exception as exc:  # noqa: BLE001
        motivo = str(exc)
        if '请选择支持的服务类型' in motivo:
            motivo = 'provedor não suportado'
        elif '请填写自定义服务的接口地址' in motivo:
            motivo = 'informe o endereço da API para o provedor personalizado'
        raise HTTPException(status_code=400,
                            detail=f'Não consegui salvar: {motivo[:240]}') from exc
    return {'ok': True, 'salvo': True}


@router.post('/ai/test')
def ext_ai_test() -> dict[str, Any]:
    """Faz uma chamada mínima ao modelo de análise para ver se a chave responde."""
    from backend.services import ai_model_settings as ams

    config = ams.for_editing()
    if not config.analysis:
        raise HTTPException(status_code=400, detail='Nenhum modelo de análise configurado.')

    conexao = ams.connection_for(config, config.analysis)
    endpoint = ams.chat_endpoint(conexao, config.analysis.model)
    url = endpoint['base_url'].rstrip('/') + '/chat/completions'
    corpo = {
        'model': endpoint['model'],
        'max_tokens': 8,
        'messages': [{'role': 'user', 'content': 'responda apenas: ok'}],
    }

    import requests

    try:
        resposta = requests.post(
            url, json=corpo, timeout=25,
            headers={'Authorization': f"Bearer {endpoint['api_key']}"},
        )
    except Exception as exc:  # noqa: BLE001
        return {'ok': False, 'modelo': endpoint['model'],
                'detalhe': f'Falha de rede: {str(exc)[:180]}'}

    texto = ''
    try:
        escolhas = resposta.json().get('choices') or []
        texto = ((escolhas[0] or {}).get('message') or {}).get('content') or ''
    except Exception:  # noqa: BLE001
        texto = ''
    return {
        'ok': bool(resposta.ok),
        'status': resposta.status_code,
        'modelo': endpoint['model'],
        'resposta': (texto or resposta.text[:160]).strip()[:200],
    }


# --------------------------------------------------------------------------- #
# editor do corte: título, gancho, início/fim e regeração
# --------------------------------------------------------------------------- #
# estilos de título válidos por versão do modelo de texto (mesma regra do models.py)
ESTILOS_POR_VERSAO: dict[int, list[str]] = {
    1: ['plain', 'impact', 'card', 'comic', 'neon', 'arena'],
    2: ['plain', 'impact', 'card', 'comic', 'neon', 'arena', 'editorial'],
    3: ['plain', 'impact', 'card', 'comic', 'neon', 'arena', 'editorial', 'pixel', 'frosted'],
    4: ['comic'],
    5: ['comic'],
    6: ['comic', 'neon', 'arena', 'editorial', 'pixel', 'frosted'],
}


def _rastreio(pid: str, vid: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Devolve (estado, variante, rascunho) do corte pedido."""
    estado = _read_studio(pid)
    variante = next((v for v in estado.get('output_variants', []) if v.get('id') == vid), None)
    if not variante:
        raise HTTPException(status_code=404, detail='Este corte não existe no projeto.')
    rascunho = next((d for d in estado.get('drafts', [])
                     if d.get('id') == variante.get('draft_id')), None)
    if not rascunho:
        raise HTTPException(status_code=404, detail='O rascunho deste corte não existe mais.')
    return estado, variante, rascunho


@router.get('/projects/{pid}/clips/{vid}/edit')
def ext_clip_edit_get(pid: str, vid: str) -> dict[str, Any]:
    _estado, variante, rascunho = _rastreio(pid, vid)
    cenas = rascunho.get('scenes') or []
    inicio = float(cenas[0].get('start') or 0) if cenas else 0.0
    fim = float(cenas[-1].get('end') or 0) if cenas else 0.0
    versao = int(rascunho.get('title_template_version') or 1)
    permitidos = ESTILOS_POR_VERSAO.get(versao, list(ESTILOS_POR_VERSAO[3]))
    atual = rascunho.get('title_style') or 'plain'
    if atual not in permitidos:
        permitidos = [atual, *permitidos]
    return {
        'clip_id': vid,
        'draft_id': rascunho.get('id'),
        'revision': rascunho.get('revision'),
        'title': rascunho.get('title') or '',
        'hook': rascunho.get('hook') or '',
        'inicio': round(inicio, 2),
        'fim': round(fim, 2),
        'duracao': round(max(0.0, fim - inicio), 2),
        'legendas': bool(rascunho.get('subtitles', True)),
        'layout': rascunho.get('layout') or 'fit',
        'crop_x': rascunho.get('crop_x'),
        'title_style': atual,
        'title_template_version': versao,
        'title_styles_permitidos': permitidos,
        'title_scale': rascunho.get('title_scale'),
        'subtitle_style': rascunho.get('subtitle_style') or 'clean',
        'aspect': rascunho.get('aspect') or 'original',
        'cenas': len(cenas),
        'post': variante.get('post') or {},
        'status': variante.get('status'),
    }


@router.post('/projects/{pid}/clips/{vid}/edit')
def ext_clip_edit_save(pid: str, vid: str, body: dict[str, Any]) -> dict[str, Any]:
    """Grava o rascunho e, se pedido, manda regerar aquele corte."""
    import requests

    _estado, variante, rascunho = _rastreio(pid, vid)
    novo = dict(rascunho)

    titulo = (body.get('titulo') or '').strip()
    if titulo:
        novo['title'] = titulo[:200]

    if 'gancho' in body:
        novo['hook'] = (body.get('gancho') or '').strip()[:120]

    if 'legendas' in body:
        novo['subtitles'] = bool(body.get('legendas'))

    if body.get('layout') in ('fit', 'crop', 'blur', 'window'):
        novo['layout'] = body['layout']

    if body.get('title_style'):
        versao = int(novo.get('title_template_version') or 1)
        permitidos = ESTILOS_POR_VERSAO.get(versao, ESTILOS_POR_VERSAO[3])
        if body['title_style'] not in permitidos:
            raise HTTPException(status_code=400, detail=(
                f'O modelo de texto versão {versao} não aceita o estilo '
                f'"{body["title_style"]}". Use um destes: {", ".join(permitidos)}.'))
        novo['title_style'] = body['title_style']

    if body.get('subtitle_style') in ('clean', 'bold', 'box', 'accent'):
        novo['subtitle_style'] = body['subtitle_style']

    if 'crop_x' in body:
        try:
            novo['crop_x'] = min(1.0, max(0.0, float(body['crop_x'])))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400,
                                detail='O enquadramento precisa ser um número entre 0 e 1.') from None

    if 'title_scale' in body:
        try:
            novo['title_scale'] = min(1.2, max(0.75, float(body['title_scale'])))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400,
                                detail='O tamanho do título precisa ser numérico.') from None

    cenas = [dict(c) for c in (novo.get('scenes') or [])]
    if cenas:
        try:
            inicio = float(body.get('inicio', cenas[0].get('start')))
            fim = float(body.get('fim', cenas[-1].get('end')))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail='Início e fim precisam ser números.') from None
        if inicio < 0 or fim <= 0:
            raise HTTPException(status_code=400, detail='Início e fim precisam ser positivos.')
        if fim - inicio < 0.1:
            raise HTTPException(status_code=400,
                                detail='O trecho precisa ter pelo menos 0,1 segundo.')
        cenas[0]['start'] = round(inicio, 3)
        cenas[-1]['end'] = round(fim, 3)
        for cena in cenas[1:]:
            if float(cena.get('start') or 0) < cenas[0]['start']:
                cena['start'] = cenas[0]['start']
        novo['scenes'] = cenas

    porta = SELF_PORT or int(os.getenv('AC_PORTA', '8765'))
    base = f'http://127.0.0.1:{porta}/api/v1/studio/{pid}'

    try:
        resposta = requests.put(f'{base}/drafts/{novo["id"]}', json=novo, timeout=120)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502,
                            detail=f'Não consegui salvar o rascunho: {exc}') from exc
    if resposta.status_code >= 400:
        detalhe = 'falha ao salvar'
        try:
            detalhe = resposta.json().get('detail') or detalhe
        except Exception:  # noqa: BLE001
            detalhe = resposta.text[:200]
        raise HTTPException(status_code=400, detail=f'A edição foi recusada: {detalhe}')

    regerar = bool(body.get('regerar'))
    if regerar:
        try:
            requests.post(f'{base}/output-variants/{vid}/produce', timeout=120)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502,
                                detail=f'Salvou, mas não consegui regerar: {exc}') from exc

    return {'ok': True, 'regerando': regerar}


@router.post('/projects/{pid}/clips/{vid}/cover')
def ext_clip_cover(pid: str, vid: str) -> dict[str, Any]:
    """Pede uma capa nova (IA) para o corte e devolve o estado do pedido."""
    import requests

    porta = SELF_PORT or int(os.getenv('AC_PORTA', '8765'))
    url = f'http://127.0.0.1:{porta}/api/v1/studio/{pid}/output-variants/{vid}/cover/ai'
    try:
        resposta = requests.post(url, timeout=120)
        dados = resposta.json() if resposta.content else {}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f'Falha ao pedir a capa: {exc}') from exc
    if resposta.status_code >= 400:
        detalhe = dados.get('detail') if isinstance(dados, dict) else resposta.text[:200]
        raise HTTPException(status_code=400, detail=f'A capa foi recusada: {detalhe}')
    return {'ok': True, 'pedido': dados}


# --------------------------------------------------------------------------- #
# capa a partir de um frame do vídeo
# --------------------------------------------------------------------------- #
def _corte_renderizado(pid: str, job_id: str | None) -> Path | None:
    if not job_id:
        return None
    alvo = projects_dir() / pid / 'output' / 'studio' / f'{job_id}.mp4'
    return alvo if alvo.is_file() else None


def _segundo_seguro(valor: Any, duracao: float | None) -> float:
    try:
        segundo = max(0.0, float(valor))
    except (TypeError, ValueError):
        segundo = 0.0
    if duracao and duracao > 0.2:
        segundo = min(segundo, float(duracao) - 0.1)
    return round(segundo, 3)


def _extrair_frame(video: Path, segundo: float, destino: Path) -> None:
    import subprocess

    from backend.utils.ffmpeg_utils import get_ffmpeg_path

    destino.parent.mkdir(parents=True, exist_ok=True)
    comando = [get_ffmpeg_path(), '-y', '-ss', f'{segundo:.3f}', '-i', str(video),
               '-frames:v', '1', '-q:v', '3', str(destino)]
    sem_janela = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000) if os.name == 'nt' else 0
    proc = subprocess.run(comando, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=120,
                          creationflags=sem_janela)
    if proc.returncode != 0 or not destino.is_file() or destino.stat().st_size == 0:
        raise HTTPException(status_code=500, detail=(
            'Não consegui extrair o frame: ' + (proc.stderr or '')[-200:]))


def _dados_do_corte(pid: str, vid: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None, float]:
    estado, variante, _rascunho = _rastreio(pid, vid)
    jobs = {j.get('job_id'): j for j in estado.get('jobs', [])}
    job = jobs.get(variante.get('render_job_id')) or {}
    duracao = 0.0
    try:
        duracao = float((job.get('result') or {}).get('duration') or 0.0)
    except (TypeError, ValueError):
        duracao = 0.0
    return estado, variante, job, duracao


@router.get('/projects/{pid}/clips/{vid}/cover/frame')
def ext_cover_frame_preview(pid: str, vid: str, segundo: float = 1.0) -> Response:
    """Devolve o frame do instante pedido, para pré-visualizar a capa."""
    _estado, _variante, job, duracao = _dados_do_corte(pid, vid)
    video = _corte_renderizado(pid, job.get('job_id'))
    if not video:
        raise HTTPException(status_code=404, detail='Este corte ainda não tem vídeo renderizado.')

    instante = _segundo_seguro(segundo, duracao)
    cache = data_dir() / 'cache' / 'frames' / f'{pid[:8]}-{vid[:8]}-{instante:.2f}.jpg'
    if not cache.is_file():
        _extrair_frame(video, instante, cache)
    return Response(content=cache.read_bytes(), media_type='image/jpeg',
                    headers={'Cache-Control': 'no-store'})


@router.post('/projects/{pid}/clips/{vid}/cover/frame')
def ext_cover_frame_salvar(pid: str, vid: str, body: dict[str, Any]) -> dict[str, Any]:
    """Grava o frame escolhido como capa do corte (vale também para a publicação)."""
    import shutil

    from backend.services import cover as cover_service
    from backend.services.studio import publish_kit

    _estado, variante, job, duracao = _dados_do_corte(pid, vid)
    video = _corte_renderizado(pid, job.get('job_id'))
    if not video:
        raise HTTPException(status_code=404, detail='Este corte ainda não tem vídeo renderizado.')

    instante = _segundo_seguro(body.get('segundo'), duracao)
    job_id = job.get('job_id')
    estrategia = variante.get('strategy_id') or 'tiktok'
    pasta = cover_service.cover_dir(pid, publish_kit.clip_id(job_id))

    alvo_publicacao = cover_service.cover_path(pid, publish_kit.clip_id(job_id),
                                               publish_kit._publish_slot(estrategia))
    alvo_kit = publish_kit.kit_cover_path(pid, job_id, estrategia)

    # guarda a capa anterior antes de sobrescrever: a troca é reversível
    marca = int(time.time())
    for anterior in (alvo_publicacao, alvo_kit):
        if anterior.is_file():
            try:
                anterior.rename(anterior.with_name(f'{anterior.stem}.anterior-{marca}.jpg'))
            except OSError:
                pass

    _extrair_frame(video, instante, alvo_publicacao)
    # o kit também precisa do frame: grava no slot de design
    shutil.copy2(alvo_publicacao, alvo_kit)

    # se havia capa de IA, ela venceria a escolha: guarda o metadado de lado
    meta = cover_service.meta_path(pid, publish_kit.clip_id(job_id),
                                   publish_kit._publish_slot(estrategia))
    if meta.is_file():
        try:
            meta.rename(meta.with_suffix(f'.anterior-{marca}.json'))
        except OSError:
            pass

    return {'ok': True, 'segundo': instante, 'backup': marca,
            'cover_url': f'/api/v1/ext/projects/{pid}/clips/{vid}/cover?t={marca}'}


@router.post('/projects/{pid}/clips/{vid}/cover/frame/desfazer')
def ext_cover_frame_desfazer(pid: str, vid: str) -> dict[str, Any]:
    """Devolve a capa que estava antes da troca por frame."""
    from backend.services import cover as cover_service
    from backend.services.studio import publish_kit

    _estado, variante, job, _duracao = _dados_do_corte(pid, vid)
    job_id = job.get('job_id')
    if not job_id:
        raise HTTPException(status_code=404, detail='Este corte ainda não tem vídeo renderizado.')

    estrategia = variante.get('strategy_id') or 'tiktok'
    pasta = cover_service.cover_dir(pid, publish_kit.clip_id(job_id))
    backups = sorted(pasta.glob('*.anterior-*.jpg'), reverse=True)
    if not backups:
        raise HTTPException(status_code=404, detail='Não há capa anterior guardada.')

    # restaura o conjunto do backup mais recente
    restaurados = []
    marca = backups[0].stem.rsplit('anterior-', 1)[-1]
    for backup in [b for b in backups if b.stem.endswith(f'anterior-{marca}')]:
        destino = pasta / (backup.stem.rsplit('.anterior-', 1)[0] + '.jpg')
        try:
            backup.replace(destino)
            restaurados.append(destino.name)
        except OSError:
            continue
    for meta_backup in pasta.glob(f'*.anterior-{marca}.json'):
        destino = pasta / (meta_backup.stem.rsplit('.anterior-', 1)[0] + '.json')
        try:
            meta_backup.replace(destino)
        except OSError:
            pass

    if not restaurados:
        raise HTTPException(status_code=500, detail='Não consegui restaurar a capa anterior.')
    return {'ok': True, 'restaurados': restaurados}


# --------------------------------------------------------------------------- #
# cookies do YouTube (sessão logada, para passar do bloqueio anti-robô)
# --------------------------------------------------------------------------- #
@router.get('/youtube/login/status')
def ext_youtube_login_status() -> dict[str, Any]:
    """Situação do login do YouTube feito dentro do TikClip."""
    from backend.services import youtube_login
    return youtube_login.status()


@router.post('/youtube/login/iniciar')
def ext_youtube_login_iniciar(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Abre o navegador do app para a pessoa entrar na conta do Google."""
    from backend.services import youtube_login
    limite = 5
    if isinstance(body, dict):
        try:
            limite = max(1, min(30, int(body.get('limite_min') or 5)))
        except (TypeError, ValueError):
            limite = 5
    return youtube_login.iniciar_login(limite)


@router.post('/youtube/login/cancelar')
def ext_youtube_login_cancelar() -> dict[str, Any]:
    """Fecha o navegador do login."""
    from backend.services import youtube_login
    return youtube_login.cancelar_login()


@router.post('/youtube/login/exportar')
def ext_youtube_login_exportar() -> dict[str, Any]:
    """Mantido por compatibilidade: o login agora é sempre isolado, sem perfil em disco."""
    return {'ok': False,
            'mensagem': 'Use "Entrar no YouTube" para abrir o navegador e fazer o login.'}


@router.get('/youtube/cookies')
def ext_youtube_cookies() -> dict[str, Any]:
    """Situação dos cookies usados no download (sem expor valores)."""
    from backend.services.youtube_cookies import status
    return status()


@router.post('/youtube/cookies/importar')
def ext_youtube_cookies_importar() -> dict[str, Any]:
    """Lê os cookies do navegador embutido do YT Saver (sessão Google logada)."""
    from backend.services.youtube_cookies import renovar
    return renovar()


# --------------------------------------------------------------------------- #
# hashtags personalizadas do projeto
# --------------------------------------------------------------------------- #
@router.get('/projects/{pid}/hashtags')
def ext_hashtags_get(pid: str) -> dict[str, Any]:
    return {'tags': ler_hashtags_projeto(pid)}


def _meta_do_youtube(url: str) -> dict[str, Any]:
    """Lê título, descrição, palavras-chave e hashtags da página — sem baixar o vídeo.

    Uma requisição HTML simples, sem cookies: não passa pela API do player, então
    não esbarra no bloqueio anti-robô que atinge o download.
    """
    import html as _html
    import json as _json
    import urllib.error
    import urllib.request
    from urllib.parse import urlparse

    from backend.services.studio.jobs import _id_do_youtube

    host = (urlparse(url).hostname or '').lower()
    if not any(host == d or host.endswith('.' + d) for d in ('youtube.com', 'youtu.be')):
        raise HTTPException(status_code=400, detail='Por enquanto só leio metadados do YouTube.')
    ident = _id_do_youtube(url)
    if not ident:
        raise HTTPException(status_code=400, detail='Link do YouTube inválido.')

    meta: dict[str, Any] = {'id': ident, 'titulo': '', 'canal': '', 'descricao': '',
                            'palavras_chave': [], 'hashtags': [], 'origem': ''}
    usuario = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
               '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')
    try:
        pedido = urllib.request.Request(
            f'https://www.youtube.com/watch?v={ident}&hl=pt&gl=BR',
            headers={'User-Agent': usuario, 'Accept-Language': 'pt-BR,pt;q=0.9'})
        with urllib.request.urlopen(pedido, timeout=20) as resposta:
            pagina = resposta.read(3 * 1024 * 1024).decode('utf-8', 'replace')

        def _meta(nome: str) -> str:
            achado = re.search(
                rf'<meta[^>]+name="{nome}"[^>]+content="([^"]*)"', pagina, re.IGNORECASE)
            return _html.unescape(achado.group(1)).strip() if achado else ''

        meta['titulo'] = _meta('title')
        meta['descricao'] = _meta('description')
        meta['palavras_chave'] = [p.strip() for p in _meta('keywords').split(',') if p.strip()][:15]
        meta['origem'] = 'pagina'
    except Exception:  # noqa: BLE001 - cai para o oembed
        pass

    if not meta['titulo'] or not meta['canal']:
        try:
            consulta = (f'https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v='
                        f'{ident}&format=json')
            with urllib.request.urlopen(consulta, timeout=12) as resposta:
                dados = _json.load(resposta)
            meta['titulo'] = meta['titulo'] or str(dados.get('title') or '')
            meta['canal'] = str(dados.get('author_name') or '')
            meta['origem'] = meta['origem'] or 'oembed'
        except Exception:  # noqa: BLE001
            pass

    if not meta['origem']:
        raise HTTPException(status_code=502,
                            detail='Não consegui ler a página do vídeo (rede ou link privado).')

    texto = f'{meta["titulo"]} {meta["descricao"]}'
    achados = re.findall(r'#([\w\u00c0-\u024f]{2,30})', texto)
    meta['hashtags'] = [h for h in dict.fromkeys(achados)][:15]

    # tags de verdade do vídeo (as do autor), via yt-dlp — o meta keywords da
    # página é genérico do YouTube e não serve como hashtag
    meta.setdefault('tags_video', [])
    meta.setdefault('categorias', [])
    try:
        import yt_dlp

        from backend.services.studio.jobs import _arquivos_de_cookie
        from backend.utils.ffmpeg_utils import ytdlp_js_runtimes

        opcoes = {'quiet': True, 'no_warnings': True, 'skip_download': True,
                  'noplaylist': True, 'socket_timeout': 20, **ytdlp_js_runtimes()}
        candidatos = _arquivos_de_cookie()
        if candidatos:
            opcoes['cookiefile'] = str(candidatos[0])
        with yt_dlp.YoutubeDL(opcoes) as ydl:
            info = ydl.extract_info(url, download=False, process=False) or {}
        meta['tags_video'] = [str(t) for t in (info.get('tags') or [])][:25]
        meta['categorias'] = [str(c) for c in (info.get('categories') or [])][:3]
        meta['descricao'] = (str(info.get('description') or '') or meta['descricao'])[:2000]
        meta['titulo'] = meta['titulo'] or str(info.get('title') or '')
        meta['canal'] = meta['canal'] or str(info.get('uploader') or '')
        meta['origem'] = 'yt-dlp'
    except Exception:  # noqa: BLE001 - sem cookies/rede ainda dá para usar a página
        pass
    return meta


# termos que não dizem nada sobre o assunto do vídeo
GENERICAS = {
    'video', 'videos', 'compartilhamento', 'celular', 'videofone', 'gratuito', 'envio',
    'youtube', 'youtuber', 'tiktok', 'shorts', 'viral', 'fyp', 'paravoce', 'conteudo',
    'canal', 'inscreva', 'like', 'subscribe', 'brasil', 'portugues', 'podcast',
}

PROMPT_TAGS = (
    'Você escolhe hashtags para um corte de vídeo no TikTok. '
    'Com base no título, na descrição e nas tags do autor, liste de 4 a 6 hashtags em '
    'PORTUGUÊS DO BRASIL, sem #, específicas sobre o ASSUNTO tratado — nunca genéricas '
    'como video, viral, shorts, podcast, brasil. Prefira o tema concreto (nomes de '
    'pessoas, lugares, assuntos) que apareça no material. '
    'Retorne APENAS JSON: {"tags":["...","..."]}'
)


def _filtrar_hashtags(tags: Any) -> list[str]:
    saida = []
    for tag in tags or []:
        limpo = re.sub(r'[^0-9A-Za-zÀ-ÿ_]', '', str(tag)).strip()
        if 2 <= len(limpo) <= 30 and limpo.lower() not in GENERICAS:
            saida.append(limpo)
    return list(dict.fromkeys(saida))[:8]


def _hashtags_do_video(url: str, meta: dict[str, Any]) -> tuple[list[str], str]:
    """IA sobre o conteúdo primeiro; depois hashtags do autor; depois tags do vídeo."""
    texto = ' '.join(filter(None, [
        str(meta.get('titulo') or ''), str(meta.get('descricao') or '')[:2000],
        ' '.join(meta.get('tags_video') or []),
    ])).strip()
    if texto:
        try:
            from backend.services.studio.intelligence import text_json
            resposta = text_json(PROMPT_TAGS, {
                'titulo': meta.get('titulo') or '',
                'canal': meta.get('canal') or '',
                'descricao': str(meta.get('descricao') or '')[:1800],
                'tags_do_autor': (meta.get('tags_video') or [])[:20],
                'categorias': meta.get('categorias') or [],
            })
            tags = _filtrar_hashtags((resposta or {}).get('tags') if isinstance(resposta, dict) else None)
            if tags:
                return tags, 'modelo'
        except Exception:  # noqa: BLE001 - sem IA ainda dá para usar o autor
            pass
    autor = _filtrar_hashtags(meta.get('hashtags') or [])
    if autor:
        return autor[:6], 'autor'
    return _filtrar_hashtags(meta.get('tags_video') or [])[:6], 'tags do vídeo'


@router.post('/projects/{pid}/hashtags/do-link')
def ext_hashtags_do_link(pid: str, body: dict[str, Any]) -> dict[str, Any]:
    """Hashtags a partir do link: tags reais do vídeo + IA sobre título/descrição.

    Antes isso usava `<meta name="keywords">` da página, que é genérico do YouTube
    ("video", "compartilhamento", "celular") e não diz nada sobre o conteúdo.
    """
    url = str(body.get('url') or '').strip()
    if not url:
        raise HTTPException(status_code=400, detail='Cole o link do vídeo original.')
    meta = _meta_do_youtube(url)
    sugestoes, origem = _hashtags_do_video(url, meta)

    atuais = ler_hashtags_projeto(pid)
    juntas = list(atuais)
    vistos = {t.lower() for t in juntas}
    for tag in sugestoes:
        if tag.lower() not in vistos:
            juntas.append(tag)
            vistos.add(tag.lower())
    juntas = juntas[:12]

    caminho = _hashtags_path(pid)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    try:
        caminho.write_text(json.dumps({'tags': juntas}, ensure_ascii=False, indent=2),
                           encoding='utf-8')
    except OSError as exc:
        raise HTTPException(status_code=500,
                            detail=f'Não consegui salvar as hashtags: {exc}') from exc

    return {'ok': True, 'tags': juntas, 'novas': sugestoes,
            'titulo': meta['titulo'], 'canal': meta['canal'],
            'origem': origem, 'link': url}


@router.put('/projects/{pid}/hashtags')
def ext_hashtags_put(pid: str, body: dict[str, Any]) -> dict[str, Any]:
    """Grava as hashtags do projeto; elas entram na publicação de todos os cortes."""
    tags = _limpar_hashtags(body.get('tags'))
    caminho = _hashtags_path(pid)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    try:
        caminho.write_text(json.dumps({'tags': tags}, ensure_ascii=False, indent=2),
                           encoding='utf-8')
    except OSError as exc:
        raise HTTPException(status_code=500,
                            detail=f'Não consegui salvar as hashtags: {exc}') from exc
    return {'ok': True, 'tags': tags}

# --------------------------------------------------------------------------
# Gerar do zero: assunto -> roteiro -> imagens -> voz -> vídeo 1080x1920
# --------------------------------------------------------------------------
@router.post('/gerar/iniciar')
def ext_gerar_iniciar(body: dict[str, Any]) -> dict[str, Any]:
    """Começa a gerar um vídeo novo a partir de um assunto."""
    from backend.services import gerador

    assunto = str(body.get('assunto') or '').strip()
    if len(assunto) < 3:
        raise HTTPException(status_code=422, detail='Escreva o assunto do vídeo.')
    try:
        duracao = float(body.get('duracao') or 30)
    except (TypeError, ValueError):
        duracao = 30.0
    duracao = max(10.0, min(120.0, duracao))
    voz = str(body.get('voz') or '').strip() or None
    try:
        quantidade = int(body.get('quantidade') or 1)
    except (TypeError, ValueError):
        quantidade = 1
    fundo = str(body.get('fundo') or '').strip() or None
    if fundo and gerador.caminho_fundo(fundo) is None:
        raise HTTPException(status_code=422, detail='Vídeo de fundo não encontrado.')
    return gerador.iniciar(assunto, duracao, voz, max(1, min(5, quantidade)), fundo)


@router.get('/gerar/status')
def ext_gerar_status() -> dict[str, Any]:
    from backend.services import gerador

    return gerador.status()


@router.get('/gerar/lista')
def ext_gerar_lista() -> dict[str, Any]:
    from backend.services import gerador

    itens = gerador.lista()
    return {'total': len(itens), 'itens': itens}


@router.get('/gerar/video/{ident}')
def ext_gerar_video(ident: str):
    from backend.services import gerador

    caminho = gerador.caminho_video(ident)
    if not caminho:
        raise HTTPException(status_code=404, detail='Vídeo não encontrado.')
    return FileResponse(str(caminho), media_type='video/mp4')

@router.post('/gerar/publicar')
def ext_gerar_publicar(body: dict[str, Any]) -> dict[str, Any]:
    """Publica um vídeo da galeria no TikTok (conta ativa ou a informada)."""
    from backend.services import gerador

    ident = str(body.get('id') or '').strip()
    if not ident:
        raise HTTPException(status_code=422, detail='Escolha um vídeo da galeria.')
    conta = str(body.get('conta') or '').strip() or None
    return gerador.publicar(ident, conta)


@router.get('/gerar/publicar/status')
def ext_gerar_publicar_status() -> dict[str, Any]:
    from backend.services import gerador

    return gerador.status_publicacao()

@router.post('/gerar/para-projeto')
def ext_gerar_para_projeto(body: dict[str, Any]) -> dict[str, Any]:
    """Coloca um vídeo da galeria como corte do projeto "Gerados com IA"."""
    import json as _json

    from backend.services import gerador
    from backend.services.studio import gerado_clip

    ident = str(body.get('id') or '').strip()
    if not gerador.caminho_video(ident):
        raise HTTPException(status_code=404, detail='Vídeo não encontrado na galeria.')
    meta_path = gerador.PASTA / ident / 'gerado.json'
    try:
        meta = _json.loads(meta_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        meta = {}
    try:
        resultado = gerado_clip.adicionar(ident, gerador.PASTA, meta)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500,
                            detail=f'Não consegui criar o corte: {exc}') from exc
    return {'ok': True, **resultado,
            'mensagem': 'Corte criado no projeto "Gerados com IA".'}

# --------------------------------------------------------------------------
# andamento do projeto (alimenta o "terminal" da tela do projeto)
# --------------------------------------------------------------------------
_FASES = {
    'screening': ('Analisando o material', 10),
    'analyzing': ('Entendendo o conteúdo', 30),
    'planning': ('Montando o plano de cortes', 50),
    'rendering': ('Renderizando os cortes', 65),
    'production': ('Produzindo a peça pronta', 65),
}

# o motor às vezes escreve a mensagem em chinês (herança da base chinesa); a
# interface é toda em pt-BR, então essas mensagens não podem vazar
_CJK = re.compile(r'[\u3000-\u9fff\uff00-\uffef]')


def _sem_cjk(texto: str) -> str:
    """Devolve a mensagem em pt-BR; se vier em chinês, cai no rótulo da fase."""
    if not texto or _CJK.search(texto):
        return ''
    return texto.strip()


_ERROS = {
    'rate_limited': 'a IA atingiu o limite de uso — aguarde alguns minutos ou troque o modelo em Ajustes',
    'quota_exceeded': 'a cota da IA acabou — troque a chave ou o modelo em Ajustes',
    'unauthorized': 'a chave da IA foi recusada — confira em Ajustes',
    'timeout': 'a IA demorou demais para responder',
}


def _erro_legivel(codigo: Any, texto: Any) -> str | None:
    """Mensagem de erro em pt-BR, sem vazar o texto em chinês do motor."""
    codigo = str(codigo or '')
    if codigo in _ERROS:
        return _ERROS[codigo]
    limpo = _sem_cjk(str(texto or ''))
    if limpo:
        return limpo
    if codigo or texto:
        return f'erro no motor ({codigo or "sem código"}) — veja o log'
    return None


@router.get('/projects/{pid}/progresso')
def ext_project_progresso(pid: str) -> dict[str, Any]:
    """Retrato do que está acontecendo: fase, mensagem, % e cortes."""
    if not (projects_dir() / pid).is_dir():
        raise HTTPException(status_code=404, detail='Projeto não encontrado')
    state = _read_studio(pid)
    analise = state.get('analysis') or {}
    jobs = state.get('jobs') or []
    cortes = project_clips(pid)

    fase = str(analise.get('phase') or '')
    mensagem = _sem_cjk(str(analise.get('message') or ''))
    status_analise = str(analise.get('status') or '')
    rotulo, base = _FASES.get(fase, (mensagem or 'Processando', 15))

    ativos = [j for j in jobs if j.get('status') in ('queued', 'preparing', 'running')]
    concluidos = [j for j in jobs if j.get('status') == 'completed']
    falhou = next((j for j in jobs if j.get('status') == 'failed'), None)

    percent = 0
    if ativos:
        media = sum(float(j.get('percent') or 0) for j in ativos) / len(ativos)
        percent = int(base + (media / 100) * (100 - base))
    elif status_analise == 'failed' or falhou:
        percent = int(base)
    elif jobs and len(concluidos) == len(jobs):
        percent = 100 if cortes else 95
    elif cortes:
        percent = 100

    # trabalho em andamento manda mais que um erro antigo: um job pode falhar e
    # ser reenfileirado, e a tela não deve anunciar "interrompido" enquanto roda
    if ativos or status_analise == 'running':
        situacao = 'rodando'
    elif status_analise == 'failed' or falhou:
        situacao = 'falhou'
    elif cortes:
        situacao = 'concluido'
    else:
        situacao = 'parado'

    return {
        'ativo': situacao == 'rodando',
        'situacao': situacao,
        'fase': rotulo,
        'fase_id': fase,
        'mensagem': mensagem,
        'percent': max(0, min(100, percent)),
        'analysis_status': status_analise,
        'cortes': {'total': len(cortes),
                   'prontos': sum(1 for c in cortes if c['status'] == 'pronto'),
                   'publicados': sum(1 for c in cortes if c['status'] == 'publicado')},
        'jobs': [{'id': str(j.get('job_id') or '')[:8],
                  'status': j.get('status'),
                  'percent': int(float(j.get('percent') or 0))} for j in jobs],
        'erro': _erro_legivel(analise.get('error_code') or (falhou or {}).get('error_code'),
                            analise.get('error') or (falhou or {}).get('error')),
    }

@router.get('/projects/{pid}/mais-cortes/disponiveis')
def ext_mais_cortes_disponiveis(pid: str) -> dict[str, Any]:
    """Quantos trechos pontuados pela análise ainda não viraram corte."""
    from backend.services.studio import mais_cortes

    if not (projects_dir() / pid).is_dir():
        raise HTTPException(status_code=404, detail='Projeto não encontrado')
    livres = mais_cortes.candidatos_livres(pid)
    return {'total': len(livres),
            'itens': [{'titulo': c['titulo'], 'inicio': c['inicio'], 'fim': c['fim'],
                       'nota': c['nota']} for c in livres[:10]]}


@router.post('/projects/{pid}/mais-cortes')
def ext_mais_cortes(pid: str, body: dict[str, Any]) -> dict[str, Any]:
    """Cria cortes novos com os trechos que sobraram da análise (sem gastar IA)."""
    from backend.services.studio import mais_cortes

    if not (projects_dir() / pid).is_dir():
        raise HTTPException(status_code=404, detail='Projeto não encontrado')
    try:
        quantidade = int(body.get('quantidade') or 2)
    except (TypeError, ValueError):
        quantidade = 2
    resultado = mais_cortes.adicionar(pid, quantidade)
    if not resultado.get('criados'):
        resultado['precisa_ia'] = True
    return resultado


@router.post('/projects/{pid}/mais-cortes/reanalisar')
def ext_mais_cortes_reanalisar(pid: str) -> dict[str, Any]:
    """Reanalisa pedindo momentos ainda não usados (consome cota da IA)."""
    import requests as _requests

    if not (projects_dir() / pid).is_dir():
        raise HTTPException(status_code=404, detail='Projeto não encontrado')
    if not SELF_PORT:
        raise HTTPException(status_code=503, detail='Motor indisponível')
    try:
        resposta = _requests.post(f'http://127.0.0.1:{SELF_PORT}/api/v1/studio/{pid}/analyze',
                                  timeout=120)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f'Falha ao reanalisar: {exc}') from exc
    if resposta.status_code >= 400:
        raise HTTPException(status_code=resposta.status_code,
                            detail='O motor recusou a reanálise (veja o andamento).')
    return {'ok': True, 'mensagem': 'Reanalisando: acompanhe no painel de andamento.'}

# --------------------------------------------------------------------------
# tendências do TikTok Studio (pauta, não cópia)
# --------------------------------------------------------------------------
@router.get('/tendencias')
def ext_tendencias() -> dict[str, Any]:
    """Lista em cache + situação da última leitura."""
    from backend.services import tendencias

    itens = tendencias.lista()
    estado = tendencias.status()
    return {'itens': itens, 'total': len(itens), **estado}


@router.post('/tendencias/atualizar')
def ext_tendencias_atualizar(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Abre a página de inspiração com a sessão logada e relê os tópicos."""
    from backend.services import tendencias

    limite = 25
    if isinstance(body, dict):
        try:
            limite = max(5, min(50, int(body.get('limite') or 25)))
        except (TypeError, ValueError):
            limite = 25
    return tendencias.atualizar(limite)

# --------------------------------------------------------------------------
# vídeo de fundo: envio e listagem
# --------------------------------------------------------------------------
@router.post('/gerar/fundos')
async def ext_gerar_fundo_enviar(file: UploadFile = File(...)) -> dict[str, Any]:
    """Recebe um vídeo do computador para servir de fundo nas próximas gerações."""
    from backend.services import gerador

    dados = await file.read()
    try:
        resultado = gerador.salvar_fundo(file.filename or 'fundo.mp4', dados)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await file.close()
    return {'ok': True, **resultado}


@router.get('/gerar/fundos')
def ext_gerar_fundos() -> dict[str, Any]:
    from backend.services import gerador

    itens = gerador.listar_fundos()
    return {'total': len(itens), 'itens': itens}

# --------------------------------------------------------------------------
# baixar vídeos do TikTok (material para outra ferramenta)
# --------------------------------------------------------------------------
@router.post('/baixar/tiktok')
def ext_baixar_tiktok(body: dict[str, Any]) -> dict[str, Any]:
    from backend.services import baixar_tiktok

    return baixar_tiktok.iniciar(str(body.get('url') or ''))


@router.get('/baixar/status')
def ext_baixar_status() -> dict[str, Any]:
    from backend.services import baixar_tiktok

    return baixar_tiktok.status()


@router.get('/baixar/lista')
def ext_baixar_lista() -> dict[str, Any]:
    from backend.services import baixar_tiktok

    itens = baixar_tiktok.lista()
    return {'total': len(itens), 'itens': itens}


@router.get('/baixar/video/{ident}')
def ext_baixar_video(ident: str):
    from backend.services import baixar_tiktok

    caminho = baixar_tiktok.caminho_video(ident)
    if not caminho:
        raise HTTPException(status_code=404, detail='Vídeo não encontrado.')
    return FileResponse(str(caminho), media_type='video/mp4')


@router.post('/baixar/para-fundo')
def ext_baixar_para_fundo(body: dict[str, Any]) -> dict[str, Any]:
    """Copia o baixado para a pasta de fundos, para usar no modo Gerar."""
    from backend.services import baixar_tiktok, gerador

    ident = str(body.get('id') or '').strip()
    origem = baixar_tiktok.caminho_video(ident)
    if not origem:
        raise HTTPException(status_code=404, detail='Vídeo não encontrado.')
    try:
        resultado = gerador.salvar_fundo(f'baixado-{ident}{origem.suffix}', origem.read_bytes())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {'ok': True, **resultado,
            'mensagem': 'Disponível como vídeo de fundo na aba Gerar.'}

@router.post('/baixar/cancelar')
def ext_baixar_cancelar() -> dict[str, Any]:
    from backend.services import baixar_tiktok

    return baixar_tiktok.cancelar()

# --------------------------------------------------------------------------
# mini editor de tela dividida (conteúdo em cima, vídeo de foco embaixo)
# --------------------------------------------------------------------------
@router.get('/split/fontes')
def ext_split_fontes() -> dict[str, Any]:
    from backend.services import split

    itens = split.fontes()
    return {'total': len(itens), 'itens': itens}


@router.post('/split/iniciar')
def ext_split_iniciar(body: dict[str, Any]) -> dict[str, Any]:
    from backend.services import split

    principal = str(body.get('principal') or '').strip()
    foco = str(body.get('foco') or '').strip()
    if not principal or not foco:
        raise HTTPException(status_code=422, detail='Escolha o conteúdo e o vídeo de foco.')
    if split._resolver(principal) is None or split._resolver(foco) is None:
        raise HTTPException(status_code=422, detail='Vídeo não encontrado.')
    try:
        divisao = float(body.get('divisao') or 0.5)
    except (TypeError, ValueError):
        divisao = 0.5
    audio = 'foco' if str(body.get('audio') or '') == 'foco' else 'principal'
    titulo = str(body.get('titulo') or '').strip()[:120]
    return split.iniciar(principal, foco, divisao, audio, titulo)


@router.get('/split/status')
def ext_split_status() -> dict[str, Any]:
    from backend.services import split

    return split.status()


@router.get('/split/lista')
def ext_split_lista() -> dict[str, Any]:
    from backend.services import split

    itens = split.lista()
    return {'total': len(itens), 'itens': itens}


@router.get('/split/video/{ident}')
def ext_split_video(ident: str):
    from backend.services import split

    caminho = split.caminho_video(ident)
    if not caminho:
        raise HTTPException(status_code=404, detail='Vídeo não encontrado.')
    return FileResponse(str(caminho), media_type='video/mp4')

# --------------------------------------------------------------------------
# dublar: transcrever, tirar o som original e recriar a fala
# --------------------------------------------------------------------------
@router.post('/dublar/enviar')
async def ext_dublar_enviar(file: UploadFile = File(...)) -> dict[str, Any]:
    from backend.services import dublar

    dados = await file.read()
    try:
        resultado = dublar.salvar_video(file.filename or 'video.mp4', dados)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await file.close()
    return {'ok': True, **resultado}


@router.get('/dublar/entradas')
def ext_dublar_entradas() -> dict[str, Any]:
    from backend.services import dublar

    itens = dublar.listar_entradas()
    return {'total': len(itens), 'itens': itens}


@router.post('/dublar/iniciar')
def ext_dublar_iniciar(body: dict[str, Any]) -> dict[str, Any]:
    from backend.services import dublar

    ident = str(body.get('id') or '').strip()
    voz = str(body.get('voz') or '').strip() or None
    modelo = str(body.get('modelo') or 'base').strip()
    if modelo not in ('tiny', 'base', 'small', 'medium'):
        modelo = 'base'
    return dublar.iniciar(ident, voz, modelo)


@router.get('/dublar/status')
def ext_dublar_status() -> dict[str, Any]:
    from backend.services import dublar

    return dublar.status()


@router.get('/dublar/lista')
def ext_dublar_lista() -> dict[str, Any]:
    from backend.services import dublar

    itens = dublar.lista()
    return {'total': len(itens), 'itens': itens}


@router.get('/dublar/video/{ident}')
def ext_dublar_video(ident: str):
    from backend.services import dublar

    caminho = dublar.caminho_video(ident)
    if not caminho:
        raise HTTPException(status_code=404, detail='Vídeo não encontrado.')
    return FileResponse(str(caminho), media_type='video/mp4')

@router.post('/dublar/publicar')
def ext_dublar_publicar(body: dict[str, Any]) -> dict[str, Any]:
    """Publica um vídeo dublado na conta informada (ou na ativa)."""
    from backend.services import dublar

    ident = str(body.get('id') or '').strip()
    if not ident:
        raise HTTPException(status_code=422, detail='Escolha um vídeo dublado.')
    conta = str(body.get('conta') or '').strip() or None
    return dublar.publicar(ident, conta)


@router.get('/dublar/publicar/status')
def ext_dublar_publicar_status() -> dict[str, Any]:
    from backend.services import dublar

    return dublar.status_publicacao()

@router.post('/split/publicar')
def ext_split_publicar(body: dict[str, Any]) -> dict[str, Any]:
    """Publica uma montagem na conta informada (ou na ativa)."""
    from backend.services import split

    ident = str(body.get('id') or '').strip()
    if not ident:
        raise HTTPException(status_code=422, detail='Escolha uma montagem.')
    conta = str(body.get('conta') or '').strip() or None
    return split.publicar(ident, conta)


@router.get('/split/publicar/status')
def ext_split_publicar_status() -> dict[str, Any]:
    from backend.services import split

    return split.status_publicacao()
