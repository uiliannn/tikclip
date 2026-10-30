"""Bounded still-frame analysis through an OpenAI-compatible vision endpoint."""
import base64
import json
import math
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from fractions import Fraction
from http.client import HTTPException
from typing import Literal
from pydantic import Field
from backend.services.studio.models import Scene, Draft, Preferences
from backend.services.publish_export import _probe
from backend.utils.ffmpeg_utils import get_ffmpeg_path, get_ffprobe_path


def visual_config():
    from backend.services.studio.vision_settings import effective
    c = effective()
    return c.get('api_key', ''), c['base_url'], c['model']

def ready():
    _, base, model = visual_config()
    return bool(base and model)


def source_frame_analysis_allowed():
    """Automatic frame helpers require current consent as well as a model.

    A legacy vision binding can remain configured while the user turns frame
    analysis off. Invalid saved settings must never authorize transmission.
    """
    from backend.services.studio import analysis_preferences
    try:
        return analysis_preferences.visual_screening_allowed(
            analysis_preferences.load(), vision_configured=ready())
    except ValueError:
        return False

def decode_json(raw):
    raw = raw.strip()
    if raw.startswith('```'):
        raw = raw.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    return json.loads(raw)

def text_json(prompt, data):
    from backend.utils.llm_client import LLMClient
    client = LLMClient()
    prompt += '\nResponda APENAS em JSON valido, sem Markdown. Todos os titulos e textos DEVEM ser estritamente em PORTUGUES DO BRASIL (pt-BR). E PROIBIDO usar chines.'
    if not client.llm_manager.get_current_provider_info().get('available'):
        raise ValueError('Modelo de texto indisponivel. Configure o modelo nas configuracoes.')
    return decode_json(client.call(prompt, data))

class VisionRequestError(RuntimeError):
    """Stable diagnostics without URLs, prompts, keys or provider response bodies."""
    def __init__(self, code, message, *, elapsed_seconds=0, http_status=None):
        super().__init__(message)
        self.code = code
        self.elapsed_seconds = round(max(0, elapsed_seconds), 2)
        self.http_status = http_status
        self.phase = 'request'

    def diagnostics(self):
        result = {'code': self.code, 'phase': self.phase, 'elapsed_seconds': self.elapsed_seconds}
        if self.http_status is not None:
            result['http_status'] = self.http_status
        return result


def vision_call_at(phase, content):
    try:
        if phase in ('scan', 'refine'):
            return vision_call(content, allow_event_list=True)
        return vision_call(content)
    except VisionRequestError as error:
        error.phase = phase
        raise


