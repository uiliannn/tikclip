"""Rotas extras do TikClip Web (Edição SupoClip).

Adiciona:
- Gerenciamento dual de IA (Google Gemini e DeepSeek) com chave seletora rápida
- Teste individual de conexão para Gemini (SDK google-genai + endpoint OpenAI) e DeepSeek
- Configurações visuais de legenda padrão com preview (fonte, tamanho, cor, posição)
- Gerenciamento de chaves de API e configuração MCP para a aba "Agentes e API"
"""
from __future__ import annotations

import json
import os
import secrets
import time
import threading
import logging

logger = logging.getLogger('tikclip.supo_api')
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter()


def _data_dir() -> Path:
    raw = os.getenv('TIKCLIP_DATA_DIR')
    if raw:
        return Path(raw)
    return Path(os.getenv('APPDATA', str(Path.home()))) / 'TikClip'


def _supo_settings_path() -> Path:
    return _data_dir() / 'supoclip-web-settings.json'


DEFAULT_SUPO_SETTINGS: dict[str, Any] = {
    'language': 'pt-BR',
    'active_provider': 'deepseek',
    'gemini_model': 'gemini-3.5-flash',
    'deepseek_model': 'deepseek-chat',
    'subtitle': {
        'font_family': 'Tiktoksans Regular',
        'font_size': 24,
        'text_color': '#FFFFFF',
        'stroke_color': '#000000',
        'highlight_color': '#FFD600',
        'position': 'bottom',
    },
    'defaults': {
        'duration_mode': 'medium',   # auto, short (15-30), medium (30-60), long (60-90)
        'duration_seconds': 45,
        'aspect': 'portrait',        # portrait (9:16), landscape (16:9), square (1:1)
        'goal': 'auto',
        'clean_fillers': True,
        'auto_start': True,
    },
    'notifications': {
        'sound_on_complete': True,
        'browser_notify': True,
    },
    'api_keys': [],
}


def load_supo_settings() -> dict[str, Any]:
    path = _supo_settings_path()
    data = json.loads(json.dumps(DEFAULT_SUPO_SETTINGS))
    if path.is_file():
        try:
            saved = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(saved, dict):
                for k, v in saved.items():
                    if isinstance(v, dict) and isinstance(data.get(k), dict):
                        data[k].update(v)
                    else:
                        data[k] = v
        except Exception:
            pass
    return data


def save_supo_settings(data: dict[str, Any]) -> dict[str, Any]:
    path = _supo_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return data


def _mask_key(key: str | None) -> str:
    if not key:
        return ''
    k = key.strip()
    if len(k) <= 10:
        return '*' * len(k)
    return f'{k[:6]}••••{k[-4:]}'


def _ensure_dual_connections() -> tuple[Any, dict[str, Any], dict[str, Any]]:
    """Garante que ai-model-settings.json tenha conexões separadas para Gemini e DeepSeek."""
    from backend.services import ai_model_settings as ams

    atual = ams.for_editing()
    dados = atual.model_dump()
    conexoes: list[dict[str, Any]] = dados.get('connections') or []

    gemini_conn = next((c for c in conexoes if c.get('provider') == 'gemini'), None)
    if not gemini_conn:
        gemini_conn = {
            'id': 'gemini-analise',
            'name': 'Google Gemini',
            'provider': 'gemini',
            'base_url': '',
            'api_key': '',
            'image_api': 'auto',
            'image_base_url': '',
        }
        conexoes.append(gemini_conn)

    deepseek_conn = next((c for c in conexoes if c.get('provider') == 'deepseek'), None)
    if not deepseek_conn:
        deepseek_conn = {
            'id': 'deepseek-analise',
            'name': 'DeepSeek',
            'provider': 'deepseek',
            'base_url': 'https://api.deepseek.com',
            'api_key': '',
            'image_api': 'auto',
            'image_base_url': '',
        }
        conexoes.append(deepseek_conn)

    dados['connections'] = conexoes
    return ams, dados, {'gemini': gemini_conn, 'deepseek': deepseek_conn}


# --------------------------------------------------------------------------- #
# Rotas de IA Dual (Google Gemini <-> DeepSeek)
# --------------------------------------------------------------------------- #
GEMINI_MODELS = [
    {'id': 'gemini-3.5-flash', 'label': 'Gemini 3.5 Flash (Recomendado · Rápido e Preciso)'},
    {'id': 'gemini-3.5-flash-lite', 'label': 'Gemini 3.5 Flash Lite (Ultra Rápido · Baixa Latência)'},
    {'id': 'gemini-3.8-flash', 'label': 'Gemini 3.8 Flash (Nova Geração)'},
    {'id': 'gemini-flash-latest', 'label': 'Gemini Flash Latest (Última Versão Automática)'},
]

DEEPSEEK_MODELS = [
    {'id': 'deepseek-chat', 'label': 'DeepSeek V3 Chat (Recomendado · Rápido)'},
    {'id': 'deepseek-reasoner', 'label': 'DeepSeek R1 Reasoner (Raciocínio Profundo)'},
]


@router.get('/ai')
def get_dual_ai_config() -> dict[str, Any]:
    """Retorna o estado completo do seletor Gemini vs DeepSeek."""
    ams, dados, conns = _ensure_dual_connections()
    supo = load_supo_settings()

    gem = conns['gemini']
    ds = conns['deepseek']

    analise = dados.get('analysis') or {}
    conn_ativa_id = analise.get('connection_id')
    modelo_ativo = analise.get('model') or ''

    if conn_ativa_id == gem.get('id'):
        active_provider = 'gemini'
        gemini_model = modelo_ativo or supo.get('gemini_model') or 'gemini-3.5-flash'
        deepseek_model = supo.get('deepseek_model') or 'deepseek-chat'
    elif conn_ativa_id == ds.get('id'):
        active_provider = 'deepseek'
        deepseek_model = modelo_ativo or supo.get('deepseek_model') or 'deepseek-chat'
        gemini_model = supo.get('gemini_model') or 'gemini-3.5-flash'
    else:
        active_provider = supo.get('active_provider') or 'deepseek'
        gemini_model = supo.get('gemini_model') or 'gemini-3.5-flash'
        deepseek_model = supo.get('deepseek_model') or 'deepseek-chat'

    gem_key = (gem.get('api_key') or '').strip()
    ds_key = (ds.get('api_key') or '').strip()

    transcricao = dados.get('transcription') or {}

    return {
        'ok': True,
        'active_provider': active_provider,
        'active_model': gemini_model if active_provider == 'gemini' else deepseek_model,
        'analysis_mode': dados.get('analysis_mode') or 'auto',
        'whisper_model': transcricao.get('model') or 'large-v3-turbo',
        'max_clips': dados.get('max_clips_per_collection') or 5,
        'min_score': dados.get('min_score_threshold') or 0.7,
        'gemini': {
            'connection_id': gem.get('id'),
            'has_key': bool(gem_key),
            'masked_key': _mask_key(gem_key),
            'model': gemini_model,
            'models': GEMINI_MODELS,
        },
        'deepseek': {
            'connection_id': ds.get('id'),
            'has_key': bool(ds_key),
            'masked_key': _mask_key(ds_key),
            'base_url': ds.get('base_url') or 'https://api.deepseek.com',
            'model': deepseek_model,
            'models': DEEPSEEK_MODELS,
        },
    }


