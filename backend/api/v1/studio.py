"""Unified creative workspace. Legacy projects are adapted, never overwritten."""

import logging

logger = logging.getLogger(__name__)
import json
import uuid
from pathlib import Path
from typing import Optional, Literal
from urllib.parse import urlparse
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, Query
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session
from backend.core.database import get_db
from backend.core.sentry_setup import capture_studio_exception
from backend.models.project import Project
from backend.models.clip import Clip
from backend.schemas.project import ProjectCreate, ProjectType
from backend.services.project_service import ProjectService
from backend.services.platform_strategy import list_platform_strategies
from backend.services.studio import store, jobs, intelligence
from backend.services.studio.models import Draft, CreateDraft, DuplicateDraft, ExportDraftRequest, RewriteRequest, Preferences, Language, Scene, ImportOptions, ConfirmPlan, AppendPlatformsRequest, PostCopy

router = APIRouter()


def project_or_404(project_id: str, db: Session):
    call(store.directory, project_id)
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(404, 'O projeto não existe')
    return project

def call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except store.ConflictError as error:
        raise HTTPException(409, str(error)) from None
    except FileNotFoundError as error:
        raise HTTPException(404, str(error)) from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None

def validate_draft(project_id, draft):
    duration = intelligence._probe(jobs.source(project_id)).get('duration', 0)
    intelligence.validate_scenes(draft.scenes, duration)

@router.get('/title-presets/{style}/thumbnail')
def title_preset_thumbnail(style: Literal['comic', 'neon', 'arena', 'editorial', 'pixel', 'frosted'], v: int = Query(1, ge=1, le=6)):
    from backend.services.studio.title_art import thumbnail
    return Response(call(thumbnail, style, v), media_type='image/png', headers={'Cache-Control':'public, max-age=86400'})

@router.get('/readiness')
def import_readiness():
    from backend.services.studio import readiness
    return readiness.report()


@router.get('/capabilities')
def capabilities():
    return {'visual_analysis': intelligence.ready(), 'visual_model': intelligence.visual_config()[2], 'languages': ['source', 'zh', 'en', 'ja']}


@router.get('/platform-strategies')
def platform_strategies():
    """Public output targets for the quick-generation entry point."""
    return {'strategies': list_platform_strategies()}


from backend.services.studio import vision_settings, analysis_preferences


@router.get('/analysis-preferences')
def get_analysis_preferences():
    return call(analysis_preferences.load)

@router.put('/analysis-preferences')
def save_analysis_preferences(body: analysis_preferences.AnalysisPreferences):
    return call(analysis_preferences.save, body)


@router.get('/vision-settings')
def get_vision_settings():
    return vision_settings.public()

@router.put('/vision-settings')
def save_vision_settings(body: vision_settings.VisionSettingsInput):
    return call(vision_settings.save, body)

@router.post('/vision-settings/test')
def test_vision_settings(body: vision_settings.VisionSettingsInput):
    try:
        return vision_settings.test(body)
    except Exception as error:
        capture_studio_exception(error, 'vision_test', analysis_mode='visual')
        raise HTTPException(502, 'O teste de conexão de visão falhou; verifique o endereço, a chave e se o modelo aceita imagem.') from None

