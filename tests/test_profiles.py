import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import App
import settings


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


@pytest.fixture
def store(tmp_path, monkeypatch):
    import keyring

    monkeypatch.setattr(settings, "DATA", tmp_path)
    monkeypatch.setattr(settings, "CONFIG", tmp_path / "settings.json")
    keys = {}
    monkeypatch.setattr(
        keyring, "set_password", lambda service, account, key: keys.update({account: key})
    )
    monkeypatch.setattr(keyring, "get_password", lambda service, account: keys.get(account))
    monkeypatch.setattr(
        keyring, "delete_password", lambda service, account: keys.pop(account, None)
    )
    return keys


def test_profiles_preserve_legacy_settings_and_isolate_keys_on_same_url(store):
    original = settings.Settings(model="first/model", deck="My deck")
    # Existing installations have no profile fields.
    legacy = asdict(original)
    legacy.pop("profile_id")
    settings.CONFIG.write_text(json.dumps(legacy), encoding="utf-8")
    store[settings.account(original)] = "first-secret"
    loaded, key = settings.load()
    assert loaded == original and key == "first-secret"
    profiles = settings.load_profiles(loaded)
    assert profiles[0]["name"] == "Основний"
    second = replace(original, profile_id="second", model="second/model")
    profiles.append(
        dict(id="second", name="Другий", **{k: getattr(second, k) for k in settings.PROFILE_FIELDS})
    )
    settings.save(second, "second-secret", True, profiles)
    assert settings.load() == (second, "second-secret")
    assert settings.get_key(original) == "first-secret"
    assert settings.get_key(second) == "second-secret"
    raw = settings.CONFIG.read_text(encoding="utf-8")
    assert "first-secret" not in raw and "second-secret" not in raw
    assert settings.load_profiles(second)[0]["model"] == "first/model"


def test_rename_does_not_change_credentials_and_forget_is_isolated(store):
    first = settings.Settings(profile_id="first", model="a")
    second = replace(first, profile_id="second", model="b")
    profiles = [
        dict(
            id=s.profile_id,
            name=s.profile_id,
            **{k: getattr(s, k) for k in settings.PROFILE_FIELDS},
        )
        for s in (first, second)
    ]
    settings.save(first, "first-secret", True, profiles)
    settings.save(second, "second-secret", True, profiles)
    profiles[1]["name"] = "Renamed"
    settings.save(second, "second-secret", True, profiles)
    assert settings.load()[1] == "second-secret"
    settings.save(second, "", False, profiles)
    assert settings.get_key(second) == ""
    assert settings.get_key(first) == "first-secret"


def view_for(s, profiles):
    view = SimpleNamespace(
        vars={k: Value(v) for k, v in asdict(s).items()},
        profiles=profiles,
        profile_keys={},
        profile_limits={},
        rate_limit=None,
        key=Value(""),
        remember=Value(True),
        profile_name=Value("Основний"),
        profile_box=Mock(),
        model_box=Mock(),
        rate_limit_summary=Value(""),
    )
    return view


def test_profile_switch_keeps_global_anki_settings_session_keys_and_limits(store):
    first = settings.Settings(model="a", deck="Existing deck", language="Ukrainian")
    second = replace(
        first,
        profile_id="second",
        provider="OpenAI-compatible",
        base_url="https://example.test/v1",
        model="b",
        max_tokens=4000,
    )
    profiles = [
        dict(
            id=s.profile_id,
            name=s.profile_id,
            **{k: getattr(s, k) for k in settings.PROFILE_FIELDS},
        )
        for s in (first, second)
    ]
    view = view_for(first, profiles)
    # Keys may be deliberately kept only in memory.
    view.profile_keys = {
        first.profile_id: "first-session-key",
        second.profile_id: "second-session-key",
    }
    view.rate_limit = {"scope": "Daily limit", "reset_at": None}
    App.apply_profile(view, profiles[1])
    assert view.key.get() == "second-session-key"
    assert view.vars["base_url"].get() == second.base_url
    assert view.vars["model"].get() == "b"
    assert view.vars["max_tokens"].get() == 4000
    assert view.vars["deck"].get() == "Existing deck"
    assert view.rate_limit is None
    App.apply_profile(view, profiles[0])
    assert view.key.get() == "first-session-key"
    assert view.rate_limit["scope"] == "Daily limit"
    assert view.model_box.configure.call_args.kwargs == {"values": []}


def test_switch_profile_autosaves_current_edits_and_restores_after_restart(store):
    first = settings.Settings(model="first-edited")
    second = replace(first, profile_id="second", model="second-model")
    profiles = [
        dict(
            id=s.profile_id,
            name=s.profile_id,
            **{k: getattr(s, k) for k in settings.PROFILE_FIELDS},
        )
        for s in (first, second)
    ]
    view = view_for(first, profiles)
    view.busy = False
    view.key.set("first-secret")
    view.profile_keys["second"] = "second-secret"
    view.profile_name.set("second")
    view.snapshot = lambda: App.snapshot(view)
    view.persist_profile = lambda: App.persist_profile(view)
    view.apply_profile = lambda p: App.apply_profile(view, p)
    view.status = Value("")
    view.write_log = Mock()
    App.switch_profile(view)
    assert settings.load() == (second, "second-secret")
    assert settings.get_key(first) == "first-secret"
    assert settings.load_profiles(second)[0]["model"] == "first-edited"


def test_new_profile_preserves_first_connection_and_starts_with_empty_key(store):
    first = settings.Settings(model="first-model")
    profiles = settings.load_profiles(first)
    view = view_for(first, profiles)
    view.busy = False
    view.key.set("first-secret")
    view.ask_profile_name = lambda: "Другий акаунт"
    view.snapshot = lambda: App.snapshot(view)
    view.persist_profile = lambda: App.persist_profile(view)
    view.apply_profile = lambda p: App.apply_profile(view, p)
    view.status = Value("")
    view.write_log = Mock()
    App.new_profile(view)
    assert len(view.profiles) == 2
    assert view.profile_name.get() == "Другий акаунт"
    assert view.key.get() == "" and view.vars["model"].get() == ""
    assert settings.get_key(first) == "first-secret"
    assert settings.load()[0].profile_id == view.vars["profile_id"].get()
    assert settings.load()[0].profile_id != first.profile_id