@router.post('/ai')
def save_dual_ai_config(body: dict[str, Any]) -> dict[str, Any]:
    """Salva as chaves do Gemini e/ou DeepSeek e define qual provedor está ativo."""
    ams, dados, conns = _ensure_dual_connections()
    supo = load_supo_settings()

    gem = conns['gemini']
    ds = conns['deepseek']

    # Atualiza chaves se fornecidas (não sobrescreve se vier vazio)
    if 'gemini_api_key' in body:
        nova_gem = str(body.get('gemini_api_key') or '').strip()
        if nova_gem:
            gem['api_key'] = nova_gem

    if 'deepseek_api_key' in body:
        nova_ds = str(body.get('deepseek_api_key') or '').strip()
        if nova_ds:
            ds['api_key'] = nova_ds

    if 'deepseek_base_url' in body:
        nova_url = str(body.get('deepseek_base_url') or '').strip()
        if nova_url:
            ds['base_url'] = nova_url

    gemini_model = str(body.get('gemini_model') or supo.get('gemini_model') or 'gemini-3.5-flash').strip()
    deepseek_model = str(body.get('deepseek_model') or supo.get('deepseek_model') or 'deepseek-chat').strip()

    active_provider = str(body.get('active_provider') or supo.get('active_provider') or 'deepseek').strip().lower()
    if active_provider not in ('gemini', 'deepseek'):
        active_provider = 'deepseek'

    supo['active_provider'] = active_provider
    supo['gemini_model'] = gemini_model
    supo['deepseek_model'] = deepseek_model
    save_supo_settings(supo)

    # Atualiza o vínculo ativo no motor do TikClip
    if active_provider == 'gemini':
        dados['analysis'] = {
            'connection_id': gem['id'],
            'model': gemini_model,
            'capability': 'auto',
        }
    else:
        dados['analysis'] = {
            'connection_id': ds['id'],
            'model': deepseek_model,
            'capability': 'auto',
        }

    if body.get('analysis_mode') in ('auto', 'visual', 'subtitle'):
        dados['analysis_mode'] = body['analysis_mode']

    if body.get('whisper_model'):
        dados.setdefault('transcription', {})
        dados['transcription']['model'] = str(body['whisper_model']).strip()

    if body.get('max_clips') is not None:
        try:
            dados['max_clips_per_collection'] = max(1, min(20, int(body['max_clips'])))
        except (TypeError, ValueError):
            pass

    try:
        validado = ams.ModelSettings.model_validate(dados)
        ams.save(validado)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f'Erro ao salvar configuração de IA: {exc}') from exc

    return get_dual_ai_config()


@router.post('/ai/switch')
def switch_ai_provider(body: dict[str, Any]) -> dict[str, Any]:
    """Troca rapidamente entre 'gemini' e 'deepseek' com 1 clique."""
    provider = str(body.get('provider') or '').strip().lower()
    if provider not in ('gemini', 'deepseek'):
        raise HTTPException(status_code=400, detail="Provedor inválido. Use 'gemini' ou 'deepseek'.")
    return save_dual_ai_config({'active_provider': provider})


