"""Testes do alinhamento de legendas por WordBoundary (edge-tts)."""
from __future__ import annotations

import pytest

from backend.services import subtitle_timing as st

T = st.TICKS_PER_SECOND


def evento(texto: str, inicio: float, duracao: float) -> dict:
    """Monta um evento no formato que o edge-tts entrega em stream()."""
    return {
        "type": "WordBoundary",
        "offset": int(round(inicio * T)),
        "duration": int(round(duracao * T)),
        "text": texto,
    }


class TestTicksToSeconds:
    def test_um_segundo(self):
        assert st.ticks_to_seconds(T) == pytest.approx(1.0)

    def test_meio_segundo(self):
        assert st.ticks_to_seconds(T // 2) == pytest.approx(0.5)

    @pytest.mark.parametrize("invalido", [None, "abc", "", [], {}])
    def test_entrada_invalida_vira_zero(self, invalido):
        assert st.ticks_to_seconds(invalido) == 0.0

    def test_negativo_vira_zero(self):
        assert st.ticks_to_seconds(-100) == 0.0

    def test_nan_vira_zero(self):
        assert st.ticks_to_seconds(float("nan")) == 0.0


class TestWordsFromBoundaries:
    def test_extrai_palavras_com_tempo(self):
        palavras = st.words_from_boundaries(
            [evento("Olá", 0.0, 0.4), evento("mundo", 0.4, 0.6)]
        )
        assert [p.text for p in palavras] == ["Olá", "mundo"]
        assert palavras[0].start == pytest.approx(0.0)
        assert palavras[0].end == pytest.approx(0.4)
        assert palavras[1].end == pytest.approx(1.0)

    def test_texto_vazio_e_ignorado(self):
        assert st.words_from_boundaries([evento("", 0, 1), evento("  ", 1, 1)]) == []

    def test_ordena_eventos_fora_de_ordem(self):
        palavras = st.words_from_boundaries(
            [evento("segunda", 1.0, 0.5), evento("primeira", 0.0, 0.5)]
        )
        assert [p.text for p in palavras] == ["primeira", "segunda"]

    def test_evento_sem_type_tambem_e_aceito(self):
        palavras = st.words_from_boundaries(
            [{"offset": 0, "duration": T, "text": "oi"}]
        )
        assert len(palavras) == 1

    def test_type_diferente_e_ignorado(self):
        assert st.words_from_boundaries([{"type": "SentenceBoundary", "text": "oi"}]) == []

    def test_entradas_que_nao_sao_dicionario(self):
        assert st.words_from_boundaries(["lixo", 42, None]) == []

    def test_fallback_devolve_texto_inteiro(self):
        palavras = st.words_from_boundaries([], "texto inteiro do trecho")
        assert len(palavras) == 1
        assert palavras[0].text == "texto inteiro do trecho"
        assert palavras[0].start == 0.0

    def test_duracao_negativa_nao_inverte_o_fim(self):
        palavras = st.words_from_boundaries([evento("x", 1.0, -0.5)])
        assert palavras[0].end >= palavras[0].start


class TestGroupWordsIntoCues:
    def test_lista_vazia(self):
        assert st.group_words_into_cues([]) == []

    def test_respeita_max_chars(self):
        palavras = [st.Word("a" * 10, i * 0.5, i * 0.5 + 0.5) for i in range(4)]
        cues = st.group_words_into_cues(palavras, max_chars=15)
        assert len(cues) == 4
        assert all(len(c.text) <= 15 for c in cues)

    def test_agrupa_palavras_curtas(self):
        palavras = [st.Word("oi", i * 0.3, i * 0.3 + 0.3) for i in range(3)]
        cues = st.group_words_into_cues(palavras, max_chars=42)
        assert len(cues) == 1
        assert cues[0].text == "oi oi oi"

    def test_quebra_por_pontuacao_forte(self):
        palavras = [
            st.Word("Uma", 0.0, 0.3),
            st.Word("frase.", 0.3, 0.7),
            st.Word("Outra", 1.0, 1.3),
        ]
        cues = st.group_words_into_cues(palavras, max_chars=20)
        assert cues[0].text == "Uma frase."
        assert cues[1].text == "Outra"

    def test_pontuacao_nao_quebra_cue_curto(self):
        # "Fim." tem 4 chars; com max_chars=42 o limite de 40% (16) não foi
        # atingido, então a legenda continua em vez de quebrar cedo demais.
        palavras = [st.Word("Fim.", 0.0, 0.3), st.Word("depois", 0.4, 0.7)]
        cues = st.group_words_into_cues(palavras, max_chars=42)
        assert len(cues) == 1

    def test_quebra_por_silencio(self):
        palavras = [st.Word("antes", 0.0, 0.4), st.Word("depois", 3.0, 3.4)]
        cues = st.group_words_into_cues(palavras, max_gap=0.8)
        assert len(cues) == 2

    def test_quebra_por_duracao_maxima(self):
        palavras = [st.Word("p", i * 1.0, i * 1.0 + 1.0) for i in range(5)]
        cues = st.group_words_into_cues(palavras, max_chars=999, max_duration=2.5)
        assert len(cues) > 1
        assert all((c.end - c.start) <= 3.0 for c in cues)

    def test_cue_tem_duracao_minima(self):
        palavras = [st.Word("x", 5.0, 5.0)]  # duração zero
        cues = st.group_words_into_cues(palavras)
        assert cues[0].end - cues[0].start >= st.MIN_CUE_DURATION

    def test_nao_gera_cue_com_texto_vazio(self):
        cues = st.group_words_into_cues([st.Word("   ", 0, 1)])
        assert cues == []


class TestCuesFromWordsAnchored:
    def test_desloca_para_o_ancora(self):
        palavras = [st.Word("oi", 0.0, 0.5)]
        cues = st.cues_from_words_anchored(palavras, 10.0)
        assert cues[0].start == pytest.approx(10.0)
        assert cues[0].end == pytest.approx(10.5)

    def test_clamp_limita_ao_proximo_trecho(self):
        palavras = [st.Word("uma", 0.0, 0.4), st.Word("fala", 0.4, 2.0)]
        cues = st.cues_from_words_anchored(palavras, 0.0, clamp_to=1.0)
        assert cues
        assert all(c.end <= 1.0 for c in cues)

    def test_clamp_antes_do_inicio_nao_apaga_tudo(self):
        palavras = [st.Word("oi", 0.0, 0.5)]
        cues = st.cues_from_words_anchored(palavras, 5.0, clamp_to=4.0)
        # limite antes do início: devolve o cue original em vez de lista vazia
        assert len(cues) == 1

    def test_ancora_negativa_vira_zero(self):
        cues = st.cues_from_words_anchored([st.Word("oi", 0.0, 0.5)], -3.0)
        assert cues[0].start == pytest.approx(0.0)

    def test_lista_vazia(self):
        assert st.cues_from_words_anchored([], 1.0) == []


class TestFormatTimestamp:
    @pytest.mark.parametrize(
        "segundos,esperado",
        [
            (0, "00:00:00,000"),
            (1.5, "00:00:01,500"),
            (59.999, "00:00:59,999"),
            (60, "00:01:00,000"),
            (3599.999, "00:59:59,999"),
            (3661.25, "01:01:01,250"),
            (360000, "100:00:00,000"),
        ],
    )
    def test_formatos(self, segundos, esperado):
        assert st.format_timestamp(segundos) == esperado

    @pytest.mark.parametrize("invalido", [-10, None, "abc", float("nan")])
    def test_invalidos_viram_zero(self, invalido):
        assert st.format_timestamp(invalido) == "00:00:00,000"


class TestBuildSrt:
    def test_duas_legendas(self):
        srt = st.build_srt(
            [st.Cue(0.0, 1.0, "Olá"), st.Cue(1.2, 2.5, "mundo")]
        )
        assert srt.startswith("1\n00:00:00,000 --> 00:00:01,000\nOlá\n")
        assert "2\n00:00:01,200 --> 00:00:02,500\nmundo" in srt
        assert srt.endswith("\n")

    def test_maiusculas(self):
        srt = st.build_srt([st.Cue(0, 1, "olá")], uppercase=True)
        assert "OLÁ" in srt

    def test_sem_cues_retorna_vazio(self):
        assert st.build_srt([]) == ""

    def test_ignora_cue_vazio(self):
        assert st.build_srt([st.Cue(0, 1, "   ")]) == ""

    def test_ignora_cue_com_tempo_invertido(self):
        assert st.build_srt([st.Cue(2, 1, "x")]) == ""

    def test_numeracao_sequencial_apos_ignorados(self):
        srt = st.build_srt(
            [st.Cue(2, 1, "lixo"), st.Cue(0, 1, "bom"), st.Cue(1, 2, "outro")]
        )
        assert srt.startswith("1\n00:00:00,000")


class TestSidecar:
    def test_ida_e_volta(self, tmp_path):
        audio = tmp_path / "fala_000.mp3"
        original = [st.Word("Olá", 0.1, 0.6), st.Word("mundo", 0.6, 1.0)]
        destino = st.write_words_sidecar(audio, original)
        assert destino is not None and destino.name == "fala_000.words.json"

        lidas = st.read_words_sidecar(audio)
        assert [p.text for p in lidas] == ["Olá", "mundo"]
        assert lidas[0].start == pytest.approx(0.1, abs=1e-6)
        assert lidas[1].end == pytest.approx(1.0, abs=1e-6)

    def test_lista_vazia_nao_escreve(self, tmp_path):
        assert st.write_words_sidecar(tmp_path / "a.mp3", []) is None

    def test_ausente_retorna_vazio(self, tmp_path):
        assert st.read_words_sidecar(tmp_path / "nao_existe.mp3") == []

    def test_json_corrompido_retorna_vazio(self, tmp_path):
        audio = tmp_path / "a.mp3"
        st.sidecar_path(audio).write_text("{nao eh json", encoding="utf-8")
        assert st.read_words_sidecar(audio) == []

    def test_payload_nao_lista_retorna_vazio(self):
        assert st.words_from_payload({"a": 1}) == []


class TestCuesFromSegmentTimes:
    def test_usa_tempos_do_segmento(self):
        cues = st.cues_from_segment_times([{"start": 1.0, "end": 2.0, "trans": "oi"}])
        assert cues[0].start == 1.0 and cues[0].end == 2.0 and cues[0].text == "oi"

    def test_quebra_texto_longo(self):
        texto = " ".join(f"palavra{i}" for i in range(20))
        cues = st.cues_from_segment_times(
            [{"start": 0.0, "end": 10.0, "trans": texto}], max_chars=20
        )
        assert len(cues) > 1
        assert all(len(c.text) <= 20 for c in cues)
        assert cues[-1].end <= 10.0 + 1e-9

    def test_aceita_campo_text(self):
        cues = st.cues_from_segment_times([{"start": 0, "end": 1, "text": "olá"}])
        assert cues[0].text == "olá"

    def test_ignora_segmento_sem_texto(self):
        assert st.cues_from_segment_times([{"start": 0, "end": 1, "trans": ""}]) == []

    def test_ignora_tempos_invalidos(self):
        assert st.cues_from_segment_times([{"start": "x", "end": "y", "trans": "oi"}]) == []

    def test_uppercase(self):
        cues = st.cues_from_segment_times(
            [{"start": 0, "end": 1, "trans": "oi"}], uppercase=True
        )
        assert cues[0].text == "OI"


class TestParseFfmpegDuration:
    def test_extrai_duracao(self):
        stderr = "  Duration: 00:01:23.45, start: 0.0, bitrate: 1000 kb/s"
        assert st.parse_ffmpeg_duration(stderr) == pytest.approx(83.45)

    def test_sem_duracao(self):
        assert st.parse_ffmpeg_duration("entrada invalida") == 0.0

    def test_vazio(self):
        assert st.parse_ffmpeg_duration("") == 0.0
