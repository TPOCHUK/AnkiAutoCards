import json
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from anki_core import AnkiConnect, WordInfo, highlight, read_words
from settings import Settings, import_env
import settings
from workflow import build_graph, make_chain, error_message, list_models

RAW = {
    "lemma": "thrive",
    "form": "thrive",
    "translation": "процвітати",
    "sentence": "Small plants thrive beside the sunny window in my room.",
}
INFO = WordInfo(**RAW)


class Anki:
    def __init__(self, existing=()):
        self.existing = set(existing)
        self.added = []
        self.media = []

    def word_exists(self, deck, word):
        return word in self.existing

    def store_media(self, filename, data):
        self.media.append((filename, data))
        return filename

    def add_note(self, deck, fields, tags):
        self.added.append((deck, fields, tags))
        self.existing.add(fields["Word"])
        return 123


def test_preview_then_add_does_not_call_llm_again():
    api = Anki()
    chain = Mock()
    chain.invoke.return_value = json.dumps(RAW)
    workflow = build_graph(Settings(), "fake", api, chain=chain, tts=lambda t: b"audio")
    preview = workflow.invoke({"word": "thrive", "mode": "preview", "check_anki": False})
    assert preview["status"] == "ready"
    assert api.media == api.added == []
    added = workflow.invoke({"word": "thrive", "info": preview["info"], "mode": "add"})
    assert added["status"] == "added"
    assert chain.invoke.call_count == 1
    assert len(api.media) == 2
    fields = api.added[0][1]
    assert "<b>thrive</b>" in fields["Sentence"]
    assert fields["Audio"].count("[sound:") == 2
    again = workflow.invoke({"word": "thrive", "mode": "add"})
    assert again["status"] == "duplicate"
    assert chain.invoke.call_count == 1


def test_lemma_duplicate_does_not_make_audio():
    chain = Mock()
    chain.invoke.return_value = json.dumps(RAW)
    api = Anki(["thrive"])
    workflow = build_graph(
        Settings(), "fake", api, chain=chain, tts=Mock(side_effect=AssertionError)
    )
    assert workflow.invoke({"word": "thriving", "mode": "add"})["status"] == "duplicate"
    assert not api.added


def test_validation_retry_and_exhaustion():
    chain = Mock()
    chain.invoke.side_effect = ["not JSON", json.dumps(RAW)]
    workflow = build_graph(Settings(), "fake", None, chain=chain)
    state = workflow.invoke({"word": "thrive", "mode": "preview", "check_anki": False})
    assert state["status"] == "ready" and chain.invoke.call_count == 2
    chain.invoke.side_effect = ["[]", "{}"]
    state = workflow.invoke({"word": "thrive", "mode": "preview", "check_anki": False})
    assert state["status"] == "invalid" and state["attempts"] == 2


def test_phrase_normalization_and_missing_expression_retry():
    chain = Mock()
    chain.invoke.side_effect = [
        json.dumps(
            {"translation": "Я уважно слухаю", "sentence": "Tell me what happened today, please."}
        ),
        json.dumps(
            {
                "translation": "Я уважно слухаю",
                "sentence": "I'm all ears, tell me about your amazing trip.",
            }
        ),
    ]
    workflow = build_graph(Settings(), "", None, chain=chain)
    result = workflow.invoke({"word": "I’m all ears", "mode": "preview", "check_anki": False})
    assert result["info"].lemma == "I'm all ears"
    assert result["status"] == "ready" and result["attempts"] == 2


def test_tts_failure_does_not_add_note_or_upload_partial_audio():
    tts = Mock(side_effect=[b"audio", RuntimeError("TTS failed")])
    api = Anki()
    result = build_graph(Settings(), "", api, tts=tts).invoke(
        {"word": "thrive", "info": INFO, "mode": "add"}
    )
    assert result["status"] == "failed"
    assert not api.added and not api.media


