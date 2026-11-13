"""Aproveita os trechos que a análise pontuou mas não entraram na primeira leva.

A análise grava `step3_all_scored.json` com **todos** os trechos avaliados e
`step3_high_score_clips.json` com os escolhidos. Aqueles que sobraram viram cortes
novos sem gastar IA de novo — só o render, que é local.
"""
from __future__ import annotations

import json
import re
import uuid

NOME_ARQUIVO = 'step3_all_scored.json'
TOLERANCIA = 1.5  # segundos de sobreposição aceitos ao comparar com o que já existe


def _segundos(texto: str) -> float:
    """'00:01:15,780' -> 75.78"""
    achado = re.match(r'\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})', str(texto))
    if not achado:
        raise ValueError(f'tempo inválido: {texto!r}')
    h, m, s, ms = achado.groups()
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, '0')) / 1000


def _faixas_usadas(estado: dict) -> list[tuple[float, float]]:
    faixas = []
    for rascunho in estado.get('drafts') or []:
        for cena in rascunho.get('scenes') or []:
            try:
                faixas.append((float(cena['start']), float(cena['end'])))
            except (KeyError, TypeError, ValueError):
                continue
    return faixas


def _sobrepoe(inicio: float, fim: float, faixas: list[tuple[float, float]]) -> bool:
    for a, b in faixas:
        if min(fim, b) - max(inicio, a) > TOLERANCIA:
            return True
    return False


def candidatos_livres(pid: str) -> list[dict]:
    """Trechos pontuados que ainda não viraram corte."""
    from backend.services.studio import store

    pasta = store.directory(pid) / 'metadata'
    arquivo = pasta / NOME_ARQUIVO
    if not arquivo.is_file():
        return []
    try:
        itens = json.loads(arquivo.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    estado = store.read(pid)
    faixas = _faixas_usadas(estado)
    livres = []
    for item in itens if isinstance(itens, list) else []:
        try:
            inicio = _segundos(item['start_time'])
            fim = _segundos(item['end_time'])
        except (KeyError, TypeError, ValueError):
            continue
        if fim - inicio < 5 or _sobrepoe(inicio, fim, faixas):
            continue
        livres.append({'inicio': round(inicio, 2), 'fim': round(fim, 2),
                       'titulo': str(item.get('outline') or 'Trecho').strip()[:120],
                       'motivo': str(item.get('recommend_reason') or '')[:500],
                       'nota': item.get('final_score')})
    livres.sort(key=lambda c: (-(c['nota'] or 0), c['inicio']))
    return livres


def _modelo(estado: dict) -> dict:
    """Copia o estilo do último corte para o novo ficar igual."""
    rascunhos = estado.get('drafts') or []
    if not rascunhos:
        return {'language': 'source', 'aspect': 'portrait', 'layout': 'fit', 'crop_x': 0.5,
                'title_style': 'plain', 'title_template_version': 1, 'title_motion': True,
                'title_scale': 1.0, 'title_y': 0.12, 'title_accent': None,
                'subtitles': True, 'subtitle_style': 'clean', 'original_audio': True}
    base = rascunhos[-1]
    campos = ('language', 'aspect', 'layout', 'crop_x', 'title_style',
              'title_template_version', 'title_motion', 'title_scale', 'title_y',
              'title_accent', 'subtitles', 'subtitle_style', 'original_audio')
    return {k: base[k] for k in campos if k in base}


def adicionar(pid: str, quantidade: int = 2) -> dict:
    """Cria rascunhos a partir dos trechos livres. Não renderiza."""
    from backend.services.studio import store
    from backend.services.studio.models import Draft, Scene

    disponiveis = candidatos_livres(pid)
    if not disponiveis:
        return {'criados': [], 'restantes': 0,
                'mensagem': 'A análise não deixou trechos de reserva. '
                            'Rode "Analisar de novo" pedindo mais cortes.'}
    escolhidos = disponiveis[:max(1, min(10, quantidade))]
    estado = store.read(pid)
    estilo = _modelo(estado)
    criados = []
    for candidato in escolhidos:
        cena = Scene(id=f'am-{uuid.uuid4().hex[:8]}', label=candidato['titulo'][:120],
                     start=candidato['inicio'], end=candidato['fim'],
                     evidence=candidato['motivo'])
        rascunho = Draft(
            id=uuid.uuid4().hex, title=candidato['titulo'] or 'Trecho',
            hook='', scenes=[cena], origin='mais-cortes', **estilo)
        store.save_draft(pid, rascunho, create=True)
        criados.append({'titulo': candidato['titulo'], 'inicio': candidato['inicio'],
                        'fim': candidato['fim']})
    return {'criados': criados, 'restantes': max(0, len(disponiveis) - len(criados)),
            'mensagem': f'{len(criados)} corte(s) novo(s) criado(s).'}


if __name__ == '__main__':
    import sys
    print(json.dumps(adicionar(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 2),
                     ensure_ascii=False, indent=2))
