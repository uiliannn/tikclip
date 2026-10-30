"""Per-platform post copy for one clip: title, description and tags, in one model call.

The output card ships with everything needed to publish, so nobody types a description on the
publish page. Each platform has its own rules (length, tag count, tone); the model writes for all
selected platforms of a clip at once and the rules are enforced here, never trusted. Anything
missing or malformed falls back to the clip title, so a post is never blocked on copy.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PostRules:
    language: str
    title_max: int
    description_max: int
    tags: tuple[int, int]
    style: str


# Product defaults. Hard title limits follow the platforms' upload forms as we know them
# (Xiaohongshu 20, Bilibili 80, YouTube 100); re-check when a platform changes its form.
# Shorter descriptions and tag counts are an editorial choice, not platform limits.
RULES = {
    'douyin': PostRules('pt', 80, 300, (3, 5), 'Título forte tipo gancho, tom conversacional em Português do Brasil; descrição curta incentivando comentários'),
    'xiaohongshu': PostRules('pt', 80, 600, (5, 8), 'Título chamativo e prático em Português do Brasil; descrição em 2–4 frases com os pontos principais'),
    'bilibili': PostRules('pt', 80, 250, (5, 10), 'Título completo com tema central em Português do Brasil; descrição apresentando o destaque'),
    'tiktok': PostRules('pt', 100, 300, (3, 5), 'TikTok: título com gancho viral forte em Português do Brasil, legenda direta e tom casual'),
    'instagram_reels': PostRules('pt', 100, 400, (5, 10), 'Instagram Reels: frase de impacto seguida de legenda curta com a lição principal em Português do Brasil'),
    'youtube_shorts': PostRules('pt', 100, 300, (3, 5), 'YouTube Shorts: título claro e atraente em Português do Brasil, descrição curta citando o tema'),
    'youtube_long': PostRules('pt', 100, 800, (5, 10), 'YouTube: título descritivo em Português do Brasil; descrição resumindo o trecho'),
    'original': PostRules('pt', 80, 300, (3, 5), 'Geral: título resumindo a ideia central e descrição curta em Português do Brasil'),
}
TAG_CHARS = 25

PROMPT = (
    'Você é um editor especialista em vídeos curtos virais (Reels, TikTok, Shorts). '
    'Com base nas legendas do trecho (lines), no título de referência (title_hint) e no contexto do vídeo original (source_context: título, canal, categorias, tags e descrição), escreva os textos de publicação para cada plataforma listada em platforms. '
    'IMPORTANTE: Todo o texto (title, description e tags) DEVE ser escrito OBRIGATORIAMENTE em PORTUGUÊS DO BRASIL (pt-BR), independentemente do idioma de title_hint! '
    'Regras: Seja fiel ao conteúdo original das falas, sem inventar fatos ou cargos; '
    'tags são palavras-chave sem #, específicas sobre o assunto tratado — use as legendas e o source_context como fonte; '
    'source_tags traz hashtags que o autor do vídeo já usava: reuse as que fizerem sentido. '
    'source é a origem conhecida do vídeo (quando vazio, não invente nome de podcast ou canal). '
    'Retorne APENAS JSON válido no formato: {"posts":{"id_da_plataforma":{"title":"...","description":"...","tags":["..."]}}}'
)


def _clean(text: Any) -> str:
    import html
    return re.sub(r'\s+', ' ', re.sub(r'[*_`]+', '', html.unescape(str(text or '')))).strip()


def _fit(text: str, limit: int) -> str:
    """Cut to `limit` characters at a sentence or phrase end when possible."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stops = [cut.rfind(mark) for mark in '。！？!?.；;，, ']
    best = max(stops)
    return (cut[:best + 1] if best >= limit * 0.6 else cut).rstrip('，,;； ')


def _tags(raw: Any, rules: PostRules, *, evidence: str | None = None) -> list[str]:
    tags, seen = [], set()
    evidence = unicodedata.normalize('NFKC', evidence).casefold() if evidence is not None else None
    for item in raw if isinstance(raw, list) else []:
        tag = re.sub(r'[#\s]+', '', _clean(item))[:TAG_CHARS]
        if evidence is not None and _parece_nome(item):
            literal = unicodedata.normalize('NFKC', tag).casefold()
            # Local uploads have no verified identity metadata. A model-generated
            # reference title cannot justify a person or company hashtag.
            if not literal or (re.search(r'[\u3400-\u9fff]', literal) is not None
                               and literal not in evidence):
                continue
            if not re.search(r'[\u3400-\u9fff]', literal) and not re.search(
                    rf'(?<!\w){re.escape(literal)}(?!\w)', evidence):
                continue
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)
    return tags[:rules.tags[1]]


