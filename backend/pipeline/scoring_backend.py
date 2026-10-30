"""Step 3 scoring contract; provider selection remains with LLMClient by default."""
from __future__ import annotations

from typing import Any, Dict, List, Protocol, Sequence, TypedDict


class ScoreResult(TypedDict, total=False):
    id: str
    outline: str
    final_score: float
    recommend_reason: str


class ScoringBackend(Protocol):
    """Evaluate clips identified by id/outline, returning final_score and reason.

    Input includes transcript, content and absolute start/end times. Return a
    JSON-compatible list; scores use 0–1. Missing/invalid results are handled by
    the pipeline's alignment and fallback guards. Implementations must retain
    clip identifiers; they do not select, reorder or cut the source video.
    """

    def score(self, clips: Sequence[Dict[str, Any]]) -> List[ScoreResult]: ...


SCORING_CONTRACT = '''

## Contrato de saída desta avaliação (sobrepõe os exemplos acima)
Sua tarefa é avaliar o valor de conteúdo de cada trecho, com base no transcript original, no content e no outline.
Devolva um array JSON com um objeto para cada trecho de entrada, preservando o id e o outline originais:
[{"id":"id original","outline":"outline original","final_score":0.8,"recommend_reason":"motivo curto em português, baseado no texto"}]
final_score precisa ser um número finito entre 0 e 1; sem evidência suficiente no conteúdo, não invente nota alta.
Não devolva um dicionário de id para motivo, não omita as notas e não altere id nem outline.
'''


class LLMScoringBackend:
    """Current LLM scoring with a consistent schema for every category prompt."""

    def __init__(self, client: Any, prompt: str):
        self.client = client
        self.prompt = prompt + SCORING_CONTRACT

    def score(self, clips: Sequence[Dict[str, Any]]) -> List[ScoreResult]:
        response = self.client.call_with_retry(self.prompt, list(clips))
        results = self.client.parse_json_response(response)
        return results if isinstance(results, list) else []