@router.post('/import')
async def import_visual(
    goal: Literal['auto', 'content', 'highlight', 'promo'] = Form('auto'),
    language: Language = Form('source'),
    aspect: Optional[Literal['original', 'portrait', 'landscape']] = Form(None),
    duration: Optional[int] = Form(None, ge=10, le=120),
    instruction: str = Form('', max_length=1000),
    platforms: list[str] = Form(['douyin']),
    auto_start: bool = Form(False),
    portrait_style: Literal['auto', 'interview', 'podcast'] = Form('auto'),
    brand_outro_enabled: bool = Form(True),
    subtitle: Optional[UploadFile] = File(None),
    name: str = Form('Edição inteligente', max_length=200),
    url: Optional[str] = Form(None),
    browser: Optional[Literal['chrome', 'edge', 'firefox', 'safari']] = Form(None),
    video: Optional[UploadFile] = File(None),
    features: str = Form(''),
    db: Session = Depends(get_db),
):
    if bool(url) == bool(video):
        raise HTTPException(422, 'Informe um link de vídeo ou escolha um arquivo')
    if url:
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower()
        if parsed.scheme != 'https' or parsed.username or parsed.password or not any(host == d or host.endswith('.' + d) for d in ('youtube.com', 'youtu.be', 'bilibili.com', 'b23.tv')):
            raise HTTPException(422, 'Só aceito link HTTPS do YouTube')
    ext = Path(video.filename or '').suffix.lower() if video else '.mp4'
    if ext not in ('.mp4', '.mov', '.mkv', '.webm', '.avi'):
        raise HTTPException(422, 'Formato de vídeo não suportado. Use MP4, MOV, MKV, WEBM ou AVI.')
    first_chunk = await video.read(1024 * 1024) if video else None
    if video and not first_chunk:
        await video.close()
        raise HTTPException(422, 'O arquivo de vídeo está vazio; escolha outro')
    subtitle_bytes = None
    if subtitle:
        try:
            if Path(subtitle.filename or '').suffix.lower() != '.srt':
                raise HTTPException(422, 'A legenda só pode ser em SRT')
            subtitle_bytes = await subtitle.read(2 * 1024 * 1024 + 1)
            if not subtitle_bytes or len(subtitle_bytes) > 2 * 1024 * 1024:
                raise HTTPException(422, 'A legenda está vazia ou passa de 2 MB')
        finally:
            await subtitle.close()
    prefs = ImportOptions(
        goal=goal, language=language, aspect=aspect, duration=duration, instruction=instruction,
        platforms=platforms, auto_start=auto_start, portrait_style=portrait_style, branding={'outro_enabled': brand_outro_enabled},
    )
    project = ProjectService(db).create_project(ProjectCreate(name=name.strip() or 'Edição inteligente', project_type=ProjectType.DEFAULT, source_url=url, settings={'creative': {'goal': goal}, 'smart_import': prefs.model_dump(), 'import_staging': not prefs.auto_start, 'creative_browser': browser, 'platforms': prefs.platforms, 'brand_outro_enabled': prefs.branding.outro_enabled}))
    pid = str(project.id)
    raw = store.directory(pid) / 'raw'
    raw.mkdir(parents=True, exist_ok=True)
    try:
        if subtitle_bytes is not None:
            (raw / 'input.srt').write_bytes(subtitle_bytes)
        if video:
            path = raw / ('input' + ext)
            with path.open('wb') as target:
                target.write(first_chunk)
                while chunk := await video.read(1024 * 1024):
                    target.write(chunk)
            project.video_path = str(path)
            db.commit()
        snapshot = None
        if features and len(features) <= 4000:
            try:
                parsed = json.loads(features)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                snapshot = parsed
        run_id = call(jobs.inspect_project, pid, prefs, url, browser, features=snapshot)
    except Exception:
        project.status = 'failed'
        db.commit()
        raise
    finally:
        if video:
            await video.close()
    return {'project_id': pid, 'analysis_run_id': run_id}

@router.get('/{project_id}')
def workspace(project_id: str, db: Session = Depends(get_db)):
    project = project_or_404(project_id, db)
    data = call(store.read, project_id)
    try:
        from backend.services.upload_post_publisher import list_records
        records = list_records(project_id)
        published = {
            r.get('output_variant_id') or r.get('clip_id'): r
            for r in records if r.get('status') == 'completed'
        }
        for v in data.get('output_variants', []):
            vid = v.get('id')
            job_id = v.get('render_job_id')
            rec = published.get(vid) or (published.get(f'studio-{job_id}') if job_id else None)
            if rec and v.get('post') and v['post'].get('title'):
                title = v['post']['title']
                if not title.startswith('[PUBLICADO'):
                    v['post']['title'] = f"[PUBLICADO] {title}"
    except Exception:
        logger.debug("excecao ignorada", exc_info=True)
    return {**data, 'material_origin': 'sample' if (project.processing_config or {}).get('example') else 'user', 'example_version': (project.processing_config or {}).get('example_version'), 'jobs': [{k: v for k, v in j.items() if k not in ('instance', 'snapshot')} for j in data['jobs']]}