FOREIGN_FOR_EN = re.compile(r'[\u3040-\u30ff\u3400-\u9fff]')
SOURCE_ATTRIBUTION = re.compile(
    r'出自|出处|摘自|来源\s*[:：]|[《【\[]\s*[》】\]]|'
    r'\bsource\s*:|\b(?:from|on)\s+(?:the\s+)?[^.!?\n]{0,100}\b(?:podcast|show|channel|interview)\b',
    re.IGNORECASE,
)


# A finite check for executive-role claims, not a general fact verifier. Only
# original subtitles can support an identity; generated titles/source listings cannot.
EXECUTIVE_ROLES = {
    'ceo': re.compile(
        r'(?<![a-z])(?:ceo|chief executive officer)(?![a-z])|首席执行官|首席執行官|'
        r'最高経営責任者|최고경영자|\bdirector ejecutivo\b|\bdirectora ejecutiva\b|'
        r'\bdiretor executivo\b|\bdiretora executiva\b|\bгенеральный директор\b|'
        r'\bdirecteur général\b|\bdirectrice générale\b', re.IGNORECASE,
    ),
    'company_leader': re.compile(
        r'(?:公司|企业|企業)\s*(?:的\s*)?(?:负责人|負責人|主管|掌舵人)|'
        r'\b(?:company|business|corporate)(?:[\'’]s)?\s+(?:leader|head|manager)\b|'
        r'\b(?:leader|head|manager)\s+of\s+(?:the\s+)?(?:company|business)\b|'
        r'(?:会社|企業)(?:の)?(?:責任者|代表|経営者)|'
        r'(?:회사|기업)(?:의)?\s*(?:책임자|대표|경영자)|'
        r'\b(?:líder|responsable|director|directora)\s+(?:de\s+)?(?:la\s+)?(?:empresa|compañía)\b|'
        r'\b(?:líder|responsável|diretor|diretora)\s+(?:de|da)\s+(?:empresa|companhia)\b|'
        r'\b(?:руководитель|глава)\s+(?:компании|предприятия)\b|'
        r'\b(?:dirigeant|dirigeante|responsable|chef)\s+(?:d[\'’]|de\s+(?:la\s+|l[\'’])?)'
        r'(?:entreprise|société)\b', re.IGNORECASE,
    ),
    'cto': re.compile(r'(?<![a-z])cto(?![a-z])', re.IGNORECASE),
    'cfo': re.compile(r'(?<![a-z])cfo(?![a-z])', re.IGNORECASE),
    'coo': re.compile(r'(?<![a-z])coo(?![a-z])', re.IGNORECASE),
}
FORMER_ROLE_PREFIX = re.compile(
    r'(?:\b(?:former|ex|previous|ancien|ancienne|antiguo|antigua|anterior|antigo|antiga|'
    r'бывший|бывшая)\s*[-–]?\s*|(?:前任?|元|旧|전|이전)\s*)$', re.IGNORECASE,
)


def _executive_claims(text: str) -> set[tuple[str, bool]]:
    text = unicodedata.normalize('NFKC', text).casefold()
    return {
        (role, bool(FORMER_ROLE_PREFIX.search(text[:match.start()])))
        for role, pattern in EXECUTIVE_ROLES.items() for match in pattern.finditer(text)
    }


def _unsupported_executive_role(description: str, evidence: str) -> bool:
    supported = _executive_claims(evidence)
    # An explicit CEO can be described less specifically as a company leader.
    # Keep current/former evidence separate; a generic leader cannot imply CEO.
    supported |= {('company_leader', former) for role, former in supported if role == 'ceo'}
    return bool(_executive_claims(description) - supported)


def fallback(title: str, platform: str, source_tags: list[str] | None = None) -> dict[str, Any]:
    rules = RULES.get(platform, RULES['original'])
    text = _clean(title)
    if rules.language in ('en', 'pt') and FOREIGN_FOR_EN.search(text):
        text = ''  # never a Chinese title on an English/Portuguese platform; the user fills it in
    # Hashtags do vídeo original primeiro; a reserva da plataforma só se não houver nenhuma.
    tags = _merge_source_tags([], source_tags, rules) or _fallback_tags(platform)
    return {'title': _fit(text, rules.title_max), 'description': '', 'tags': tags}


