import json
import queue
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
from openai import RateLimitError
import pytest

from app import App
from rate_limits import format_rate_limit, rate_limit_info
from settings import Settings
from workflow import build_graph


NOW = 1_800_000_000


def limited(headers=None, body=None):
    response = httpx.Response(
        429, headers=headers or {}, request=httpx.Request("POST", "https://example.test")
    )
    return RateLimitError("rate limited", response=response, body=body or {})


@pytest.mark.parametrize(
    "headers,delay",
    [
        ({"Retry-After": "65"}, 65),
        ({"Retry-After": "Sun, 15 Jan 2027 08:01:00 GMT"}, None),
        ({"X-RateLimit-Reset": str(NOW + 90)}, 90),
        ({"X-RateLimit-Reset": str((NOW + 90) * 1000)}, 90),
        ({"Retry-After": "10", "X-RateLimit-Reset": str(NOW + 90)}, 90),
    ],
)
def test_reset_headers(headers, delay):
    info = rate_limit_info(limited(headers), now=NOW)
    if delay is None:
        from email.utils import parsedate_to_datetime

        expected = parsedate_to_datetime(headers["Retry-After"]).timestamp()
    else:
        expected = NOW + delay
    assert info["reset_at"] == expected


def test_openrouter_metadata_headers_and_scope():
    body = {
        "error": {
            "message": "Rate limit exceeded: free-models-per-day",
            "metadata": {
                "headers": {
                    "X-RateLimit-Reset": str((NOW + 3600) * 1000),
                    "X-RateLimit-Limit": "50",
                    "X-RateLimit-Remaining": "0",
                }
            },
        }
    }
    info = rate_limit_info(limited(body=body), now=NOW)
    assert info == {
        "scope": "Денна квота безкоштовних моделей",
        "reset_at": NOW + 3600,
        "limit": 50,
        "remaining": 0,
    }
    text = format_rate_limit(info, now=NOW)
    assert "01:00:00" in text and "UTC" in text
    assert "Ліміт: 50" in text and "Залишилось: 0" in text
    assert "можна повторити" in format_rate_limit(info, now=NOW + 3600)


@pytest.mark.parametrize("value", ["bad", "NaN", "Infinity", "1e100", "-100"])
def test_invalid_reset_does_not_invent_deadline(value):
    info = rate_limit_info(limited({"X-RateLimit-Reset": value}), now=NOW)
    assert info["reset_at"] is None
    assert "API не повідомив час" in format_rate_limit(info, now=NOW)


def test_other_errors_are_not_rate_limits():
    assert rate_limit_info(ValueError("invalid JSON")) is None


def test_workflow_delivers_rate_limit_to_ui_without_retrying():
    chain = Mock()
    chain.invoke.side_effect = limited({"Retry-After": "120"})
    state = build_graph(Settings(), "secret-key", None, chain=chain).invoke(
        {"word": "thrive", "check_anki": False}
    )
    assert state["status"] == "failed"
    assert state["rate_limit"]["reset_at"] is not None
    assert chain.invoke.call_count == 1
    view = SimpleNamespace(events=queue.Queue())
    App.publish_usage(view, state, Settings(model="test-model"), "Перегляд")
    kind, value = view.events.get_nowait()
    assert kind == "rate_limit"
    assert value["model"] == "test-model"
    assert "secret-key" not in json.dumps(value)


def test_countdown_updates_without_network_requests(monkeypatch):
    monkeypatch.setattr("rate_limits.time.time", lambda: NOW)
    view = SimpleNamespace(
        rate_limit={
            "provider": "OpenRouter",
            "model": "test-model",
            "scope": "Ліміт запитів API (429)",
            "reset_at": NOW + 65,
        },
        rate_limit_summary=Mock(),
        after=Mock(),
        refresh_rate_limit=Mock(),
    )
    App.refresh_rate_limit(view)
    assert "00:01:05" in view.rate_limit_summary.set.call_args.args[0]
    view.after.assert_called_once_with(1000, view.refresh_rate_limit)


def test_preview_stops_batch_after_first_rate_limit(monkeypatch):
    graph = Mock()
    graph.invoke.return_value = {
        "word": "thrive",
        "status": "failed",
        "rate_limit": {"reset_at": NOW + 60},
    }
    monkeypatch.setattr("app.build_graph", lambda *args, **kwargs: graph)
    monkeypatch.setattr("app.AnkiConnect", Mock())
    view = SimpleNamespace(
        snapshot=lambda *args: (Settings(), "fake"),
        words=Mock(),
        events=queue.Queue(),
        cancel=Mock(),
        publish_usage=Mock(),
        start_job=lambda work: work(),
    )
    view.words.get.return_value = "thrive\ncome across\nmanage to"
    view.cancel.is_set.return_value = False
    App.preview(view)
    assert graph.invoke.call_count == 1
    assert any("Решта слів не оброблялася" in str(event) for event in view.events.queue)