def vision_call(content, config=None, *, allow_event_list=False):
    from backend.core.project_cancellation import checkpoint
    checkpoint()  # the project was deleted: no more paid calls for it (RC156 #12)
    from backend.services.studio.vision_settings import effective
    config = config or effective()
    key, base, model = config.get('api_key', ''), config['base_url'], config['model']
    if not base or not model:
        raise ValueError('O modelo atual só trabalha com texto e não enxerga imagem; troque por um modelo multimodal em «Ajustes → Análise por IA» ou use análise por legenda.')
    body = {'model': model, 'messages': [{'role': 'user', 'content': content}], 'max_tokens': 1000 if config.get('quick_screening') else 4000}
    # Structured visual observation must finish within the bounded request budget.
    # Other compatible providers must not receive Seed-specific parameters.
    if model.lower().startswith('doubao-seed'):
        body['thinking'] = {'type': 'disabled'}
    from backend.core.llm_providers import disables_qwen_thinking
    if disables_qwen_thinking(model, base):
        # Qwen3.5–3.8 on Bailian think by default (RC156 Win QA #18); allowlist only, see llm_providers.
        body['enable_thinking'] = False
    req = urllib.request.Request(base.rstrip('/') + '/chat/completions', data=json.dumps(body).encode(), headers={**({'Authorization': 'Bearer ' + key} if key else {}), 'Content-Type': 'application/json'})
    started = time.monotonic()

    def failure(code, message, status=None):
        return VisionRequestError(code, message, elapsed_seconds=time.monotonic()-started, http_status=status)

    try:
        with urllib.request.urlopen(req, timeout=config.get('timeout', 180)) as response:
            result = json.load(response)
    except TimeoutError:
        raise failure('timeout', 'O modelo de visão demorou além do limite; aumente o tempo de requisição em «Ajustes → Compreensão visual» e tente de novo. O material original foi mantido.') from None
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        code = 'rate_limited' if status == 429 else 'authentication' if status in (401, 403) else 'provider_error'
        message = 'Requisições ao modelo de visão em excesso; tente de novo mais tarde, manualmente. Não haverá nova tentativa automática.' if status == 429 else f'视觉模型请求失败（HTTP {status}），请检查视觉模型设置后重试'
        raise failure(code, message, status) from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise failure('timeout', 'Tempo esgotado ao conectar ao modelo de visão; verifique a rede e tente de novo. O material original foi mantido.') from None
        raise failure('connection', 'Não foi possível conectar ao modelo de visão; confira o endereço do serviço e a rede, e tente de novo.') from None
    except (OSError, EOFError, HTTPException):
        raise failure('connection', 'A conexão com o modelo de visão foi interrompida; tente de novo em instantes. O material original foi mantido.') from None
    except (ValueError, UnicodeError):
        raise failure('invalid_response', 'O modelo de visão devolveu uma resposta que não deu para interpretar; verifique a compatibilidade do serviço e tente de novo.') from None
    from backend.core import llm_usage
    llm_usage.record(model, result.get('usage') if isinstance(result, dict) else None, kind='vision',
                     prompt_chars=sum(len(part.get('text', '')) for part in content if isinstance(part, dict)),
                     completion_chars=len(str(((result.get('choices') or [{}])[0].get('message') or {}).get('content') or '')) if isinstance(result, dict) else 0,
                     images=sum(1 for part in content if isinstance(part, dict) and part.get('type') == 'image_url'))
    try:
        choice = result['choices'][0]
        if choice.get('finish_reason') == 'length':
            raise failure('output_truncated', 'A resposta do modelo de visão foi cortada e o resultado ficou incompleto; use um material mais curto ou outro modelo de visão.')
        if choice.get('finish_reason') == 'content_filter' or choice.get('message', {}).get('refusal'):
            raise failure('refused', 'O modelo de visão não processou este material; verifique o arquivo ou troque de modelo.')
        value = decode_json(choice['message']['content'])
        # Some compatible models return the requested events as a bare array.
        # Only scan/refinement declare that meaning; other stages remain strict.
        if allow_event_list and isinstance(value, list) and all(isinstance(item, dict) for item in value):
            value = {'events': value}
        if not isinstance(value, dict):
            raise ValueError('Expected a JSON object')
        return value
    except VisionRequestError:
        raise
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise failure('invalid_response', 'O modelo de visão não devolveu o resultado estruturado completo; verifique a compatibilidade do modelo e tente de novo.') from None

def validate_scenes(scenes, duration):
    if not scenes:
        raise ValueError('Nenhum trecho aproveitável foi encontrado')
    for scene in scenes:
        if scene.end > duration + .05:
            raise ValueError('O trecho ultrapassa a duração do vídeo original')
    if sum(s.end - s.start for s in scenes) > 1800:
        raise ValueError('Cada peça final não pode passar de 30 minutos')

def _sampling_bounds(video):
    """Container/audio duration can outlive the last decodable video frame."""
    try:
        result = subprocess.run([get_ffprobe_path(), '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=duration,avg_frame_rate:format=duration', '-of', 'json', str(video)],
            check=True, capture_output=True, timeout=30)
        info = json.loads(result.stdout)
        stream = info['streams'][0]
        try:
            duration = float(stream.get('duration', ''))
        except (ValueError, TypeError):
            duration = float(info.get('format', {}).get('duration', 0))
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError('Invalid video duration')
        try:
            fps = float(Fraction(stream.get('avg_frame_rate', '0/0')))
            frame_seconds = 1 / fps if math.isfinite(fps) and fps > 0 else 1
        except (ValueError, TypeError, ZeroDivisionError):
            frame_seconds = 1
        return max(0, duration - max(.1, frame_seconds) - .001), max(.5, frame_seconds)
    except subprocess.TimeoutExpired:
        raise VisionRequestError('timeout', 'Tempo esgotado ao ler imagens do material; verifique o vídeo e tente de novo. O material original foi mantido.') from None
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError):
        raise VisionRequestError('missing_resource', 'Não foi possível ler imagens do material; verifique o vídeo e tente de novo. O material original foi mantido.') from None


