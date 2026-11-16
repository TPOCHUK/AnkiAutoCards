"""Check display totals with fixed data. No model calls or GUI windows."""

from types import SimpleNamespace
from unittest.mock import Mock

from app import App


def test_card_total_includes_retries_and_marks_partial_usage():
    state = {"attempts": 2, "usage_requests": 2, "usage": {"total_tokens": 420}}
    assert App.card_tokens(state) == "420"
    state["usage_requests"] = 1
    assert App.card_tokens(state) == "420+"
    state["usage_requests"] = 0
    assert App.card_tokens(state) == "Невідомо"
    state["attempts"] = 0
    assert App.card_tokens(state) == "0"


def test_session_summary_keeps_failed_and_unknown_attempts():
    view = SimpleNamespace(token_records=[], token_tree=Mock(), token_summary=Mock())
    record = {
        "source": "Перегляд",
        "word": "thrive",
        "model": "test-model",
        "attempt": 1,
        "status": "invalid",
        "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    }
    App.show_usage(view, record)
    App.show_usage(view, dict(record, attempt=2, status="valid", usage=None))
    message = view.token_summary.set.call_args.args[0]
    assert "разом 120" in message
    assert "Спроб: 2" in message
    assert "Без статистики: 1" in message
    assert "Сума неповна" in message
    assert view.token_tree.insert.call_count == 2


def test_clear_token_history_removes_rows_and_resets_summary():
    tree = Mock()
    tree.get_children.return_value = ("first", "second")
    view = SimpleNamespace(
        token_records=[{"usage": None}], token_tree=tree, token_summary=Mock()
    )

    App.clear_token_history(view)

    assert view.token_records == []
    tree.delete.assert_called_once_with("first", "second")
    view.token_summary.set.assert_called_once_with("Ще немає запитів до моделі.")
