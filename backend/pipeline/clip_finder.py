"""One-pass clip finding for fast output: the whole transcript in, finished clips out.

The legacy pipeline (outline → timeline → scoring → titles) was built for small context windows:
the transcript was cut into 5000-character chunks and each chunk went through four calls, with
JSON repair and mechanical length fixes in between (a 20 s range stretched to the 90 s minimum).
Today's models read a two-hour transcript at once, so one call returns complete clips — rows
from the question's start to the end of its answer, with title, reason and score.

Rows go to the model as `id|time|text` lines (no SRT timestamps: ~40 % fewer tokens). Long
videos are split into ~60-minute windows with overlap, analysed in parallel; overlapping picks
across windows are deduplicated. Local models (Ollama, LM Studio, a local/LAN compatible server)
usually run with a much smaller context, so they get ~20-minute / 12K-character windows instead
(RC156 Win QA #14b); cloud windows are unchanged. Every clip is validated, snapped by `refine_timeline` and
selected by `select_clips` exactly like the legacy path, and returned in the legacy
`titled_clips` shape so studio and the clip database are unchanged. Raises `ClipFinderError`
when nothing valid comes back, so the caller can fall back to the legacy steps.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
from pathlib import Path
from typing import Any

from .quality import DurationProfile, profile_from_srt, refine_timeline, save_profile, save_report, select_clips, to_seconds, to_srt_time

logger = logging.getLogger(__name__)

WINDOW_SEC = 60 * 60
OVERLAP_SEC = 4 * 60
WINDOW_CHARS = 70_000     # keep one request well inside a 128K-token context
DUPLICATE_OVERLAP = 0.5   # picks sharing more than half of the shorter one are the same moment
CJK = re.compile(r'[\u4e00-\u9fff]')


def short_video_profile(profile: DurationProfile) -> DurationProfile:
    """Clip lengths for short-video platforms.

    The legacy long-video tier (90 s minimum, 2–6 min target) was a podcast-clip rule; padding a
    60 s answer to 90 s drags in the next question. Short sources keep their own tighter tier.
    """
    if profile.tier == 'short':
        return profile
    return dataclasses.replace(profile, min_clip_sec=60, target_clip_sec=(90, 180), max_clip_sec=300)


class ClipFinderError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class WindowSize:
    seconds: float
    overlap: float
    chars: int
    kind: str


CLOUD_WINDOW = WindowSize(WINDOW_SEC, OVERLAP_SEC, WINDOW_CHARS, 'cloud')
# Local servers often default to a 4–8K context (Ollama num_ctx, LM Studio context length) and
# silently drop the start of a longer prompt. ~12K characters is ~8K tokens of transcript, which
# with the instructions and the answer fits a 16K context; the UI asks local users for >= 16384.
# The overlap keeps the cloud ratio (4 min per 60 min).
LOCAL_WINDOW_SEC = 20 * 60
LOCAL_WINDOW = WindowSize(LOCAL_WINDOW_SEC, OVERLAP_SEC * LOCAL_WINDOW_SEC / WINDOW_SEC, 12_000, 'local')


def window_size() -> WindowSize:
    """LOCAL_WINDOW while a local model is the analysis model, CLOUD_WINDOW otherwise (or when unsure)."""
    try:
        from backend.core.llm_manager import get_llm_manager
        from backend.core.local_presets import is_local_model
        manager = get_llm_manager()
        manager.get_current_provider_info()  # picks up a model switched in settings since the last call
        return LOCAL_WINDOW if is_local_model(manager.settings) else CLOUD_WINDOW
    except Exception as error:  # noqa: BLE001 - unknown provider: keep the established cloud windows
        logger.debug('Clip finder window size falls back to cloud: %s', error)
        return CLOUD_WINDOW


PROMPT = (
    'Você é um editor-chefe de vídeos curtos especializado em identificar e extrair os melhores cortes virais de podcasts, entrevistas e vídeos longos.\n'
    'Cada linha em rows é no formato "número_da_linha|tempo_inicio|texto", ordenada cronologicamente.\n'
    'Critérios de seleção: alto valor de informação, impacto emocional, curiosidade, frases de efeito e lógica completa.\n'
    'Regras de corte: cada corte deve conter uma linha de raciocínio, pergunta e resposta ou ponto alto completo, sem cortar frases pela metade.\n'
    'Duração de cada corte: entre {min_sec} e {max_sec} segundos (ideal entre {target_lo} e {target_hi} segundos).\n'
    'Quantidade: selecione entre {count_lo} e {count_hi} melhores cortes, sem sobreposição.\n'
    'REGRA OBRIGATÓRIA DE IDIOMA: O título (title) e o motivo da recomendação (reason) DEVEM ser redigidos OBRIGATORIAMENTE em PORTUGUÊS DO BRASIL (pt-BR).\n'
    'Para cada corte retorne: start / end (linhas inicial e final), title (título chamativo, viral e fiel em Português do Brasil, até 60 caracteres), reason (motivo em Português do Brasil, 15 a 30 palavras), score (0.0 a 1.0).\n'
    'Retorne APENAS JSON: {{"clips":[{{"start":0,"end":0,"title":"...","reason":"...","score":0.0}}]}}'
)


def _clock(sec: float) -> str:
    sec = int(sec)
    return f'{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}' if sec >= 3600 else f'{sec // 60}:{sec % 60:02d}'


def windows(rows: list[dict[str, Any]], size: WindowSize = CLOUD_WINDOW) -> list[tuple[int, int]]:
    """[first, last] row index ranges: ~size.seconds each, size.overlap shared, under size.chars."""
    if not rows:
        return []
    out, first = [], 0
    while first < len(rows):
        start, chars, last = rows[first]['start'], 0, first
        while last + 1 < len(rows) and rows[last + 1]['end'] - start <= size.seconds and chars + len(rows[last + 1]['text']) <= size.chars:
            last += 1
            chars += len(rows[last]['text'])
        out.append((first, last))
        if last + 1 >= len(rows):
            break
        back = last
        while back > first and rows[last]['end'] - rows[back]['start'] < size.overlap:
            back -= 1
        first = max(back, first + 1)
    return out


def _parse(raw: Any, first: int, last: int, rows, profile: DurationProfile) -> list[dict[str, Any]]:
    items = (raw or {}).get('clips') if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ValueError('response has no clips list')
    clips = []
    for item in items:
        if not isinstance(item, dict):
            continue
        start, end = item.get('start'), item.get('end')
        title = re.sub(r'[*_`#]+', '', str(item.get('title') or '')).strip()
        if not (isinstance(start, int) and isinstance(end, int)) or not first <= start <= end <= last or not title:
            continue
        length = rows[end]['end'] - rows[start]['start']
        if length < profile.min_clip_sec * 0.5 or length > profile.max_clip_sec * 1.5:
            continue  # refine_timeline adjusts lengths near the limits; far off means the model misread
        try:
            score = max(0.0, min(1.0, float(item.get('score'))))
        except (TypeError, ValueError):
            score = 0.5
        clips.append({'start_row': start, 'end_row': end, 'title': title[:60],
                      'reason': str(item.get('reason') or '').strip()[:120], 'score': score})
    if items and not clips:
        raise ValueError('no valid clips in response')
    if clips and sum(bool(CJK.search(c['title'])) for c in clips) < len(clips) / 2:
        raise ValueError('title 与 reason 必须用中文写')
    return clips


def _dedupe(clips: list[dict[str, Any]], rows) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for clip in sorted(clips, key=lambda c: c['score'], reverse=True):
        s, e = rows[clip['start_row']]['start'], rows[clip['end_row']]['end']
        duplicate = False
        for other in kept:
            os_, oe = rows[other['start_row']]['start'], rows[other['end_row']]['end']
            shared = min(e, oe) - max(s, os_)
            if shared > DUPLICATE_OVERLAP * min(e - s, oe - os_):
                duplicate = True
                break
        if not duplicate:
            kept.append(clip)
    return sorted(kept, key=lambda c: c['start_row'])


def _window_count(profile: DurationProfile, share: float) -> tuple[int, int]:
    lo, hi = profile.topics_hint
    return max(1, round(lo * share)), max(2, round(hi * share))


def find_clips(srt_entries: list[dict[str, Any]], call, *, threshold: float, metadata_dir: Path | None = None,
               window: WindowSize | None = None) -> list[dict[str, Any]]:
    """Titled clips (legacy `step4_titles.json` shape) from one pass over the transcript.

    `window` defaults to `window_size()` (smaller windows for local models)."""
    from .concurrency import map_chunks
    rows = [{'start': to_seconds(e['start_time']), 'end': to_seconds(e['end_time']), 'text': str(e.get('text') or '').strip()}
            for e in srt_entries if str(e.get('text') or '').strip()]
    if not rows:
        raise ClipFinderError('字幕为空')
    profile = short_video_profile(profile_from_srt(srt_entries))
    total = max(rows[-1]['end'] - rows[0]['start'], 1.0)
    window = window or window_size()
    spans = windows(rows, window)

    def one(span):
        first, last = span
        share = min(1.0, (rows[last]['end'] - rows[first]['start']) / total)
        count_lo, count_hi = _window_count(profile, share)
        prompt = PROMPT.format(min_sec=profile.min_clip_sec, max_sec=profile.max_clip_sec, target_lo=profile.target_clip_sec[0],
                               target_hi=profile.target_clip_sec[1], count_lo=count_lo, count_hi=count_hi)
        payload = {'rows': '\n'.join(f"{i}|{_clock(rows[i]['start'])}|{rows[i]['text']}" for i in range(first, last + 1))}
        for attempt in range(2):
            try:
                return _parse(call(prompt, payload), first, last, rows, profile)
            except Exception as error:  # noqa: BLE001 - one retry with the reason, then this window is lost
                logger.warning('Clip finder window %s-%s rejected (attempt %d): %s', first, last, attempt + 1, error)
                payload = {**payload, 'previous_error': f'上一次返回不合格：{error}。请严格按格式重新返回。'}
        return None

    results = map_chunks(one, spans)
    if all(r is None for r in results):
        raise ClipFinderError('模型没有返回可用的片段')
    picked = _dedupe([clip for result in results if result for clip in result], rows)
    items = []
    for n, clip in enumerate(picked, 1):
        start, end = rows[clip['start_row']]['start'], rows[clip['end_row']]['end']
        items.append({'id': str(n), 'start_time': to_srt_time(start), 'end_time': to_srt_time(end),
                      'outline': clip['title'], 'content': [clip['reason']] if clip['reason'] else [],
                      'chunk_index': 0, 'final_score': clip['score'], 'recommend_reason': clip['reason'],
                      'generated_title': clip['title'], 'score_source': 'llm'})
    refined, report = refine_timeline(items, srt_entries, profile)
    chosen, selection = select_clips(refined, threshold, profile)
    if metadata_dir is not None:
        metadata_dir = Path(metadata_dir)
        save_profile(profile, metadata_dir)
        save_report({'clip_finder': {'windows': len(spans), 'window': window.kind, 'failed_windows': sum(r is None for r in results),
                                     'picked': len(picked), 'refine': report, 'selection': selection}}, metadata_dir)
        for name, data in (('step3_all_scored.json', refined), ('step4_titles.json', chosen)):
            (metadata_dir / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    if not chosen:
        raise ClipFinderError('没有片段通过评分筛选')
    logger.info('Clip finder: %d %s windows, %d picked, %d kept', len(spans), window.kind, len(picked), len(chosen))
    return chosen
