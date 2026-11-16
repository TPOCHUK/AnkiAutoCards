from types import SimpleNamespace

from app import App


class Widget:
    def __init__(self):
        self.generated = []

    def event_generate(self, event):
        self.generated.append(event)


def test_clipboard_shortcuts_use_windows_physical_keycodes():
    expected = {
        65: "<<SelectAll>>",
        67: "<<Copy>>",
        88: "<<Cut>>",
        86: "<<Paste>>",
        90: "<<Undo>>",
    }

    for keycode, virtual_event in expected.items():
        widget = Widget()
        event = SimpleNamespace(keysym="Cyrillic_letter", keycode=keycode, widget=widget)

        assert App.clipboard_shortcut(event) == "break"
        assert widget.generated == [virtual_event]


def test_clipboard_shortcuts_still_accept_latin_keysyms():
    widget = Widget()
    event = SimpleNamespace(keysym="v", keycode=0, widget=widget)

    assert App.clipboard_shortcut(event) == "break"
    assert widget.generated == ["<<Paste>>"]


def test_other_control_shortcuts_are_not_intercepted():
    widget = Widget()
    event = SimpleNamespace(keysym="q", keycode=81, widget=widget)

    assert App.clipboard_shortcut(event) is None
    assert widget.generated == []