# Hashtags genéricos por plataforma: garantem que a publicação nunca saia sem
# nenhuma hashtag quando o modelo não devolve ou quando tudo é filtrado.
FALLBACK_TAGS = {
    'tiktok': ['paravoce', 'viral', 'fyp'],
    'reels': ['reels', 'viral', 'paravoce'],
    'shorts': ['shorts', 'viral'],
    'youtube': ['shorts', 'viral'],
    'douyin': ['热门', '推荐'],
    'xiaohongshu': ['日常', '分享'],
    'bilibili': ['日常', '分享'],
    'original': [],
}


def _fallback_tags(platform: str) -> list[str]:
    return list(FALLBACK_TAGS.get(platform, []))[:3]


def _parece_nome(item: Any) -> bool:
    """Hashtag com maiúscula inicial é nome próprio e precisa de prova literal.

    Hashtags comuns (#viral, #fyp, #paravoce) não aparecem no que foi falado;
    exigir prova para elas zerava a lista inteira.
    """
    texto = str(item or '').lstrip('#@ ').strip()
    return bool(texto) and texto[0].isupper()


def _merge_source_tags(tags: list[str], source_tags: list[str] | None,
                       rules: PostRules) -> list[str]:
    """Anexa hashtags que o autor do vídeo já usava: são prova, não invenção."""
    resultado = list(tags)
    vistos = {t.lower() for t in resultado}
    for bruto in source_tags or []:
        tag = re.sub(r'[#\s]+', '', _clean(bruto))[:TAG_CHARS]
        if not tag or tag.lower() in vistos:
            continue
        vistos.add(tag.lower())
        resultado.append(tag)
        if len(resultado) >= rules.tags[1]:
            break
    return resultado


def build_posts(title: str, lines: list[str], platforms: list[str], *, source: str = '',
                source_tags: list[str] | None = None,
                call: Callable[[str, dict], dict] | None = None) -> dict[str, dict[str, Any]]:
    """{platform: {'title','description','tags'}}; never raises for model problems."""
    platforms = [p for p in dict.fromkeys(platforms) if p in RULES]
    posts = {platform: fallback(title, platform, source_tags) for platform in platforms}
    source = source.strip()[:120]
    text = ' '.join(lines)[:6000]
    if not platforms or not text.strip():
        return posts
    if call is None:
        from backend.services.studio.intelligence import text_json as call
    payload = {'title_hint': title, 'source': source, 'source_context': source[:800],
               'source_tags': [str(t) for t in (source_tags or [])][:15],
               'lines': text, 'platforms': platforms,
               'rules': {p: {'language': r.language, 'title_max': r.title_max, 'description_max': r.description_max,
                             'tags': f'{r.tags[0]}-{r.tags[1]}', 'style': r.style} for p in platforms for r in [RULES[p]]}}
    for attempt in range(2):
        try:
            result = call(PROMPT, payload)
            raw = (result or {}).get('posts') if isinstance(result, dict) else None
            if not isinstance(raw, dict):
                raise ValueError('posts missing')
            mixed = []
            for platform in platforms:
                item = raw.get(platform) if isinstance(raw.get(platform), dict) else {}
                rules = RULES[platform]
                post_title = _fit(_clean(item.get('title')), rules.title_max)
                description = _fit(_clean(item.get('description')), rules.description_max)
                if not source and SOURCE_ATTRIBUTION.search(description):
                    # Local uploads have no listing provenance. Keep the useful title,
                    # but never ship a guessed programme or an empty source placeholder.
                    description = ''
                if _unsupported_executive_role(description, text):
                    # Keep valid title/tags and do not retry a model just for optional copy.
                    description = ''
                tags = _tags(item.get('tags'), rules, evidence=text if not source else None)
                tags = _merge_source_tags(tags, source_tags, rules)
                if rules.language in ('en', 'pt'):
                    if FOREIGN_FOR_EN.search(post_title):
                        mixed.append(platform)
                        continue
                    description = '' if FOREIGN_FOR_EN.search(description) else description
                    tags = [tag for tag in tags if not FOREIGN_FOR_EN.search(tag)]
                if post_title:
                    posts[platform] = {'title': post_title, 'description': description,
                                       'tags': tags or _fallback_tags(platform)}
            if mixed and attempt == 0:
                raise ValueError(f'{"、".join(mixed)}: títulos devem estar em Português sem caracteres em outros idiomas')
            return posts
        except Exception as error:  # noqa: BLE001 - copy falls back to the clip title
            logger.warning('Post copy rejected (attempt %d): %s', attempt + 1, type(error).__name__)
            payload = {**payload, 'previous_error': f'上一次返回不合格：{error}。请严格按格式重新返回。'}
    return posts
