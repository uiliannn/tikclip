"""Regressão: detecção de projeto pronto para auto-publicação.

Bug original: `_verificar_auto_publicacoes` filtrava
`j.get("type") in ("render", "export")`, mas os jobs gravados no studio.json
não têm o campo `type` (vem `None`). A lista ficava sempre vazia, `all_done`
era sempre falso e a publicação automática nunca disparava.
"""
from __future__ import annotations

import channel_monitor as cm


def estado(status_geracao, estados_variantes, jobs=None):
    return {
        "generation": {"status": status_geracao, "auto_start": True},
        "output_variants": [
            {"id": f"v{i}", "status": s} for i, s in enumerate(estados_variantes)
        ],
        "jobs": jobs if jobs is not None else [],
    }


class TestProjetoPronto:
    def test_jobs_sem_type_nao_bloqueiam(self):
        """O caso real: jobs existem, mas sem "type" — precisa publicar mesmo assim."""
        jobs_reais = [{"job_id": "a", "status": "completed"}, {"job_id": "b", "status": "completed"}]
        assert cm._projeto_pronto_para_publicar(estado("completed", ["completed"], jobs_reais)) is True

    def test_partial_conta_como_pronto(self):
        assert cm._projeto_pronto_para_publicar(estado("partial", ["completed", "failed"])) is True

    def test_rendering_nao_esta_pronto(self):
        assert cm._projeto_pronto_para_publicar(estado("rendering", ["completed"])) is False

    def test_screening_nao_esta_pronto(self):
        assert cm._projeto_pronto_para_publicar(estado("screening", [])) is False

    def test_fallback_por_estado_das_variantes(self):
        assert cm._projeto_pronto_para_publicar(estado(None, ["completed", "on_demand"])) is True

    def test_fallback_com_variante_em_andamento_bloqueia(self):
        assert cm._projeto_pronto_para_publicar(estado(None, ["completed", "rendering"])) is False

    def test_geracao_concluida_sem_variantes(self):
        assert cm._projeto_pronto_para_publicar(estado("completed", [])) is True

    def test_sem_geracao_e_sem_variantes(self):
        assert cm._projeto_pronto_para_publicar(estado(None, [])) is False

    def test_entradas_invalidas(self):
        assert cm._projeto_pronto_para_publicar(None) is False
        assert cm._projeto_pronto_para_publicar({}) is False
        assert cm._projeto_pronto_para_publicar("texto") is False
