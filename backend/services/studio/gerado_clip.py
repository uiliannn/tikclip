"""Coloca um vídeo gerado como corte de projeto (aba Projetos), sem passar pelo render.

O app monta a lista de cortes assim: cada `output_variant` com `status=completed`,
cujo `render_job_id` exista em `jobs`, e cujo arquivo
`projects/<pid>/output/studio/<job_id>.mp4` exista, vira um corte "pronto".
Então basta gravar esse trio (draft + job + variante) e copiar o mp4 no lugar certo:
nada é re-renderizado — o vídeo gerado já é o resultado final.
"""
from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

NOME_PROJETO = 'Gerados com IA'


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sessao():
    from backend.core.database import SessionLocal
    return SessionLocal()


def projeto_destino() -> str:
    """Projeto único que recebe os gerados; cria na primeira vez."""
    from backend.models.project import Project
    from backend.schemas.project import ProjectCreate, ProjectType
    from backend.services.project_service import ProjectService

    db = _sessao()
    try:
        existente = db.query(Project).filter(Project.name == NOME_PROJETO).first()
        if existente is not None:
            return str(existente.id)
        servico = ProjectService(db)
        projeto = servico.create_project(
            ProjectCreate(name=NOME_PROJETO, project_type=ProjectType.DEFAULT, settings={}))
        return str(projeto.id)
    finally:
        db.close()


def montar_tripla(titulo: str, duracao: float, hashtags: list[str],
                  ids: tuple[str, str, str] | None = None,
                  quando: str | None = None) -> tuple[dict, dict, dict]:
    """Monta draft + job + variante no formato que a lista de cortes exige.

    Regra do app: variante com status 'completed' cujo render_job_id existe em
    jobs, e cujo arquivo output/studio/<job_id>.mp4 existe, vira um corte "pronto".
    """
    draft_id, job_id, var_id = ids or (uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex)
    agora = quando or _agora()
    titulo = str(titulo)[:120]
    draft = {
        'id': draft_id, 'title': titulo, 'hook': '', 'scenes': [],
        'language': 'pt', 'aspect': 'portrait', 'layout': 'fit', 'crop_x': 0.5,
        'title_style': 'impact', 'title_template_version': 1, 'title_motion': 'none',
        'title_scale': 1.0, 'title_y': 0.5, 'title_accent': '',
        'subtitles': False, 'subtitle_style': 'clean', 'original_audio': True,
        'revision': 1, 'updated_at': agora, 'origin': 'gerado',
    }
    job = {
        'job_id': job_id, 'status': 'completed', 'percent': 100,
        'draft_id': draft_id, 'title': titulo, 'revision': 1, 'brand_outro': False,
        'created_at': agora, 'instance': uuid.uuid4().hex[:32], 'snapshot': draft,
        'variant_links': [[var_id, draft_id, 1, uuid.uuid4().hex]],
        'result': {'title': titulo, 'duration': duracao, 'width': 1080, 'height': 1920},
        'cover_pending': False, 'duration_ms': 0,
    }
    variante = {
        'id': var_id, 'draft_id': draft_id, 'draft_revision': 1,
        'strategy_id': 'tiktok', 'strategy_version': 1,
        'branding': {'outro_enabled': False, 'outro_version': 'v1'},
        'status': 'completed', 'created_at': agora, 'framing': 'speaker',
        'post': {'title': titulo, 'description': '', 'tags': hashtags},
        'render_attempt_id': uuid.uuid4().hex, 'render_job_id': job_id,
        'cover': 'frame', 'generated': True,
    }
    return draft, job, variante


def adicionar(ident: str, pasta_gerados: Path, meta: dict) -> dict:
    """Copia o vídeo (e a capa) para o projeto e registra o corte."""
    from backend.services.studio import store

    origem_video = Path(pasta_gerados) / ident / 'video.mp4'
    if not origem_video.is_file():
        raise FileNotFoundError('Vídeo gerado não encontrado')

    pid = projeto_destino()
    job_id = uuid.uuid4().hex
    draft_id = uuid.uuid4().hex
    var_id = uuid.uuid4().hex
    agora = _agora()
    titulo = str(meta.get('titulo') or ident)[:120]
    duracao = float(meta.get('duracao') or 0)
    hashtags = [str(t) for t in (meta.get('hashtags') or []) if t]

    pasta = store.directory(pid)
    destino_video = pasta / 'output' / 'studio' / f'{job_id}.mp4'
    destino_video.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(origem_video, destino_video)

    origem_capa = Path(pasta_gerados) / ident / 'capa.jpg'
    if origem_capa.is_file():
        # nomes que o app procura: douyin.jpg (publicação) e kit-tiktok.jpg (kit),
        # além de frame-tiktok.jpg usado pela prévia da capa
        pasta_capas = pasta / 'output' / 'covers' / f'studio-{job_id}'
        pasta_capas.mkdir(parents=True, exist_ok=True)
        for nome in ('douyin.jpg', 'kit-tiktok.jpg', 'frame-tiktok.jpg'):
            shutil.copy2(origem_capa, pasta_capas / nome)

    draft, job, variante = montar_tripla(
        titulo, duracao, hashtags, (draft_id, job_id, var_id), agora)

    def mutar(dados):
        dados.setdefault('drafts', []).append(draft)
        dados.setdefault('jobs', []).append(job)
        dados.setdefault('output_variants', []).append(variante)
        # a grade usa source_meta.title quando existe; sem isso o cartão mostraria
        # o título do primeiro corte em vez do nome do projeto-fila
        meta_projeto = dados.setdefault('source_meta', {})
        meta_projeto.setdefault('title', NOME_PROJETO)

    store.change(pid, mutar)
    return {'projeto': pid, 'variante': var_id, 'job': job_id,
            'titulo': titulo, 'duracao': duracao, 'hashtags': hashtags}


def ja_estava(pid: str, titulo: str) -> bool:
    """Evita duplicar se o mesmo título já foi enviado para o projeto."""
    from backend.services.studio import store

    try:
        dados = store.read(pid)
    except Exception:  # noqa: BLE001
        return False
    for variante in dados.get('output_variants') or []:
        if (variante.get('post') or {}).get('title') == titulo:
            return True
    return False


if __name__ == '__main__':
    print('projeto dos gerados:', projeto_destino())