@router.post('/ai/test')
def test_ai_provider(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Testa a conexão com o provedor especificado ('gemini' ou 'deepseek') ou o ativo."""
    import requests

    ams, dados, conns = _ensure_dual_connections()
    supo = load_supo_settings()
    payload = body or {}

    provider = str(payload.get('provider') or supo.get('active_provider') or 'deepseek').strip().lower()
    inicio = time.time()

    if provider == 'gemini':
        gem = conns['gemini']
        api_key = str(payload.get('api_key') or gem.get('api_key') or '').strip()
        model = str(payload.get('model') or supo.get('gemini_model') or 'gemini-3.5-flash').strip()
        if not api_key:
            return {'ok': False, 'provider': 'gemini', 'modelo': model, 'detalhe': 'Informe a chave da API do Google Gemini.'}

        # Tenta primeiro pelo SDK oficial google-genai
        try:
            from google import genai
            client = genai.Client(api_key=api_key)
            resp = client.models.generate_content(
                model=model,
                contents='Responda apenas com a palavra: CONECTADO',
            )
            ms = int((time.time() - inicio) * 1000)
            texto = (getattr(resp, 'text', '') or 'CONECTADO').strip()
            return {
                'ok': True,
                'provider': 'gemini',
                'modelo': model,
                'latencia_ms': ms,
                'resposta': texto[:120],
            }
        except Exception as sdk_err:
            # Fallback para endpoint OpenAI-compatible do Gemini
            try:
                url = 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
                r = requests.post(
                    url,
                    json={
                        'model': model,
                        'max_tokens': 12,
                        'messages': [{'role': 'user', 'content': 'Responda apenas: CONECTADO'}],
                    },
                    headers={'Authorization': f'Bearer {api_key}'},
                    timeout=20,
                )
                ms = int((time.time() - inicio) * 1000)
                if r.ok:
                    choices = r.json().get('choices') or []
                    txt = ((choices[0] if choices else {}).get('message') or {}).get('content') or 'OK'
                    return {
                        'ok': True,
                        'provider': 'gemini',
                        'modelo': model,
                        'latencia_ms': ms,
                        'resposta': txt.strip()[:120],
                    }
                return {
                    'ok': False,
                    'provider': 'gemini',
                    'modelo': model,
                    'detalhe': f'Erro ({r.status_code}): {str(sdk_err)[:160]}',
                }
            except Exception:
                return {
                    'ok': False,
                    'provider': 'gemini',
                    'modelo': model,
                    'detalhe': f'Falha no Gemini: {str(sdk_err)[:180]}',
                }

    else:
        ds = conns['deepseek']
        api_key = str(payload.get('api_key') or ds.get('api_key') or '').strip()
        base_url = str(payload.get('base_url') or ds.get('base_url') or 'https://api.deepseek.com').strip().rstrip('/')
        model = str(payload.get('model') or supo.get('deepseek_model') or 'deepseek-chat').strip()
        if not api_key:
            return {'ok': False, 'provider': 'deepseek', 'modelo': model, 'detalhe': 'Informe a chave da API do DeepSeek.'}

        url = f'{base_url}/chat/completions'
        try:
            r = requests.post(
                url,
                json={
                    'model': model,
                    'max_tokens': 12,
                    'messages': [{'role': 'user', 'content': 'Responda apenas: CONECTADO'}],
                },
                headers={'Authorization': f'Bearer {api_key}'},
                timeout=20,
            )
            ms = int((time.time() - inicio) * 1000)
            if r.ok:
                choices = r.json().get('choices') or []
                txt = ((choices[0] if choices else {}).get('message') or {}).get('content') or 'CONECTADO'
                return {
                    'ok': True,
                    'provider': 'deepseek',
                    'modelo': model,
                    'latencia_ms': ms,
                    'resposta': txt.strip()[:120],
                }
            return {
                'ok': False,
                'provider': 'deepseek',
                'modelo': model,
                'detalhe': f'Status {r.status_code}: {r.text[:160]}',
            }
        except Exception as exc:
            return {
                'ok': False,
                'provider': 'deepseek',
                'modelo': model,
                'detalhe': f'Erro de conexão: {str(exc)[:180]}',
            }


# --------------------------------------------------------------------------- #
# Configurações Gerais, Legendas Padrão e Chaves MCP/API
# --------------------------------------------------------------------------- #
@router.get('/settings')
def get_settings() -> dict[str, Any]:
    return {'ok': True, 'settings': load_supo_settings()}


@router.post('/settings')
def update_settings(body: dict[str, Any]) -> dict[str, Any]:
    atual = load_supo_settings()
    if 'language' in body:
        atual['language'] = str(body['language'])
    if isinstance(body.get('subtitle'), dict):
        atual['subtitle'].update(body['subtitle'])
    if isinstance(body.get('defaults'), dict):
        atual['defaults'].update(body['defaults'])
    if isinstance(body.get('notifications'), dict):
        atual['notifications'].update(body['notifications'])
    save_supo_settings(atual)
    return {'ok': True, 'settings': atual}


@router.get('/api-keys')
def list_api_keys() -> dict[str, Any]:
    atual = load_supo_settings()
    keys = atual.get('api_keys') or []
    return {'ok': True, 'items': keys}


@router.post('/api-keys')
def create_api_key(body: dict[str, Any]) -> dict[str, Any]:
    nome = str((body or {}).get('name') or 'Chave MCP Local').strip()[:80]
    atual = load_supo_settings()
    keys: list[dict[str, Any]] = atual.get('api_keys') or []
    raw_token = f'sk_supo_{secrets.token_hex(16)}'
    item = {
        'id': secrets.token_hex(6),
        'name': nome,
        'token': raw_token,
        'masked': f'{raw_token[:11]}••••{raw_token[-4:]}',
        'created_at': time.strftime('%d/%m/%Y %H:%M'),
    }
    keys.insert(0, item)
    atual['api_keys'] = keys
    save_supo_settings(atual)
    return {'ok': True, 'created': item, 'items': keys}


@router.delete('/api-keys/{key_id}')
def delete_api_key(key_id: str) -> dict[str, Any]:
    atual = load_supo_settings()
    keys = [k for k in (atual.get('api_keys') or []) if k.get('id') != key_id]
    atual['api_keys'] = keys
    save_supo_settings(atual)
    return {'ok': True, 'items': keys}


# --------------------------------------------------------------------------- #
# Sugestão de Hashtags com IA para o Projeto
# --------------------------------------------------------------------------- #
@router.post('/projects/{pid}/hashtags/ai-suggest')
def supo_suggest_hashtags(pid: str) -> dict[str, Any]:
    """Usa a IA configurada (Gemini ou DeepSeek) ou metadados para sugerir hashtags virais para o projeto."""
    proj_dir = _data_dir() / 'projects' / pid
    if not proj_dir.is_dir():
        raise HTTPException(status_code=404, detail='Projeto não encontrado.')

    studio_path = proj_dir / 'metadata' / 'studio.json'
    title = ''
    if studio_path.is_file():
        try:
            studio_data = json.loads(studio_path.read_text(encoding='utf-8'))
            source_meta = studio_data.get('source_meta') or {}
            title = source_meta.get('title') or ''
            if not title:
                drafts = studio_data.get('drafts') or []
                if drafts:
                    title = drafts[0].get('title') or ''
        except Exception:
            pass

    sugestoes: list[str] = []

    _ams, _dados, conns = _ensure_dual_connections()
    supo = load_supo_settings()
    provider = supo.get('active_provider') or 'gemini'

    prompt = (
        f"Gere de 6 a 8 hashtags curtas e muito virais para o TikTok em português do Brasil "
        f"para um vídeo sobre o tema/título: '{title}'. "
        f"Responda apenas as palavras separadas por espaço, todas minúsculas, sem o caractere # e sem numeração."
    )

    if provider == 'gemini':
        gem = conns['gemini']
        key = (gem.get('api_key') or '').strip()
        model_name = supo.get('gemini_model') or 'gemini-3.5-flash'
        if key:
            try:
                from google import genai
                client = genai.Client(api_key=key)
                resp = client.models.generate_content(model=model_name, contents=prompt)
                texto = getattr(resp, 'text', '') or ''
                sugestoes = [t.strip().replace('#', '').lower() for t in texto.split() if len(t.strip()) > 1]
            except Exception:
                pass
    elif provider == 'deepseek':
        ds = conns['deepseek']
        key = (ds.get('api_key') or '').strip()
        model_name = supo.get('deepseek_model') or 'deepseek-chat'
        if key:
            try:
                import httpx
                resp = httpx.post(
                    'https://api.deepseek.com/chat/completions',
                    headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                    json={
                        'model': model_name,
                        'messages': [{'role': 'user', 'content': prompt}],
                        'temperature': 0.7,
                    },
                    timeout=15.0,
                )
                if resp.status_code == 200:
                    texto = resp.json()['choices'][0]['message']['content']
                    sugestoes = [t.strip().replace('#', '').lower() for t in texto.split() if len(t.strip()) > 1]
            except Exception:
                pass

    if not sugestoes:
        import re
        palavras = re.findall(r'[a-zA-Z0-9áéíóúâêîôûãõç]{4,}', title.lower())
        stopwords = {'para', 'como', 'sobre', 'depois', 'quem', 'mais', 'pode', 'onde', 'quando', 'fazer', 'esta', 'esse', 'pelo', 'pela', 'com', 'nomes', 'grande', 'grandes'}
        candidatos = [p for p in palavras if p not in stopwords]
        sugestoes = candidatos[:6] + ['viral', 'fyp', 'cortes', 'curiosidades']

    import re
    sugestoes_limpas: list[str] = []
    for s in sugestoes:
        limpo = re.sub(r'[^a-z0-9]', '', s.lower())
        if limpo and len(limpo) >= 3 and limpo not in sugestoes_limpas:
            sugestoes_limpas.append(limpo)

    ht_path = proj_dir / 'metadata' / 'hashtags-ext.json'
    atuais: list[str] = []
    if ht_path.is_file():
        try:
            atuais = json.loads(ht_path.read_text(encoding='utf-8')).get('tags') or []
        except Exception:
            pass

    novas = list(atuais)
    for s in sugestoes_limpas:
        if s not in novas:
            novas.append(s)

    novas = novas[:15]
    ht_path.parent.mkdir(parents=True, exist_ok=True)
    ht_path.write_text(json.dumps({'tags': novas}, ensure_ascii=False, indent=2), encoding='utf-8')

    return {'ok': True, 'tags': novas, 'adicionadas': [s for s in novas if s not in atuais]}


# --------------------------------------------------------------------------- #
# Publicação Global: Todos os Cortes Prontos de Todos os Projetos
# --------------------------------------------------------------------------- #
@router.post('/publish/all-pending')
def supo_publish_all_pending(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Inicia a publicação em lote de todos os cortes prontos de todos os projetos."""
    import threading
    import api_ext

    data = payload or {}
    account = data.get('account_id') or 'default'
    interval = int(data.get('interval_min') or 0)
    confirmado = bool(data.get('confirmado'))

    root = api_ext.projects_dir()
    targets: list[tuple[str, str, str]] = []  # (pid, vid, title)
    if root.is_dir():
        for entry in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not entry.is_dir():
                continue
            pid = entry.name
            try:
                clips = api_ext.project_clips(pid)
                for c in clips:
                    if c.get('status') == 'pronto' and c.get('has_video'):
                        targets.append((pid, c['id'], c.get('title') or 'Corte'))
            except Exception:
                continue

    if not targets:
        raise HTTPException(status_code=400, detail='Nenhum corte pronto encontrado para publicação nos projetos.')

    # Guarda de sessão por IP
    ip_atual = api_ext._ip_publico()
    registro = api_ext._ler_sessao_ip().get(account) or {}
    ip_sessao = registro.get('ip')
    if ip_atual and ip_sessao and ip_atual != ip_sessao and not confirmado:
        raise HTTPException(
            status_code=409,
            detail=(
                f'A sessão desta conta foi criada noutro IP ({ip_sessao}) e agora você está em '
                f'{ip_atual}. Publicar assim pode fazer o TikTok pedir verificação e derrubar '
                'o login. Confirme para continuar mesmo assim.'
            ),
        )
    if ip_atual and (not ip_sessao or confirmado):
        api_ext._gravar_sessao_ip(account, ip_atual)

    with api_ext._PUB_LOCK:
        if api_ext._PUB.get('running'):
            raise HTTPException(status_code=409, detail='Já existe uma publicação em andamento.')

    def _loop_global():
        with api_ext._PUB_LOCK:
            api_ext._PUB.update(
                running=True,
                stop=False,
                index=0,
                total=len(targets),
                message='Iniciando publicação de todos os projetos...',
                account=account,
                project_id='todos',
            )
        api_ext._pub_log(f'Iniciando publicação global ({len(targets)} cortes no total) na conta "{account}"')

        total = len(targets)
        published = 0
        failed = 0

        for idx, (pid, vid, title) in enumerate(targets, start=1):
            with api_ext._PUB_LOCK:
                if api_ext._PUB.get('stop'):
                    api_ext._pub_log('Publicação interrompida pelo usuário.')
                    break
                api_ext._PUB['index'] = idx
                api_ext._PUB['message'] = f'Publicando {idx}/{total}: {title[:40]}'

            clip = api_ext._variant_payload(pid, vid)
            if not clip or not clip.get('has_video'):
                failed += 1
                continue

            state = api_ext._read_studio(pid)
            jobs = {j['job_id']: j for j in state.get('jobs', []) if j.get('job_id')}
            variant = next((v for v in state.get('output_variants', []) if v.get('id') == vid), {})
            job = jobs.get(variant.get('render_job_id'))
            video = api_ext._video_path(pid, job)
            if not video:
                failed += 1
                continue

            cover = api_ext._cover_path(pid, job.get('job_id') if job else None, vid)
            api_ext._pub_log(f'[{idx}/{total}] Enviando "{clip["title"]}" (Projeto {pid[:8]})...')

            def progress(msg: str, _idx=idx, _t=total):
                if msg:
                    api_ext._pub_log(f'[{_idx}/{_t}] {msg}')

            try:
                result = api_ext.ttl.publish_video(
                    video,
                    clip['title'],
                    clip['description'],
                    clip['tags'],
                    cover,
                    account=account,
                    progress=progress,
                )
                try:
                    api_ext._registrar_publicacao(pid, clip, job, variant, video, result, account)
                except Exception:
                    pass
                published += 1
                api_ext._pub_log(f'[{idx}/{total}] Publicado com sucesso no TikTok [OK]')
            except Exception as exc:
                failed += 1
                api_ext._pub_log(f'[{idx}/{total}] Falha no envio: {exc}')

            if idx >= total or not interval:
                continue

            wait_s = int(interval * 60)
            api_ext._pub_log(f'Aguardando intervalo de {interval} min antes do próximo corte...')
            fim_espera = time.time() + wait_s
            while time.time() < fim_espera:
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
        api_ext._pub_log(f'Publicação global finalizada: {published} publicados, {failed} falhas.')

    api_ext._PUB_THREAD = threading.Thread(target=_loop_global, daemon=True)
    api_ext._PUB_THREAD.start()
    return {'ok': True, 'started': True, 'total': len(targets)}


@router.post('/publish/skip-wait')
def supo_publish_skip_wait() -> dict[str, Any]:
    """Pula o intervalo de espera e avança imediatamente para o próximo corte na fila."""
    import api_ext
    with api_ext._PUB_LOCK:
        api_ext._PUB['skip_wait'] = True
    return {'ok': True, 'skipped': True}


# ==============================================================================
# MONITORAMENTO E AUTOMAÇÃO DE CANAIS DO YOUTUBE
# ==============================================================================

@router.get('/channels')
def supo_list_channels() -> dict[str, Any]:
    """Retorna a lista de canais monitorados e as configurações globais."""
    import channel_monitor
    data = channel_monitor.load_channels_data()
    proj_meta = channel_monitor.load_project_channels()
    return {
        'ok': True,
        'channels': data.get('channels', []),
        'settings': data.get('settings', {}),
        'project_meta': proj_meta,
    }


@router.post('/channels')
def supo_add_channel(payload: dict[str, Any]) -> dict[str, Any]:
    """Cadastra um novo canal no monitoramento com uma conta do TikTok vinculada."""
    import channel_monitor
    import api_ext
    server_port = getattr(api_ext, 'SELF_PORT', 8585)
    try:
        res = channel_monitor.add_channel(payload, server_port=server_port)
        return {'ok': True, **res}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put('/channels/{channel_id}')
def supo_update_channel(channel_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Atualiza as configurações de um canal monitorado (conta, hashtags, auto_publish, etc.)."""
    import channel_monitor
    try:
        updated = channel_monitor.update_channel(channel_id, updates)
        return {'ok': True, 'channel': updated}
    except KeyError:
        raise HTTPException(status_code=404, detail='Canal não encontrado.')
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete('/channels/{channel_id}')
def supo_delete_channel(channel_id: str) -> dict[str, Any]:
    """Remove um canal do monitoramento."""
    import channel_monitor
    success = channel_monitor.delete_channel(channel_id)
    if not success:
        raise HTTPException(status_code=404, detail='Canal não encontrado.')
    return {'ok': True, 'deleted': True}


@router.post('/channels/{channel_id}/check')
def supo_check_single_channel(channel_id: str) -> dict[str, Any]:
    """Varre um canal específico imediatamente para buscar e importar vídeos novos."""
    import channel_monitor
    import api_ext
    server_port = getattr(api_ext, 'SELF_PORT', 8585)
    try:
        res = channel_monitor.check_channel_for_new(channel_id, server_port=server_port)
        return {'ok': True, **res}
    except KeyError:
        raise HTTPException(status_code=404, detail='Canal não encontrado.')
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post('/channels/check-now')
def supo_check_all_channels() -> dict[str, Any]:
    """Varre todos os canais ativos imediatamente."""
    import channel_monitor
    import api_ext
    server_port = getattr(api_ext, 'SELF_PORT', 8585)
    try:
        res = channel_monitor.check_all_channels(server_port=server_port)
        return {'ok': True, **res}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get('/projects/{project_id}/channel-meta')
def supo_get_project_channel_meta(project_id: str) -> dict[str, Any]:
    """Retorna informações do canal e conta TikTok vinculada a um projeto específico."""
    import channel_monitor
    meta = channel_monitor.get_project_channel_meta(project_id)
    return {'ok': True, 'meta': meta}


# --------------------------------------------------------------------------- #
# Integração: Downloads do TikTok (tiktok-downloader-web)
# --------------------------------------------------------------------------- #

@router.get('/tiktok-downloads/creators')
def supo_get_tiktok_creators() -> dict[str, Any]:
    """Retorna a lista de canais/criadores do TikTok com vídeos baixados."""
    try:
        import tiktok_library
        creators = tiktok_library.list_tiktok_creators()
        total_videos = sum(c.get('video_count', 0) for c in creators)
        return {'ok': True, 'creators': creators, 'total_videos': total_videos}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f'Erro ao listar criadores do TikTok: {exc}')


@router.post('/tiktok-downloads/channel-account')
def supo_save_channel_account(payload: dict[str, Any]) -> dict[str, Any]:
    """Vincula uma conta do TikTok padrão a um canal baixado."""
    folder = payload.get('folder')
    account_id = payload.get('account_id')
    if not folder or not account_id:
        raise HTTPException(status_code=400, detail='folder e account_id são obrigatórios.')
    try:
        import tiktok_library
        tiktok_library.save_channel_account(folder, account_id)
        return {'ok': True, 'folder': folder, 'account_id': account_id}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get('/tiktok-downloads/creators/{folder}/videos')
def supo_get_creator_videos(folder: str) -> dict[str, Any]:
    """Retorna a lista de vídeos baixados de um criador específico com descrição e tags."""
    try:
        import tiktok_library
        videos = tiktok_library.list_creator_videos(folder)
        return {'ok': True, 'folder': folder, 'videos': videos, 'total': len(videos)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f'Erro ao listar vídeos do criador: {exc}')


@router.post('/tiktok-downloads/generate-cover')
def supo_generate_cover(payload: dict[str, Any]) -> dict[str, Any]:
    """Extrai um frame do vídeo com FFmpeg para servir de capa personalizada."""
    folder = payload.get('folder')
    filename = payload.get('filename')
    second = float(payload.get('second', 1.5))

    if not folder or not filename:
        raise HTTPException(status_code=400, detail='folder e filename são obrigatórios.')

    try:
        import urllib.parse
        import tiktok_library
        video_path = tiktok_library.TIKTOK_DOWNLOADS_DIR / folder / filename
        cover = tiktok_library.extract_video_cover(video_path, second=second)
        if not cover:
            raise HTTPException(status_code=500, detail='Não foi possível gerar a capa do vídeo.')
        encoded_folder = urllib.parse.quote(folder)
        encoded_file = urllib.parse.quote(cover.name)
        cover_url = f'/media/tiktok-downloads/{encoded_folder}/{encoded_file}'
        return {'ok': True, 'cover_name': cover.name, 'cover_url': cover_url}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post('/tiktok-downloads/publish')
def supo_publish_single_tiktok_video(payload: dict[str, Any]) -> dict[str, Any]:
    """Publica um vídeo baixado específico diretamente no TikTok."""
    folder = payload.get('folder')
    filename = payload.get('filename')
    account_id = payload.get('account_id') or 'default'
    title = payload.get('title')
    description = payload.get('description')
    tags = payload.get('tags')

    if not folder or not filename:
        raise HTTPException(status_code=400, detail='folder e filename são obrigatórios.')

    try:
        import tiktok_library
        import api_ext

        # Atualiza o log do publicador para que apareça também na aba Publicador TikTok
        api_ext._pub_log(f'Iniciando envio avulso de "{title or filename}" para conta "{account_id}"...')

        res = tiktok_library.publish_tiktok_download_single(
            folder=folder,
            filename=filename,
            account_id=account_id,
            title=title,
            description=description,
            tags=tags,
            progress_callback=lambda msg: api_ext._pub_log(f'[{account_id}] {msg}'),
        )
        api_ext._pub_log(f'Envio concluído: status={res.get("status")}')
        return {'ok': True, **res}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post('/tiktok-downloads/publish-batch')
def supo_publish_batch_tiktok_videos(payload: dict[str, Any]) -> dict[str, Any]:
    """Inicia a publicação em lote de uma seleção de vídeos baixados do TikTok."""
    items = payload.get('items') or []
    account_id = payload.get('account_id') or 'default'
    interval_min = int(payload.get('interval_min') or 10)

    if not items:
        raise HTTPException(status_code=400, detail='Nenhum vídeo selecionado para publicação.')

    try:
        import api_ext
        import tiktok_library

        # Verifica se já há uma fila rodando
        with api_ext._PUB_LOCK:
            if api_ext._PUB.get('running') and api_ext._PUB_THREAD and api_ext._PUB_THREAD.is_alive():
                raise HTTPException(status_code=409, detail='Já existe uma publicação em andamento no Publicador TikTok.')

        tiktok_library.publish_tiktok_downloads_batch(
            items=items,
            account_id=account_id,
            interval_min=interval_min,
        )
        return {'ok': True, 'queued': len(items), 'account_id': account_id, 'interval_min': interval_min}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))




# --------------------------------------------------------------------------- #
# Publicador do YouTube (Shorts & Vídeos Normais)
# --------------------------------------------------------------------------- #

_YT_PUB_LOCK = threading.RLock()
_YT_PUB: dict[str, Any] = {
    'running': False,
    'stop': False,
    'skip_wait': False,
    'index': 0,
    'total': 0,
    'message': 'Nenhuma publicação do YouTube em andamento.',
    'log': [],
    'account': 'default',
    'current_title': '',
}
_YT_PUB_THREAD: threading.Thread | None = None


def _yt_pub_log(msg: str) -> None:
    stamp = time.strftime('%H:%M:%S')
    with _YT_PUB_LOCK:
        _YT_PUB['message'] = msg
        _YT_PUB['log'].append(f'{stamp}  {msg}')
        if len(_YT_PUB['log']) > 400:
            del _YT_PUB['log'][:100]
    logger.info('[YouTube Pub] %s', msg)


@router.get('/youtube-publisher/accounts')
def supo_get_youtube_accounts() -> dict[str, Any]:
    """Retorna a lista de contas/canais do YouTube cadastrados."""
    try:
        import youtube_local_publisher as ytl
        accs = ytl.list_accounts()
        return {'ok': True, 'accounts': accs}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post('/youtube-publisher/accounts')
def supo_add_youtube_account(payload: dict[str, Any]) -> dict[str, Any]:
    """Cadastra um novo canal/conta do YouTube."""
    name = (payload.get('name') or '').strip()
    if not name:
        raise HTTPException(status_code=400, detail='O nome do canal é obrigatório.')
    try:
        import youtube_local_publisher as ytl
        new_acc = ytl.add_account(name)
        return {'ok': True, 'account': new_acc}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete('/youtube-publisher/accounts/{account_id}')
def supo_remove_youtube_account(account_id: str) -> dict[str, Any]:
    """Remove uma conta do YouTube (exceto o Canal Principal)."""
    try:
        import youtube_local_publisher as ytl
        ok = ytl.remove_account(account_id)
        if not ok:
            raise HTTPException(status_code=404, detail='Canal não encontrado.')
        return {'ok': True, 'account_id': account_id}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post('/youtube-publisher/accounts/{account_id}/connect')
def supo_connect_youtube_account(account_id: str) -> dict[str, Any]:
    """Abre uma janela de navegador persistente para fazer/verificar login no YouTube Studio."""
    import youtube_local_publisher as ytl
    t = threading.Thread(
        target=ytl.login_session,
        kwargs={'account': account_id, 'progress': _yt_pub_log},
        daemon=True,
    )
    t.start()
    return {'ok': True, 'message': 'Navegador aberto para login no YouTube Studio.', 'account_id': account_id}


@router.get('/youtube-publisher/status')
def supo_get_youtube_publisher_status() -> dict[str, Any]:
    """Retorna o estado e logs da publicação do YouTube em tempo real."""
    with _YT_PUB_LOCK:
        return dict(_YT_PUB)


@router.post('/youtube-publisher/stop')
def supo_stop_youtube_publisher() -> dict[str, Any]:
    """Interrompe a publicação atual do YouTube."""
    with _YT_PUB_LOCK:
        _YT_PUB['stop'] = True
    _yt_pub_log('Solicitação de parada recebida pelo usuário.')
    return {'ok': True}


@router.post('/youtube-publisher/skip-wait')
def supo_skip_wait_youtube_publisher() -> dict[str, Any]:
    """Pula o intervalo de espera para o próximo vídeo do lote."""
    with _YT_PUB_LOCK:
        _YT_PUB['skip_wait'] = True
    _yt_pub_log('Intervalo pulado! Avançando imediatamente...')
    return {'ok': True}


@router.post('/youtube-publisher/extract-cover')
def supo_extract_youtube_cover(payload: dict[str, Any]) -> dict[str, Any]:
    """Extrai uma capa/thumbnail de alta qualidade de um vídeo local para o YouTube."""
    video_path_str = payload.get('video_path')
    second = float(payload.get('second', 1.5))
    if not video_path_str:
        raise HTTPException(status_code=400, detail='video_path é obrigatório.')

    v_path = Path(video_path_str)
    if not v_path.is_file():
        raise HTTPException(status_code=404, detail='Arquivo de vídeo não encontrado.')

    try:
        import tiktok_library
        cover_file = tiktok_library.extract_video_cover(v_path, second=second)
        if not cover_file:
            raise HTTPException(status_code=500, detail='Falha ao extrair capa com FFmpeg.')
        return {'ok': True, 'cover_path': str(cover_file), 'cover_name': cover_file.name}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post('/youtube-publisher/publish-single')
def supo_publish_single_youtube(payload: dict[str, Any]) -> dict[str, Any]:
    """Publica um único vídeo (Shorts ou Normal) no YouTube Studio."""
    global _YT_PUB_THREAD

    video_path_str = payload.get('video_path')
    project_id = payload.get('project_id')
    variant_id = payload.get('variant_id')

    title = payload.get('title') or ''
    description = payload.get('description') or ''
    tags = payload.get('tags') or ''
    thumbnail_path_str = payload.get('thumbnail_path')
    visibility = payload.get('visibility') or 'public'
    is_short = bool(payload.get('is_short', False))
    account = payload.get('account_id') or 'default'

    # Resolve automaticamente vídeos baixados do TikTok se vier folder/filename ou video_url
    folder = payload.get('folder')
    filename = payload.get('filename')
    if (not video_path_str or not Path(str(video_path_str)).is_file()):
        if folder and filename:
            try:
                import tiktok_library
                cand = tiktok_library.TIKTOK_DOWNLOADS_DIR / folder / filename
                if cand.is_file():
                    video_path_str = str(cand)
                if not thumbnail_path_str:
                    for ext in ('.jpg', '.jpeg', '.webp', '.png'):
                        c_cand = tiktok_library.TIKTOK_DOWNLOADS_DIR / folder / (Path(filename).stem.split('_')[0] + ext)
                        if c_cand.is_file():
                            thumbnail_path_str = str(c_cand)
                            break
            except Exception:
                pass
        elif payload.get('video_url'):
            try:
                import urllib.parse
                import tiktok_library
                url_p = payload['video_url'].split('/media/tiktok-downloads/')[-1]
                parts = [urllib.parse.unquote(p) for p in url_p.split('/') if p]
                if len(parts) >= 2:
                    cand = tiktok_library.TIKTOK_DOWNLOADS_DIR / parts[0] / parts[1]
                    if cand.is_file():
                        video_path_str = str(cand)
            except Exception:
                pass

    if (not video_path_str) and project_id and variant_id:
        import api_ext
        state = api_ext._read_studio(project_id)
        jobs = {j['job_id']: j for j in state.get('jobs', []) if j.get('job_id')}
        variant = next((v for v in state.get('output_variants', []) if v.get('id') == variant_id), {})
        job = jobs.get(variant.get('render_job_id'))
        v_path_resolved = api_ext._video_path(project_id, job)
        if v_path_resolved:
            video_path_str = str(v_path_resolved)
        if not title:
            title = variant.get('title') or 'Corte TikClip'
        if not description:
            description = variant.get('description') or ''
        if not tags:
            tags = variant.get('tags') or []
            if not tags:
                try:
                    ht_file = api_ext.projects_dir() / project_id / 'metadata' / 'hashtags-ext.json'
                    if ht_file.is_file():
                        import json as _json
                        ht_data = _json.loads(ht_file.read_text(encoding='utf-8'))
                        if isinstance(ht_data, list):
                            tags = ht_data
                        elif isinstance(ht_data, dict):
                            tags = ht_data.get('tags') or []
                except Exception:
                    pass
        if not thumbnail_path_str:
            c_path = api_ext._cover_path(project_id, job.get('job_id') if job else None, variant_id)
            if c_path and c_path.is_file():
                thumbnail_path_str = str(c_path)

    if not video_path_str or not Path(video_path_str).is_file():
        raise HTTPException(status_code=400, detail='Arquivo de vídeo não encontrado para publicação.')

    with _YT_PUB_LOCK:
        if _YT_PUB.get('running') and _YT_PUB_THREAD and _YT_PUB_THREAD.is_alive():
            raise HTTPException(status_code=409, detail='Já existe uma publicação em andamento no YouTube.')
        _YT_PUB.update(
            running=True,
            stop=False,
            skip_wait=False,
            index=1,
            total=1,
            message=f'Enviando "{title or Path(video_path_str).stem}" para o YouTube...',
            account=account,
            current_title=title,
        )

    def _worker():
        import youtube_local_publisher as ytl
        try:
            _yt_pub_log(f'Iniciando envio no YouTube ({"Shorts" if is_short else "Normal"}) para conta "{account}"')
            res = ytl.publish_video(
                video_path=video_path_str,
                title=title,
                description=description,
                tags=tags,
                thumbnail_path=thumbnail_path_str,
                visibility=visibility,
                is_short=is_short,
                account=account,
                progress=_yt_pub_log,
            )
            _yt_pub_log(f'Finalizado com sucesso: {res.get("video_url") or "Vídeo publicado!"}')
        except Exception as exc:
            _yt_pub_log(f'Erro na publicação do YouTube: {exc}')
        finally:
            with _YT_PUB_LOCK:
                _YT_PUB['running'] = False

    _YT_PUB_THREAD = threading.Thread(target=_worker, daemon=True)
    _YT_PUB_THREAD.start()

    return {'ok': True, 'message': 'Publicação iniciada no YouTube Studio.', 'account': account}


@router.post('/youtube-publisher/publish-batch')
def supo_publish_batch_youtube(payload: dict[str, Any]) -> dict[str, Any]:
    """Inicia a publicação em lote de vários Shorts ou vídeos no YouTube Studio."""
    global _YT_PUB_THREAD

    items = payload.get('items') or []
    account = payload.get('account_id') or 'default'
    interval_min = int(payload.get('interval_min') or 15)
    visibility = payload.get('visibility') or 'public'

    if not items:
        raise HTTPException(status_code=400, detail='Nenhum vídeo selecionado para a fila do YouTube.')

    with _YT_PUB_LOCK:
        if _YT_PUB.get('running') and _YT_PUB_THREAD and _YT_PUB_THREAD.is_alive():
            raise HTTPException(status_code=409, detail='Já existe uma publicação em andamento no YouTube.')
        _YT_PUB.update(
            running=True,
            stop=False,
            skip_wait=False,
            index=0,
            total=len(items),
            message=f'Fila do YouTube iniciada ({len(items)} vídeos)...',
            account=account,
            log=[],
        )

    def _batch_worker():
        import youtube_local_publisher as ytl
        total = len(items)
        _yt_pub_log(f'=== Iniciando fila em lote do YouTube: {total} vídeo(s) na conta "{account}" ===')
        _yt_pub_log(f'Intervalo configurado entre publicações: {interval_min} minuto(s)')

        for idx, it in enumerate(items, start=1):
            with _YT_PUB_LOCK:
                if _YT_PUB.get('stop'):
                    _yt_pub_log('Fila cancelada pelo usuário.')
                    break
                _YT_PUB['index'] = idx
                _YT_PUB['current_title'] = it.get('title') or 'Vídeo'
                _YT_PUB['skip_wait'] = False

            v_path = it.get('video_path')
            if (not v_path or not Path(str(v_path)).is_file()):
                # Tenta resolver por folder e filename
                fld = it.get('folder')
                fname = it.get('filename')
                if fld and fname:
                    try:
                        import tiktok_library
                        cand = tiktok_library.TIKTOK_DOWNLOADS_DIR / fld / fname
                        if cand.is_file():
                            v_path = str(cand)
                    except Exception:
                        pass
                # Tenta resolver por video_url
                if (not v_path or not Path(str(v_path)).is_file()) and it.get('video_url'):
                    try:
                        import urllib.parse
                        import tiktok_library
                        url_p = it['video_url'].split('/media/tiktok-downloads/')[-1]
                        parts = [urllib.parse.unquote(p) for p in url_p.split('/') if p]
                        if len(parts) >= 2:
                            cand = tiktok_library.TIKTOK_DOWNLOADS_DIR / parts[0] / parts[1]
                            if cand.is_file():
                                v_path = str(cand)
                    except Exception:
                        pass

            if not v_path or not Path(str(v_path)).is_file():
                _yt_pub_log(f'[{idx}/{total}] Vídeo não encontrado no caminho: {v_path}')
                continue

            title = it.get('title') or Path(v_path).stem
            is_short = bool(it.get('is_short', False))
            _yt_pub_log(f'[{idx}/{total}] Preparando "{title}" ({"Shorts" if is_short else "Normal"})...')

            try:
                res = ytl.publish_video(
                    video_path=v_path,
                    title=title,
                    description=it.get('description') or '',
                    tags=it.get('tags') or '',
                    thumbnail_path=it.get('thumbnail_path'),
                    visibility=visibility,
                    is_short=is_short,
                    account=account,
                    progress=lambda m: _yt_pub_log(f'[{idx}/{total}] {m}'),
                )
                _yt_pub_log(f'[{idx}/{total}] Publicado com sucesso no YouTube [OK] {res.get("video_url") or ""}')
            except Exception as exc:
                _yt_pub_log(f'[{idx}/{total}] Falha ao publicar: {exc}')

            if idx < total:
                _yt_pub_log(f'Aguardando intervalo de {interval_min} min antes do próximo vídeo...')
                segundos = interval_min * 60
                passo = 2
                while segundos > 0:
                    with _YT_PUB_LOCK:
                        if _YT_PUB.get('stop'):
                            break
                        if _YT_PUB.get('skip_wait'):
                            _yt_pub_log('Intervalo pulado! Iniciando próximo imediatamente...')
                            break
                    time.sleep(passo)
                    segundos -= passo

        with _YT_PUB_LOCK:
            _YT_PUB['running'] = False
        _yt_pub_log('=== Fila do YouTube finalizada ===')

    _YT_PUB_THREAD = threading.Thread(target=_batch_worker, daemon=True)
    _YT_PUB_THREAD.start()

    return {'ok': True, 'queued': len(items), 'account': account}


@router.get('/youtube-publisher/library-clips')
def supo_get_youtube_library_clips() -> dict[str, Any]:
    """Retorna todos os cortes prontos de todos os projetos da biblioteca para o Publicador do YouTube."""
    import api_ext
    root = api_ext.projects_dir()
    all_clips = []
    
    # Mapeia nomes reais dos projetos
    try:
        p_list = api_ext.list_projects()
        projects_map = {p['id']: p.get('name') or p.get('title') for p in p_list if p.get('id')}
    except Exception:
        projects_map = {}

    if root.is_dir():
        for entry in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not entry.is_dir():
                continue
            pid = entry.name
            try:
                state = api_ext._read_studio(pid)
                jobs = {j['job_id']: j for j in state.get('jobs', []) if j.get('job_id')}
                p_name = projects_map.get(pid) or state.get('project_name') or state.get('title') or (state.get('meta') or {}).get('title') or pid
                clips = api_ext.project_clips(pid)
                # Garante tags do projeto caso o corte não tenha herdado diretamente
                proj_ht = []
                try:
                    ht_file = entry / 'metadata' / 'hashtags-ext.json'
                    if ht_file.is_file():
                        import json as _json
                        ht_data = _json.loads(ht_file.read_text(encoding='utf-8'))
                        if isinstance(ht_data, list):
                            proj_ht = [str(t).strip().lstrip('#') for t in ht_data if str(t).strip()]
                        elif isinstance(ht_data, dict) and isinstance(ht_data.get('tags'), list):
                            proj_ht = [str(t).strip().lstrip('#') for t in ht_data['tags'] if str(t).strip()]
                except Exception:
                    pass

                for c in clips:
                    if c.get('status') == 'pronto' and c.get('has_video'):
                        variant_id = c['id']
                        variant = next((v for v in state.get('output_variants', []) if v.get('id') == variant_id), {})
                        job = jobs.get(variant.get('render_job_id'))
                        v_path = api_ext._video_path(pid, job)
                        c_path = api_ext._cover_path(pid, job.get('job_id') if job else None, variant_id)

                        # Combina as tags do corte com as tags do projeto
                        clip_tags = list(c.get('tags') or [])
                        for pt in proj_ht:
                            if pt not in clip_tags:
                                clip_tags.append(pt)

                        all_clips.append({
                            'project_id': pid,
                            'project_name': p_name,
                            'clip_id': variant_id,
                            'title': c.get('title') or 'Corte Viral',
                            'description': c.get('description') or '',
                            'tags': clip_tags,
                            'status': c.get('status'),
                            'duration': c.get('duration'),
                            'duration_label': c.get('duration_label') or '',
                            'video_url': c.get('video_url'),
                            'cover_url': c.get('cover_url'),
                            'video_path': str(v_path) if v_path else '',
                            'cover_path': str(c_path) if (c_path and c_path.is_file()) else '',
                        })
            except Exception:
                continue
    return {'ok': True, 'clips': all_clips, 'total': len(all_clips)}
