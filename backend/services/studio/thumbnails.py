"""Small, bounded, project-scoped still previews; never invoke a model or mutate a draft."""
from functools import lru_cache
from pathlib import Path
import json
import math
import subprocess
import threading

from backend.services.studio import store, jobs, intelligence
from backend.services.studio.models import Draft
from backend.utils.ffmpeg_utils import get_ffmpeg_path, get_ffprobe_path

_slots = threading.BoundedSemaphore(2)


@lru_cache(maxsize=128)
def _frame(path: str, modified_ns: int, size: int, timestamp: float) -> bytes:
    # Stat fields invalidate cached frames if a source is replaced in place.
    if not _slots.acquire(timeout=10):
        raise ValueError('A geração de miniaturas está ocupada; tente de novo em instantes')
    try:
        result = subprocess.run([
            get_ffmpeg_path(), '-v', 'error', '-nostdin', '-ss', str(timestamp),
            '-i', path, '-frames:v', '1', '-an', '-sn',
            '-vf', 'scale=640:640:force_original_aspect_ratio=decrease',
            '-threads', '1', '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1',
        ], capture_output=True, check=True, timeout=15)
        if not result.stdout.startswith(b'\xff\xd8'):
            raise ValueError('Não foi possível gerar a miniatura; abra o editor para ver o vídeo original')
        return result.stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise ValueError('Não foi possível gerar a miniatura; abra o editor para ver o vídeo original') from None
    finally:
        _slots.release()


@lru_cache(maxsize=128)
def _duration(path: str, modified_ns: int, size: int) -> float:
    if not _slots.acquire(timeout=10):
        raise ValueError('A geração de miniaturas está ocupada; tente de novo em instantes')
    try:
        result = subprocess.run([get_ffprobe_path(), '-v', 'error', '-show_entries',
                                 'format=duration', '-of', 'json', path],
                                capture_output=True, check=True, timeout=10)
        duration = float(json.loads(result.stdout)['format']['duration'])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError
        return duration
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        raise ValueError('Não foi possível ler a imagem do vídeo') from None
    finally:
        _slots.release()


def draft_frame(project_id: str, draft_id: str, revision: int, job_id: str | None = None) -> bytes:
    state = store.read(project_id)
    raw = next((d for d in state['drafts'] if d['id'] == draft_id), None)
    if raw is None:
        raise FileNotFoundError('O rascunho não existe')
    draft = Draft.model_validate(raw)
    if draft.revision != revision:
        raise store.ConflictError('A versão do rascunho mudou; atualize a miniatura')
    if job_id:
        job = next((j for j in state['jobs'] if j['job_id'] == job_id and j['draft_id'] == draft_id
                    and j['revision'] == revision and j['status'] == 'completed'), None)
        if job is None:
            raise FileNotFoundError('Esta versão não tem essa peça exportada')
        # Resolve the expected project-owned output, never a saved result.path.
        import re
        if not re.fullmatch(r'[a-f0-9]{32}', job_id):
            raise ValueError('ID de exportação inválido')
        video = store.directory(project_id) / 'output' / 'studio' / f'{job_id}.mp4'

    else:
        video = jobs.source(project_id)

    video = Path(video)
    stat = video.stat()
    if not stat.st_size:
        raise ValueError('Não foi possível ler a imagem do vídeo')
    duration = _duration(str(video.resolve()), stat.st_mtime_ns, stat.st_size)
    if job_id:
        timestamp = min(.6, duration / 2)
    else:
        intelligence.validate_scenes(draft.scenes, duration)
        scene = draft.scenes[0]
        # Sample inside the scene, avoiding transition flashes at the cut boundary.
        timestamp = scene.start + min(3, (scene.end - scene.start) * .2)
    return _frame(str(video.resolve()), stat.st_mtime_ns, stat.st_size, round(timestamp, 3))