def test_no_audio_and_cancellation():
    api = Anki()
    s = Settings(audio=False)
    result = build_graph(s, "", api, tts=Mock(side_effect=AssertionError)).invoke(
        {"word": "thrive", "info": INFO, "mode": "add"}
    )
    assert result["status"] == "added" and api.added[0][1]["Audio"] == ""
    api = Anki()
    result = build_graph(s, "", api, stopped=lambda: True).invoke(
        {"word": "thrive", "info": INFO, "mode": "add"}
    )
    assert result["status"] == "cancelled" and not api.added


def test_legacy_env_and_credentials_not_in_json(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        'OPENAI_API_KEY="private-key"\nOPENAI_API_MODEL=custom/model\nOPENAI_API_BASE=https://openrouter.ai/api/v1\nANKI_MODEL=My Cards\nANKI_MAX_TOKENS=4000\n',
        encoding="utf-8",
    )
    s, key = import_env(env)
    assert (s.model, s.note_type, s.max_tokens, key) == (
        "custom/model",
        "My Cards",
        4000,
        "private-key",
    )
    monkeypatch.setattr(settings, "DATA", tmp_path)
    monkeypatch.setattr(settings, "CONFIG", tmp_path / "settings.json")
    import keyring

    secret = {}
    monkeypatch.setattr(
        keyring, "set_password", lambda service, account, value: secret.update(value=value)
    )
    monkeypatch.setattr(keyring, "get_password", lambda *args: secret.get("value"))
    settings.save(s, key, True)
    assert secret["value"] == key
    assert key not in (tmp_path / "settings.json").read_text()
    assert env.read_text().startswith('OPENAI_API_KEY="private-key"')


def test_credentials_redacted_and_rate_limit():
    assert "private-key" not in error_message(RuntimeError("bad private-key"), "private-key")
    exc = RuntimeError("secret")
    exc.status_code = 429
    assert "Ліміт" in error_message(exc)


def test_failed_key_deletion_is_reported(tmp_path, monkeypatch):
    import keyring

    monkeypatch.setattr(settings, "DATA", tmp_path)
    monkeypatch.setattr(settings, "CONFIG", tmp_path / "settings.json")
    monkeypatch.setattr(keyring, "get_password", lambda *args: "saved-key")
    monkeypatch.setattr(keyring, "delete_password", Mock(side_effect=RuntimeError("unavailable")))
    message = settings.save(Settings(), "", False)
    assert "Не вдалося видалити" in message