def sample(video, times, folder, width=640):
    last_seek, retry_step = _sampling_bounds(video)
    content = []
    for index, timestamp in enumerate(times):
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError('Os instantes de amostragem de imagem precisam estar dentro do material original')
        timestamp = min(timestamp, last_seek)
        frame = folder / f'{index}.jpg'
        # VFR or rounded metadata may still put a tail seek past the final frame.
        # Retry that one frame once earlier; never send a partial/stale frame set.
        seeks = [timestamp]
        if timestamp >= last_seek - retry_step and timestamp > 0:
            seeks.append(max(0, timestamp - retry_step))
        for seek in seeks:
            try:
                frame.unlink(missing_ok=True)
                subprocess.run([get_ffmpeg_path(), '-v', 'error', '-threads', '1', '-ss', str(seek),
                    '-i', str(video), '-frames:v', '1', '-vf', f'scale={width}:-2',
                    '-threads', '1', '-filter_threads', '1', '-y', str(frame)],
                    check=True, capture_output=True, timeout=30)
                image = frame.read_bytes()
                if not image:
                    raise FileNotFoundError('Empty sampled frame')
                timestamp = seek
                break
            except subprocess.TimeoutExpired:
                raise VisionRequestError('timeout', 'Tempo esgotado ao ler imagens do material; verifique o vídeo e tente de novo. O material original foi mantido.') from None
            except (OSError, subprocess.SubprocessError):
                try:
                    frame.unlink(missing_ok=True)
                except OSError:
                    pass  # A denied output directory must still produce a safe diagnostic.
        else:
            raise VisionRequestError('missing_resource', 'Não foi possível ler imagens do material; verifique o vídeo e tente de novo. O material original foi mantido.') from None
        content.extend([{'type': 'text', 'text': f'原片时间 {timestamp:.2f} 秒'}, {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(image).decode()}}])
    if not content:
        raise VisionRequestError('missing_resource', 'Não há imagens utilizáveis no material; verifique o vídeo e tente de novo. O material original foi mantido.')
    return content


class HighlightCandidate(Scene):
    """Model estimates for editing priority, never advertising-performance evidence.

    Optional fields allow older compatible endpoints to return plain Scene objects.
    Keep this analysis-only metadata out of saved scenes and historical drafts.
    """
    event_type: Literal['gameplay', 'menu', 'reward_screen', 'loading', 'other', 'unknown'] = 'unknown'
    watch_score: int | None = Field(default=None, ge=0, le=100, strict=True)
    selection_reason: str = Field(default='', max_length=500)


NON_PLAY_EVENTS = {'menu', 'reward_screen', 'loading'}


def _teto_do_corte(duracao_referencia: int) -> float:
    """Teto real de um corte: a referência do plano com 50% de folga, limitada a 120 s.

    Antes o valor de referência era usado como tamanho exato: `end = start + duração`,
    o que truncava a cena no meio da ideia e fazia todo corte sair com o mesmo tamanho.
    """
    referencia = max(1.0, float(duracao_referencia))
    return min(120.0, max(referencia, referencia * 1.5))


def select_highlights(raw_events, duration, limit=6, *, max_duration=None):
    candidates = [HighlightCandidate.model_validate(e) for e in raw_events[:12]]
    validate_scenes(candidates, duration)
    if len({e.id for e in candidates}) != len(candidates):
        raise ValueError('O modelo devolveu identificadores repetidos nos candidatos; tente de novo.')
    # Filter only explicit categories, never words in the label (e.g. winning a reward
    # during active gameplay is a valid event). Missing annotations remain usable.
    playable = [e for e in candidates if e.event_type not in NON_PLAY_EVENTS]
    too_long = {e.id for e in playable if max_duration is not None and e.end - e.start > max_duration + .1}
    usable = [e for e in playable if e.id not in too_long]
    if playable and not usable:
        raise ValueError('O trecho escolhido pelo modelo passou da duração desejada; tente de novo.')
    usable.sort(key=lambda e: -(e.watch_score if e.watch_score is not None else 0))
    selected = usable[:limit]
    if not selected:
        raise ValueError('Não achei destaque de jogabilidade: os candidatos só têm menu, premiação ou tela de carregamento. Use um trecho que mostre o jogo de verdade.')
    ids = {e.id for e in selected}
    audit = [{'id':e.id, 'label':e.label, 'start':e.start, 'end':e.end,
              'event_type':e.event_type, 'watch_score':e.watch_score,
              'selection_reason':e.selection_reason,
              'disposition':'selected' if e.id in ids else 'filtered' if e.event_type in NON_PLAY_EVENTS else 'duration_filtered' if e.id in too_long else 'limit'}
             for e in candidates]
    scenes = [Scene.model_validate({k:v for k,v in e.model_dump().items() if k in Scene.model_fields}) for e in selected]
    return scenes, audit