@router.get('/{project_id}/source-preview')
def source_preview_status(project_id: str, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    from backend.services.studio import preview
    return call(preview.status, project_id)

@router.post('/{project_id}/source-preview')
def prepare_source_preview(project_id: str, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    from backend.services.studio import preview
    return call(preview.start, project_id)

@router.get('/{project_id}/source-preview/video')
def source_preview_video(project_id: str, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    from backend.services.studio import preview
    return FileResponse(call(preview.ready_file, project_id), media_type='video/mp4')

@router.get('/{project_id}/source')
def source_video(project_id: str, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    path = call(jobs.source, project_id)
    return FileResponse(path)

@router.get('/{project_id}/drafts/{draft_id}/thumbnail')
def draft_thumbnail(project_id: str, draft_id: str, revision: int = Query(..., ge=1),
                    job_id: Optional[str] = Query(None, pattern=r'^[a-f0-9]{32}$'), db: Session = Depends(get_db)):
    from backend.services.studio.thumbnails import draft_frame
    project_or_404(project_id, db)
    data = call(draft_frame, project_id, draft_id, revision, job_id)
    # Server memory cache is keyed by actual source stats. Revalidate on navigation.
    return Response(data, media_type='image/jpeg', headers={'Cache-Control': 'private, no-cache'})

@router.post('/{project_id}/title-preview')
def title_preview(project_id: str, body: Draft, db: Session = Depends(get_db), layer: Literal['artwork', 'backdrop'] = 'artwork'):
    from backend.services.studio import title_art
    project_or_404(project_id, db)
    info = call(intelligence._probe, call(jobs.source, project_id))
    w,h = {'portrait':(1080,1920),'landscape':(1920,1080)}.get(body.aspect, (info.get('width'),info.get('height')))
    if not w or not h:
        raise HTTPException(422,'Não foi possível ler o tamanho do vídeo original')
    # Use the same output resolution/font metrics as export; no LLM call or draft mutation.
    w,h = int(w)//2*2, int(h)//2*2
    if layer == 'backdrop':
        from backend.services.studio.title_materials import backdrop_png
        data = call(backdrop_png, body.hook, body.title_style, w, h, **title_art.options_for(body))
    else:
        data = call(title_art.png_bytes, body.hook, body.title_style, w, h, **title_art.options_for(body))
    return Response(data, media_type='image/png', headers={'Cache-Control':'no-store'})

@router.get('/{project_id}/candidates')
def candidates(project_id: str, db: Session = Depends(get_db)):
    """Only offer ranges from this project's actual source video."""
    project_or_404(project_id, db)
    info = call(intelligence._probe, call(jobs.source, project_id))
    duration = info.get('duration', 0)
    rows = []
    skipped = 0
    raw_events = call(store.read, project_id)['events']
    raw_clips = db.query(Clip).filter(Clip.project_id == project_id).order_by(Clip.start_time).all()
    sources = [('visual', e) for e in raw_events]
    sources.extend(('legacy', {'id': str(c.id), 'label': c.title[:120], 'start': c.start_time,
                              'end': c.end_time, 'evidence': (c.recommendation_reason or '')[:1000]}) for c in raw_clips)
    for kind, raw in sources:
        try:
            scene = Scene.model_validate(raw)
            intelligence.validate_scenes([scene], duration)
        except ValueError:
            skipped += 1
            continue
        rows.append({**scene.model_dump(), 'id': f'{kind}-{scene.id}', 'kind': kind})
    return {'duration': duration, 'candidates': rows,
            'warnings': [f'{skipped} 个片段时间无效，已从候选中排除'] if skipped else []}

@router.post('/{project_id}/analyze')
def analyze_again(project_id: str, db: Session = Depends(get_db)):
    project = project_or_404(project_id, db)
    config = project.processing_config or {}
    # Legacy visual projects must enter the same screening/confirmation boundary.
    # A stored creative goal describes output, not permission for a new paid scan.
    prefs = ImportOptions.model_validate(config['smart_import']) if 'smart_import' in config else ImportOptions.model_validate(Preferences.model_validate(config.get('creative', {})).model_dump())
    url = None
    try:
        jobs.source(project_id)
    except FileNotFoundError:
        url = (project.project_metadata or {}).get('source_url')
        if not url:
            raise HTTPException(404, 'O material original não existe mais; importe de novo')
    run_id = call(jobs.inspect_project, project_id, prefs, url, (project.processing_config or {}).get('creative_browser'))
    return {'ok': True, 'analysis_run_id': run_id}

@router.put('/{project_id}/plan')
def correct_plan(project_id: str, body: ImportOptions, db: Session = Depends(get_db)):
    project = project_or_404(project_id, db)
    # Keep the check, requested preferences and job reservation atomic with other submissions.
    with store.lock:
        if (store.read(project_id).get('analysis') or {}).get('status') == 'running':
            raise HTTPException(409, 'A análise ainda não terminou; o plano pode ser alterado depois. Os rascunhos existentes serão mantidos.')
        url = None
        try:
            jobs.source(project_id)
        except FileNotFoundError:
            url = (project.project_metadata or {}).get('source_url')
            if not url:
                raise HTTPException(404, 'O material original não existe mais; importe de novo')
        previous_config = dict(project.processing_config or {})
        project.processing_config = {**previous_config, 'smart_import': body.model_dump()}
        db.commit()
        try:
            run_id = call(jobs.inspect_project, project_id, body, url, previous_config.get('creative_browser'))
        except Exception:
            # A rejected submission must not persist preferences for a plan
            # that was never produced. inspect_project restores the JSON state.
            project.processing_config = previous_config
            db.commit()
            raise
    return {'ok': True, 'analysis_run_id': run_id}

@router.post('/{project_id}/start')
def confirm_and_start(project_id: str, body: ConfirmPlan, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    run_id = call(jobs.confirm_project, project_id, body)
    return {'ok': True, 'analysis_run_id': run_id}

@router.post('/{project_id}/platforms')
def append_platforms(project_id: str, body: AppendPlatformsRequest, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    variants = call(jobs.append_platform_variants, project_id, body.platforms, body.branding.model_dump())
    return {'variants': variants}


def _variant_job(project_id: str, variant_id: str):
    state = call(store.read, project_id)
    variant = next((item for item in state.get('output_variants', []) if item['id'] == variant_id), None)
    if not variant:
        raise HTTPException(404, 'A versão da peça final não existe')
    job = next((j for j in state['jobs'] if j['job_id'] == variant.get('render_job_id')), None)
    if not job or job['status'] != 'completed':
        raise HTTPException(409, 'A peça final ainda não foi gerada')
    return variant, job


@router.get('/{project_id}/output-variants/{variant_id}/cover')
def output_variant_cover(project_id: str, variant_id: str, db: Session = Depends(get_db)):
    from backend.services.studio import publish_kit
    project_or_404(project_id, db)
    variant, job = _variant_job(project_id, variant_id)
    path, _ = publish_kit.cover_file(project_id, job['job_id'], variant['strategy_id'])
    if path is None:
        raise HTTPException(404, 'A capa ainda não foi gerada')
    return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})


@router.post('/{project_id}/output-variants/{variant_id}/cover/ai')
def redesign_output_variant_cover(project_id: str, variant_id: str, db: Session = Depends(get_db)):
    from backend.services import cover
    project_or_404(project_id, db)
    _variant_job(project_id, variant_id)
    cfg = cover.load_config()
    if not (cfg.enabled and cfg.configured):
        raise HTTPException(409, 'Ative a capa com IA nos ajustes e escolha um modelo de imagem')
    if not cfg.allow_send_frame:
        raise HTTPException(409, 'Autorize o envio de imagens de referência nos ajustes')
    job = call(jobs.request_ai_cover, project_id, variant_id)
    if job is None:
        raise HTTPException(409, 'Revise as configurações de capa com IA')
    return job


@router.get('/{project_id}/output-variants/{variant_id}/cover/ai')
def output_variant_cover_job(project_id: str, variant_id: str, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    _variant_job(project_id, variant_id)
    return call(jobs.ai_cover_status, project_id, variant_id)


@router.put('/{project_id}/output-variants/{variant_id}/post')
def update_output_variant_post(project_id: str, variant_id: str, body: PostCopy, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    return call(jobs.update_post, project_id, variant_id, body)


def _kit_parts(project_id: str, variant: dict, job: dict):
    from backend.services.studio import publish_kit
    video = store.directory(project_id) / 'output' / 'studio' / f"{job['job_id']}.mp4"
    if not video.is_file():
        raise HTTPException(404, 'Arquivo do vídeo não encontrado')
    cover, _ = publish_kit.cover_file(project_id, job['job_id'], variant['strategy_id'])
    post = dict(variant.get('post') or {})
    # Sem título, o kit seria nomeado com o id do job (hash) — inútil para o
    # usuário. Segue a mesma ordem de fallback que a interface usa.
    if not (post.get('title') or '').strip():
        titulo = (job.get('result') or {}).get('title') or ''
        if not titulo:
            try:
                estado = call(store.read, project_id)
                rascunhos = {d.get('id'): d for d in estado.get('drafts', [])}
                titulo = (rascunhos.get(variant.get('draft_id')) or {}).get('title') or ''
            except Exception:  # noqa: BLE001
                titulo = ''
        if not titulo:
            titulo = 'Corte'
        post['title'] = titulo
    return video, cover, post


def _completed_variants(project_id: str):
    state = call(store.read, project_id)
    jobs_by_id = {j['job_id']: j for j in state.get('jobs', [])}
    for variant in state.get('output_variants', []):
        job = jobs_by_id.get(variant.get('render_job_id'))
        if job and job.get('status') == 'completed':
            yield variant, job


@router.get('/{project_id}/output-variants/{variant_id}/kit')
def output_variant_kit(project_id: str, variant_id: str, db: Session = Depends(get_db)):
    """Exporta o kit para Downloads\\TikClip\\<data e hora>\\<título>\\ (sem zip) e abre a pasta."""
    from backend.services.studio import publish_kit
    project_or_404(project_id, db)
    variant, job = _variant_job(project_id, variant_id)
    video, cover, post = _kit_parts(project_id, variant, job)
    folder = call(publish_kit.export_kit_folder, video, cover, post, publish_kit.new_export_folder())
    publish_kit.open_in_explorer(folder)

    # O frontend espera um arquivo baixável (200 OK com Content-Disposition).
    # Retornamos um pacote leve contendo a legenda/post em texto, satisfazendo o download do app sem erros.
    safe_title = publish_kit._safe_name(post.get('title') or video.stem)
    filename = f"{safe_title}.txt"
    content = publish_kit.kit_text(post).encode('utf-8')
    from urllib.parse import quote
    return Response(
        content=content,
        media_type='text/plain; charset=utf-8',
        headers={
            'Content-Disposition': f"attachment; filename*=UTF-8''{quote(filename)}",
            'X-TikClip-Kit-Folder': quote_header(str(folder)),
        }
    )


# Duas rotas explícitas em vez de api_route(methods=['GET','POST']): com
# api_route o FastAPI gerava o mesmo operation_id para GET e POST, o que
# quebra geradores de client e gera aviso no /docs.
@router.get('/{project_id}/output-variants/kits', operation_id='output_variant_kits_all_get')
@router.post('/{project_id}/output-variants/kits', operation_id='output_variant_kits_all_post')
def output_variant_kits_all(project_id: str, db: Session = Depends(get_db)):
    """Exporta todos os cortes prontos do projeto numa só pasta Downloads\\TikClip\\<data e hora>\\."""
    from backend.services.studio import publish_kit
    project_or_404(project_id, db)
    base = publish_kit.new_export_folder()
    exported, skipped = [], 0
    for index, (variant, job) in enumerate(_completed_variants(project_id), start=1):
        try:
            video, cover, post = _kit_parts(project_id, variant, job)
            exported.append(str(call(publish_kit.export_kit_folder, video, cover, post, base, index)))
        except HTTPException:
            skipped += 1
    if not exported:
        raise HTTPException(404, 'Nenhum corte pronto para exportar')
    publish_kit.open_in_explorer(base)
    return {'folder': str(base), 'exported': len(exported), 'skipped': skipped, 'items': exported}


def quote_header(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe=':\\/ ')


@router.post('/{project_id}/output-variants/{variant_id}/produce')
def produce_output_variant(project_id: str, variant_id: str, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    return call(jobs.produce_variant, project_id, variant_id)


@router.post('/{project_id}/output-variants/{variant_id}/retry')
def retry_output_variant(project_id: str, variant_id: str, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    return call(jobs.retry_variant, project_id, variant_id)


@router.post('/{project_id}/drafts')
def create_draft(project_id: str, body: CreateDraft, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    clips = db.query(Clip).filter(Clip.project_id == project_id, Clip.id.in_(body.clip_ids)).all()
    by_id = {str(c.id): c for c in clips}
    if any(cid not in by_id for cid in body.clip_ids):
        raise HTTPException(404, 'O trecho escolhido não pertence a este projeto')
    scenes = [Scene(id=uuid.uuid4().hex, label=by_id[cid].title, start=by_id[cid].start_time, end=by_id[cid].end_time) for cid in body.clip_ids]
    project = project_or_404(project_id, db)
    prefs = (project.processing_config or {}).get('creative', {})
    draft = Draft(id=uuid.uuid4().hex, title=body.title, scenes=scenes, origin='legacy', language=prefs.get('language', 'source'), aspect=prefs.get('aspect') or 'original')
    call(validate_draft, project_id, draft)
    store.directory(project_id).mkdir(parents=True, exist_ok=True)
    import hashlib
    import json
    source_key = hashlib.sha256(json.dumps(body.clip_ids).encode()).hexdigest()
    return call(store.open_legacy_editor, project_id, draft, source_key, reuse_existing=body.reuse_existing)

@router.post('/{project_id}/events/{event_id}/draft')
def event_draft(project_id: str, event_id: str, db: Session = Depends(get_db)):
    project = project_or_404(project_id, db)
    prefs = Preferences.model_validate((project.processing_config or {}).get('creative', {}))
    event = next((e for e in store.read(project_id)['events'] if e['id'] == event_id), None)
    if not event:
        raise HTTPException(404, 'O trecho não existe')
    draft = Draft(id=uuid.uuid4().hex, title=event['label'], scenes=[Scene.model_validate(event)], aspect=prefs.aspect, layout='crop' if prefs.aspect == 'portrait' else 'fit', language=prefs.language, subtitles=False, origin='visual-highlight')
    call(validate_draft, project_id, draft)
    return call(store.save_draft, project_id, draft, create=True)

@router.put('/{project_id}/drafts/{draft_id}')
def save(project_id: str, draft_id: str, body: Draft, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    if draft_id != body.id:
        raise HTTPException(422, 'O ID do rascunho não confere')
    call(validate_draft, project_id, body)
    return call(store.save_draft, project_id, body)

@router.post('/{project_id}/drafts/{draft_id}/duplicate')
def duplicate(project_id: str, draft_id: str, body: DuplicateDraft, db: Session = Depends(get_db)):
    project = project_or_404(project_id, db)
    if draft_id != body.draft.id:
        raise HTTPException(422, 'O ID do rascunho de origem não confere')
    call(validate_draft, project_id, body.draft)
    result = call(store.duplicate_draft, project_id, body)
    project.processing_config = {**(project.processing_config or {}), 'studio_draft_count': len(store.read(project_id)['drafts'])}
    db.commit()
    return result

@router.post('/{project_id}/rewrite')
def rewrite(project_id: str, body: RewriteRequest, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    if not any(d['id'] == body.draft.id for d in store.read(project_id)['drafts']):
        raise HTTPException(404, 'O rascunho não existe')
    try:
        result = intelligence.text_json('Você é editor de texto de corte. Otimize title e hook conforme o pedido do usuário, sem alterar fatos; use apenas o texto original e a evidência das cenas. Não afirme que alterou cena, som ou vídeo, nem prometa desempenho de anúncio. Retorne {"title":"...","hook":"..."}.', {'instruction': body.instruction, 'language': body.draft.language, 'title': body.draft.title, 'hook': body.draft.hook, 'scenes': [s.model_dump() for s in body.draft.scenes]})
        candidate = Draft.model_validate({**body.draft.model_dump(), 'title': result['title'], 'hook': result['hook']})
    except Exception as error:
        capture_studio_exception(error, 'rewrite')
        raise HTTPException(502, 'Falha ao gerar o texto; verifique as configurações de modelo e tente de novo. O rascunho não foi alterado.') from None
    return candidate

@router.get('/{project_id}/subtitles')
def subtitles(project_id: str, db: Session = Depends(get_db)):
    """Subtitle cues in seconds, for the editor's styled preview overlay."""
    from backend.pipeline.quality import to_seconds
    from backend.services.publish_export import _load_srt_entries
    project_or_404(project_id, db)
    entries = _load_srt_entries(project_id)
    return {'cues': [{'start': to_seconds(e['start_time']), 'end': to_seconds(e['end_time']), 'text': e.get('text', '')} for e in entries]}

@router.get('/framing/status')
def framing_status():
    from backend.services.studio import framing
    return framing.get_status()

@router.post('/framing/install')
def framing_install():
    from backend.services.studio import framing
    return {**framing.start_install(), **framing.get_status()}

@router.post('/{project_id}/auto-frame')
def auto_frame(project_id: str, body: Draft, db: Session = Depends(get_db)):
    """Centre each scene's crop window on the speaker. Pure analysis: nothing is saved."""
    from backend.services.publish_export import _probe
    from backend.services.studio import framing
    project_or_404(project_id, db)
    if not framing.is_installed():
        raise HTTPException(409, 'O componente de reconhecimento de pessoas não está instalado')
    video = call(jobs.source, project_id)
    info = _probe(video)
    if not info.get('width') or not info.get('height'):
        raise HTTPException(422, 'Não foi possível ler o tamanho do vídeo original')
    try:
        return framing.auto_frame(video, body, int(info['width']), int(info['height']))
    except Exception as error:
        capture_studio_exception(error, 'auto_frame')
        raise HTTPException(502, 'O enquadramento automático falhou; ajuste a posição manualmente') from None

@router.post('/{project_id}/drafts/{draft_id}/export')
def export(project_id: str, draft_id: str, body: ExportDraftRequest | None = None, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    state = store.read(project_id)
    raw = next((d for d in state['drafts'] if d['id'] == draft_id), None)
    if not raw:
        raise HTTPException(404, 'O rascunho não existe')
    if body and body.revision != raw['revision']:
        raise HTTPException(409, 'O rascunho foi atualizado em outra janela; recarregue antes de exportar')
    draft = Draft.model_validate(raw)
    call(validate_draft, project_id, draft)
    variant = next((v for v in state.get('output_variants', []) if v['draft_id'] == draft_id), None)
    if variant:
        return call(jobs.export, project_id, draft, brand_outro=bool(variant.get('branding', {}).get('outro_enabled', True)))
    return call(jobs.export, project_id, draft)

@router.get('/{project_id}/exports/{job_id}/video')
def rendered_video(project_id: str, job_id: str, download: bool = False, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    job = next((j for j in store.read(project_id)['jobs'] if j['job_id'] == job_id), None)
    if not job:
        raise HTTPException(404, 'A exportação não existe')
    if job['status'] != 'completed':
        raise HTTPException(409, 'A exportação ainda não terminou')
    path = store.directory(project_id) / 'output' / 'studio' / f'{job_id}.mp4'
    if not path.is_file():
        raise HTTPException(404, 'O arquivo exportado foi removido')
    return FileResponse(path, media_type='video/mp4', filename=path.name if download else None)
