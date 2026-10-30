"""Text-only promotional drafts grounded in existing subtitle selections."""
import uuid
from pydantic import BaseModel, ConfigDict, Field
from backend.services.studio import intelligence
from backend.services.studio.models import Draft
from backend.services.studio.subtitle_highlights import make_highlights
from backend.services.publish_export import _load_srt_entries
from backend.pipeline.quality import to_seconds


class PromoChoice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    candidate: int = Field(ge=0, le=5)
    title: str = Field(min_length=1, max_length=200)
    hook: str = Field(min_length=1, max_length=120)


class PromoResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')
    drafts: list[PromoChoice] = Field(min_length=1, max_length=3)


def make_promos(project_id, clips, preferences, source_duration, instruction=''):
    candidates = make_highlights(clips, preferences, source_duration)
    entries = _load_srt_entries(project_id)
    evidence = []
    for i, draft in enumerate(candidates):
        scene = draft['scenes'][0]
        # A title alone is insufficient evidence for promotional claims.
        texts = [str(e.get('text', '')) for e in entries
                 if to_seconds(e['start_time']) < scene['end']
                 and to_seconds(e['end_time']) > scene['start']]
        transcript = '\n'.join(texts).strip()[:1500]
        if transcript:
            evidence.append({'candidate': i, 'transcript': transcript})
    if not evidence:
        raise ValueError('Os rascunhos de divulgação precisam de legenda disponível; confira a legenda e tente de novo. O material original foi mantido.')
    response = PromoResponse.model_validate(intelligence.text_json(
        'Com base nas legendas, escolha trechos completos que funcionem como divulgação e '
        'devolva de 1 a 3 rascunhos. '
        'Retorne {"drafts":[{"candidate":0,"title":"título em português","hook":"abertura curta '
        'em português"}]}. '
        'candidate só pode ser um número que exista na entrada; prefira temas diferentes e não '
        'repita o mesmo trecho só para aumentar a quantidade. '
        'O título e a abertura precisam ter base nas legendas: não invente jogadas, preços, '
        'promessas, resultado de partida nem desempenho de anúncio. '
        'São rascunhos de divulgação para revisão humana, não anúncios verificados. '
        'Não altere o início nem o fim das cenas. Escreva tudo em português do Brasil.',
        {'candidates': evidence, 'language': preferences.language, 'instruction': instruction[:1000]}))
    valid = {e['candidate'] for e in evidence}
    if any(choice.candidate not in valid for choice in response.drafts):
        raise ValueError('O plano de divulgação citou um trecho inválido; tente de novo. O rascunho original foi mantido.')
    output = []
    seen = set()
    for choice in response.drafts:
        if choice.candidate in seen:
            continue
        seen.add(choice.candidate)
        if not choice.title.strip() or not choice.hook.strip():
            raise ValueError('O texto de divulgação veio vazio; tente de novo. O rascunho original foi mantido.')
        posicao = len(output) + 1
        output.append(Draft.model_validate({**candidates[choice.candidate],
            'id': uuid.uuid4().hex,
            # sem o sanitizador, título asiático vindo do modelo era queimado no vídeo
            'title': intelligence._sanitize_title_pt(choice.title.strip(), posicao),
            'hook': intelligence._sanitize_title_pt(choice.hook.strip(), posicao),
            'origin': 'subtitle-promo', 'title_style': 'comic', 'title_template_version': 6}).model_dump())
    return output
