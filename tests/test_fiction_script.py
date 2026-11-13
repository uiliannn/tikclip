"""Roteiro de ficção: formato, limites e recusas (clone de voz, "caso real")."""
from __future__ import annotations

import pytest

from backend.services.fiction import script
from tests.fiction_helpers import fake_call, script_payload, script_payload_with


class TestParsing:
    def test_valid_story_is_accepted(self):
        story = script.parse_script(script_payload())
        assert len(story.scenes) == 6
        assert 80 <= story.word_count <= 100
        assert {line.tone for line in story.all_lines} == {'narrador', 'personagem', 'sussurro'}

    def test_too_few_words_is_refused(self):
        with pytest.raises(script.ScriptError, match='palavras'):
            script.parse_script(script_payload_with('Oi.'))

    def test_fiction_cannot_pose_as_a_real_case(self):
        text = 'Isto foi um caso real que aconteceu comigo ontem à noite.'
        with pytest.raises(script.ScriptError, match='caso real'):
            script.parse_script(script_payload_with(text))

    def test_missing_scenes_is_refused(self):
        payload = script_payload()
        payload['scenes'] = payload['scenes'][:3]
        with pytest.raises(script.ScriptError, match='formato|cenas'):
            script.parse_script(payload)

    def test_unknown_tone_is_refused(self):
        payload = script_payload()
        payload['scenes'][0]['lines'][0]['tone'] = 'gritando'
        with pytest.raises(script.ScriptError, match='formato'):
            script.parse_script(payload)

    def test_text_only_payload_is_refused(self):
        with pytest.raises(script.ScriptError, match='JSON'):
            script.parse_script('{"title": "x"}')


class TestVoiceCloning:
    def test_clone_request_is_refused_with_explanation(self):
        with pytest.raises(script.ScriptError, match='Não clono a voz'):
            script.reject_voice_cloning({'theme': 'x', 'clone_voice': 'voz.wav'})

    def test_reference_audio_is_refused(self):
        with pytest.raises(script.ScriptError, match='sintéticas'):
            script.reject_voice_cloning({'reference_audio': 'amostra.mp3'})

    def test_empty_fields_do_not_trigger_the_refusal(self):
        script.reject_voice_cloning({'theme': 'x', 'clone_voice': None})
        script.reject_voice_cloning({'theme': 'x', 'clone_voice': ''})
        script.reject_voice_cloning({'theme': 'x', 'clone_voice': False})
        script.reject_voice_cloning(None)

    def test_refusal_message_names_the_alternative(self):
        assert 'narrador' in script.CLONE_REFUSAL
        assert 'consentimento' in script.CLONE_REFUSAL


class TestGeneration:
    def test_missing_theme_is_refused(self):
        with pytest.raises(script.ScriptError, match='tema'):
            script.generate_script('   ')

    def test_retries_once_for_the_word_limit(self):
        calls = []

        def call(prompt, data):
            calls.append(prompt)
            if len(calls) == 1:
                return script_payload_with('Oi.')
            return script_payload()

        story = script.generate_script('estrada', call=call)
        assert story.word_count == 83
        assert len(calls) == 2
        assert 'palavras' in calls[1]  # a segunda tentativa avisa o limite

    def test_does_not_retry_when_the_model_ignores_the_rules(self):
        calls = []

        def call(prompt, data):
            calls.append(prompt)
            return script_payload_with('Isto foi um caso real que aconteceu comigo.')

        with pytest.raises(script.ScriptError, match='caso real'):
            script.generate_script('estrada', call=call)
        assert len(calls) == 1

    def test_gives_up_with_the_last_error(self):
        with pytest.raises(script.ScriptError, match='palavras'):
            script.generate_script('estrada', call=fake_call(script_payload_with('Oi.')),
                                   attempts=2)