def analyze(video: Path, prefs: Preferences, on_stage=None, instruction=""):
    duration = _probe(video).get('duration', 0)
    if duration < 1 or duration > 7200:
        raise ValueError('A análise visual aceita materiais de 1 segundo a 2 horas')
    interval = max(2, duration / 60)
    times = [round(t * interval, 3) for t in range(min(60, math.ceil(duration / interval))) if t * interval < duration - .1]
    prompt = (
        'Voce e um editor de video profissional e especialista em cortes virais para redes sociais (TikTok, Instagram Reels, YouTube Shorts). '
        'Abaixo estao frames ordenados cronologicamente capturados do video. '
        'Identifique ate 12 eventos candidatos independentes e com alto potencial de atracao e retencao, para reter ate 6 destaques. '
        'Cada corte deve girar em torno de uma cena interessante, reacao marcante, conversa curiosa ou momento impactante. '
        'REGRA OBRIGATORIA: O campo "label" (titulo do clipe) DEVE ser escrito OBRIGATORIAMENTE em PORTUGUES DO BRASIL (pt-BR). '
        'Crie titulos atraentes, diretos e chamativos para o TikTok (ex: "Momento inacreditavel", "Ele nao esperava por isso", "Revelacao surpreendente"). '
        'E ESTRITAMENTE PROIBIDO usar chines ou ingles no campo "label" ou "evidence". '
        'Para cada candidato, classifique event_type (gameplay/conteudo principal, menu, reward_screen, loading, other). '
        'watch_score e uma pontuacao de 0 a 100 de potencial de visualizacao. '
        f'Duracao do video: {duration:.2f}s, intervalo: {interval:.2f}s, duracao de referencia do corte: {prefs.duration}s '
        f'(escolha o fim no fim natural da ideia; nao passe de {_teto_do_corte(prefs.duration):.0f}s). '
        'Retorne APENAS um objeto JSON valido: '
        '{"events":[{"id":"event-1","label":"Titulo Chamativo em Portugues","start":segundos,"end":segundos,"evidence":"descricao em portugues","event_type":"gameplay","watch_score":85,"selection_reason":"justificativa em portugues"}]}.'
    )
    if instruction:
        prompt += '\nRequisitos do usuario: ' + instruction
    if on_stage:
        on_stage('Analisando imagens e identificando melhores momentos')
    with tempfile.TemporaryDirectory(prefix='ac-vision-') as tmp:
        response = vision_call_at('scan', [{'type': 'text', 'text': prompt}] + sample(video, times, Path(tmp)))
    teto = _teto_do_corte(prefs.duration)
    events, selection = select_highlights(response.get('events', []), duration, max_duration=teto)
    if any(e.end - e.start > teto + .1 for e in events):
        raise ValueError('O trecho escolhido pelo modelo passou da duração desejada; tente de novo.')
    # Refine the strongest candidate with a denser second pass; preserve exact source timestamps.
    best = events[0]
    start, end = max(0, best.start - 2), min(duration - .1, best.end + 2)
    dense_interval = max(1, (end - start) / 24)
    dense_times = [start + i * dense_interval for i in range(25) if start + i * dense_interval <= end]
    if on_stage:
        on_stage('Refinando limites exatos do melhor momento')
    with tempfile.TemporaryDirectory(prefix='ac-vision-refine-') as tmp:
        detail = vision_call_at('refine', [{'type': 'text', 'text': prompt + f' Revisao do intervalo {start:.2f}–{end:.2f}s. Mantenha os titulos e textos estritamente em PORTUGUES DO BRASIL (pt-BR). NUNCA use chines: ' + json.dumps(best.model_dump(), ensure_ascii=False)}] + sample(video, dense_times, Path(tmp)))
    refined_candidates = [HighlightCandidate.model_validate(e) for e in detail.get('events', [])[:1]]
    rejected = bool(refined_candidates and refined_candidates[0].event_type in NON_PLAY_EVENTS)
    if rejected:
        events = events[1:]
        for item in selection:
            if item['id'] == best.id:
                item['disposition'] = 'refine_filtered'
                item['refine_reason'] = refined_candidates[0].selection_reason or refined_candidates[0].evidence
        if not events:
            raise ValueError('Não achei destaque de jogabilidade: na revisão, o melhor trecho não mostra o jogo. Use outro material.')
        refined_candidates = []
    refined = [Scene.model_validate({k:v for k,v in e.model_dump().items() if k in Scene.model_fields}) for e in refined_candidates]
    if refined:
        validate_scenes(refined, duration)
        if refined[0].start < start or refined[0].end > end + .05 or refined[0].end - refined[0].start > _teto_do_corte(prefs.duration) + .1:
            raise ValueError('Os limites definidos na revisão do modelo saíram do intervalo amostrado; tente de novo.')
        events[0] = refined[0].model_copy(update={'id': best.id})
    return events, {'duration': duration, 'sample_interval': interval, 'refine_interval': dense_interval, 'selection': selection, 'refined_event_id': best.id if refined else None, 'note': 'Seleção feita a partir de imagens estáticas em ordem e ordenada por valor de audiência; foram excluídos menu, premiação e carregamento que o modelo marcou explicitamente. A classificação e a ordem ainda dependem de conferência humana e não garantem desempenho de anúncio. Resultados antigos sem marcação de tipo são mantidos.'}