def test_actual_langchain_chain_against_local_openai_protocol_server(monkeypatch):
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            response = {
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "usage": {"prompt_tokens": 111, "completion_tokens": 44, "total_tokens": 155},
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": json.dumps(RAW)},
                        "finish_reason": "stop",
                    }
                ],
            }
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        s = Settings(base_url=f"http://127.0.0.1:{server.server_port}/v1", model="test-model")
        result = build_graph(s, "fake-local-key", None).invoke(
            {"word": "thrive", "mode": "preview", "check_anki": False}
        )
        assert result["status"] == "ready", result
        assert requests[0]["model"] == "test-model"
        assert len(requests[0]["messages"]) == 2
        assert result["usage"]["total_tokens"] == 155
        assert result["usage_requests"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_free_models_filter_and_manual_provider_clients(monkeypatch):
    response = Mock()
    response.json.return_value = {
        "data": [
            {"id": "paid", "pricing": {"prompt": "1", "completion": "1"}},
            {"id": "free", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "unknown"},
        ]
    }
    monkeypatch.setattr("workflow.requests.get", lambda *args, **kw: response)
    assert list_models(Settings(), "", True) == ["free"]
    assert (
        make_chain(
            Settings(
                provider="Anthropic", base_url="https://api.anthropic.com", model="manual-model"
            ),
            "fake",
        )
        is not None
    )


def test_existing_note_type_is_not_overwritten():
    api = AnkiConnect(model="My Cards")
    api.invoke = Mock(side_effect=[["My Cards"], ["Word", "Sentence", "Meaning", "Audio", "Extra"]])
    assert "Extra" in api.ensure_model()
    assert [c.args[0] for c in api.invoke.call_args_list] == ["modelNames", "modelFieldNames"]


def test_text_input_and_html_safety():
    assert read_words(["thrive", "THRIVE", "I’m all ears # phrase", "", "#comment"]) == [
        "thrive",
        "I'm all ears",
    ]
    assert "&lt;script&gt;" in highlight("<script>thrive</script>", "thrive")


def test_usage_includes_invalid_response_and_retry():
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    chain = Mock()
    usage = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
    replies = iter(["bad JSON", json.dumps(RAW)])

    def invoke(data, config):
        text = next(replies)
        message = AIMessage(
            content=text, usage_metadata=usage, response_metadata={"model_name": "test-model"}
        )
        response = LLMResult(generations=[[ChatGeneration(message=message)]])
        config["callbacks"][0].on_llm_end(response)
        return text

    chain.invoke.side_effect = invoke
    state = build_graph(Settings(), "", None, chain=chain).invoke(
        {"word": "thrive", "check_anki": False}
    )
    assert state["status"] == "ready"
    assert state["usage"] == {"input_tokens": 200, "output_tokens": 40, "total_tokens": 240}
    assert state["usage_requests"] == state["attempts"] == 2


def test_missing_usage_is_not_reported_as_measured_zero():
    chain = Mock()
    chain.invoke.return_value = json.dumps(RAW)
    state = build_graph(Settings(), "", None, chain=chain).invoke(
        {"word": "thrive", "check_anki": False}
    )
    assert state["status"] == "ready"
    assert state["usage_requests"] == 0 and state["attempts"] == 1


def test_inflected_phrase_and_preferred_translation():
    chain = Mock()
    chain.invoke.return_value = json.dumps(
        {
            "form": "was supposed to",
            "translation": "wrong meaning",
            "sentence": "I was supposed to call you before leaving the office.",
        }
    )
    state = build_graph(Settings(), "", None, chain=chain).invoke(
        {"word": "be supposed to — мати щось зробити", "check_anki": False}
    )
    assert state["status"] == "ready"
    assert state["info"].lemma == "be supposed to"
    assert state["info"].form == "be supposed to"
    assert state["info"].translation == "мати щось зробити"


def test_stop_during_rate_limit_does_not_call_model(monkeypatch):
    from workflow import LLMRateLimiter

    chain = Mock()
    limiter = LLMRateLimiter()
    limiter.requests = 5
    monkeypatch.setattr("workflow.LLMRateLimiter", lambda: limiter)
    stopped = False

    def notify(message):
        nonlocal stopped
        if "Ліміт LLM" in message:
            stopped = True

    workflow = build_graph(
        Settings(), "", None, chain=chain, notify=notify, stopped=lambda: stopped
    )
    state = workflow.invoke({"word": "thrive", "check_anki": False})
    assert state["status"] == "cancelled"
    chain.invoke.assert_not_called()


def test_stop_after_word_audio_does_not_upload_or_add():
    api = Anki()
    stopped = False

    def tts(text):
        nonlocal stopped
        stopped = True
        return b"audio"

    state = build_graph(Settings(), "", api, tts=tts, stopped=lambda: stopped).invoke(
        {"word": "thrive", "info": INFO, "mode": "add"}
    )
    assert state["status"] == "cancelled"
    assert not api.media and not api.added


def test_invalid_preview_is_not_written_to_anki():
    api = Anki()
    invalid = WordInfo("thrive", "thrive", "процвітати", "A sentence about something else.")
    state = build_graph(Settings(audio=False), "", api).invoke(
        {"word": "thrive", "info": invalid, "mode": "add"}
    )
    assert state["status"] == "failed"
    assert not api.added
