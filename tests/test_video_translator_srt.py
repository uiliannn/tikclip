"""Testes da geração de SRT dentro do pipeline de dublagem."""
from __future__ import annotations


import video_translator as vt
from backend.services import subtitle_timing as st


class TestSubtitleTimingImport:
    def test_modulo_disponivel(self):
        assert vt._subtitle_timing() is st


class TestBuildSrtFallback:
    def test_usa_tempos_do_whisper(self):
        srt = vt._build_srt([{"start": 1.0, "end": 2.0, "trans": "Olá"}])
        assert "00:00:01,000 --> 00:00:02,000" in srt
        assert "Olá" in srt

    def test_maiusculas(self):
        srt = vt._build_srt([{"start": 0, "end": 1, "trans": "olá"}], uppercase=True)
        assert "OLÁ" in srt

    def test_segmento_sem_texto_e_ignorado(self):
        srt = vt._build_srt(
            [{"start": 0, "end": 1, "trans": ""}, {"start": 1, "end": 2, "trans": "bom"}]
        )
        assert "bom" in srt
        assert srt.startswith("1\n")  # numeração não deixa buraco

    def test_sem_segmentos(self):
        assert vt._build_srt([]) == ""

    def test_caminho_sem_dependencia(self, monkeypatch):
        """Se o módulo novo sumir, ainda precisa sair um SRT válido."""
        monkeypatch.setattr(vt, "_subtitle_timing", lambda: None)
        srt = vt._build_srt([{"start": 0.0, "end": 1.5, "trans": "teste"}])
        assert "00:00:00,000 --> 00:00:01,500" in srt
        assert "teste" in srt


class TestBuildSrtWithSidecar:
    def test_ancora_no_inicio_do_trecho(self, tmp_path):
        audio = tmp_path / "fala_000.mp3"
        st.write_words_sidecar(
            audio, [st.Word("Olá", 0.0, 0.5), st.Word("mundo", 0.5, 1.0)]
        )
        segmentos = [
            {"start": 5.0, "end": 6.0, "trans": "Olá mundo", "audio_file": str(audio)}
        ]
        srt = vt._build_srt(segmentos)
        assert "00:00:05,000" in srt
        assert "00:00:06,000" in srt
        assert "Olá mundo" in srt

    def test_nao_invade_o_proximo_trecho(self, tmp_path):
        audio = tmp_path / "fala_000.mp3"
        # fala de 5s ancorada em 0, mas o próximo trecho começa em 1.5s
        st.write_words_sidecar(audio, [st.Word("longa", 0.0, 5.0)])
        segmentos = [
            {"start": 0.0, "end": 1.5, "trans": "longa", "audio_file": str(audio)},
            {"start": 1.5, "end": 2.0, "trans": "curta"},
        ]
        srt = vt._build_srt(segmentos)
        # a legenda é cortada no início do próximo trecho, não esticada até 5s
        assert "00:00:00,000 --> 00:00:01,500" in srt
        assert "00:00:05,000" not in srt

    def test_sidecar_ilegivel_cai_no_fallback(self, tmp_path):
        audio = tmp_path / "fala_000.mp3"
        segmentos = [
            {"start": 2.0, "end": 3.0, "trans": "fallback", "audio_file": str(audio)}
        ]
        srt = vt._build_srt(segmentos)
        assert "00:00:02,000 --> 00:00:03,000" in srt

    def test_trecho_sem_audio_file(self):
        srt = vt._build_srt([{"start": 0.0, "end": 1.0, "trans": "sem audio"}])
        assert "sem audio" in srt

    def test_fim_invertido_recebe_duracao_minima(self):
        srt = vt._build_srt([{"start": 4.0, "end": 4.0, "trans": "iguais"}])
        assert "00:00:04,000 -->" in srt
        assert "00:00:04,200" in srt