def assemble_sequences(events, prefs, source_duration):
    """Keep independent event identities; add bounded context to each, never merge by proximity."""
    if not events or source_duration is None:
        return events
    result = []
    for event in events:
        lead = max(0, min(1.5, (prefs.duration - (event.end - event.start)) / 2))
        start = max(0, event.start - lead)
        end = min(source_duration, event.end + 2, start + _teto_do_corte(prefs.duration))
        result.append(event.model_copy(update={'start':start, 'end':end}))
    return result


def _sanitize_title_pt(text: str, index: int = 1) -> str:
    import re
    if not text:
        return f"Melhor Momento {index}"
    # Se contiver caracteres chineses/asiaticos, converter/substituir imediatamente
    if re.search(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]', text):
        CHINESE_MAP = {
            '台球桌日常展示': 'A Incrivel Mesa de Sinuca',
            '车载导航': 'Chegando no Local',
            '出行准备': 'Partiu Tour na Mansao',
            '室内参观': 'Tour Completo por Dentro',
            '室外交流': 'Area Externa Incrivel',
            '沙发聊天': 'Bate-papo Exclusivo',
            '日常展示': 'Exibicao do Local',
            '高光': 'Melhor Momento',
            '展示': 'Destaque',
            '精彩瞬间': 'Cena Marcante',
            '日常': 'Dia a Dia',
            '游戏': 'Jogada Incrivel',
            '导航': 'No Caminho',
            '准备': 'Preparacao',
            '参观': 'Visita Completa',
            '交流': 'Conversa Top',
            '聊天': 'Bate-papo',
        }
        for k, v in CHINESE_MAP.items():
            text = text.replace(k, v)
        if re.search(r'[\u4e00-\u9fff]', text):
            return f"Momento Incrivel {index}"
    return text.strip()

def make_drafts(events, prefs, instruction="", *, source_duration=None):
    events = assemble_sequences(events, prefs, source_duration)
    import uuid
    if prefs.goal == 'promo':
        scene = events[0]
        plans = vision_call_at('hooks', [{'type': 'text', 'text': 'Crie 3 titulos de abertura diferentes em PORTUGUES DO BRASIL para redes sociais (TikTok/Reels): uma pergunta curiosa, um desafio e um suspense. Maximo 24 caracteres por titulo. Retorne {"hooks":[{"title":"Titulo em Portugues","hook":"Gancho em Portugues"}]}. PROIBIDO USAR CHINES: ' + json.dumps(scene.model_dump(), ensure_ascii=False)}])
        hooks = plans.get('hooks', [])[:3]
        if not hooks:
            raise ValueError('O modelo nao retornou propostas de cortes')
        return [Draft(id=uuid.uuid4().hex, title=_sanitize_title_pt(h.get('title', ''), i+1), hook=_sanitize_title_pt(h.get('hook', ''), i+1), scenes=[scene], language=prefs.language, aspect=prefs.aspect, layout='crop' if prefs.aspect == 'portrait' else 'fit', title_style='comic', title_template_version=6, subtitles=False, origin='visual-promo').model_dump() for i, h in enumerate(hooks)]
    
    drafts = []
    for i, e in enumerate(events):
        pt_title = _sanitize_title_pt(e.label, i+1)
        drafts.append(Draft(id=uuid.uuid4().hex, title=pt_title, scenes=[e], language=prefs.language, aspect=prefs.aspect, layout='crop' if prefs.aspect == 'portrait' else 'fit', subtitles=False, origin='visual-highlight').model_dump())
    return drafts
