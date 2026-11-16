"""Public preferences in JSON; credentials in the OS credential store."""

import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from urllib.parse import urlsplit

ROOT = (
    Path(sys.executable).parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
DATA = Path(os.getenv("APPDATA") or Path.home() / ".config") / "AnkiAutoCards"
CONFIG = DATA / "settings.json"


@dataclass
class Settings:
    provider: str = "OpenRouter"
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = ""
    deck: str = "English"
    note_type: str = "AutoVocab"
    anki_url: str = "http://127.0.0.1:8765"
    voice: str = "en-US-AriaNeural"
    language: str = "Ukrainian"
    max_tokens: int = 2000
    audio: bool = True

    def validate(self, key="", require_model=True):
        if require_model and (not self.model.strip() or not key.strip()):
            raise ValueError("Вкажи API-ключ і модель у налаштуваннях.")
        if not self.deck.strip() or not self.note_type.strip():
            raise ValueError("Вкажи колоду і тип нотатки.")
        if not 128 <= self.max_tokens <= 32000:
            raise ValueError("Ліміт токенів має бути від 128 до 32000.")
        for url in (self.base_url, self.anki_url):
            part = urlsplit(url)
            if (
                part.scheme not in ("http", "https")
                or not part.hostname
                or part.username
                or part.password
            ):
                raise ValueError("Потрібна коректна HTTP(S) адреса без ключів і паролів.")


def import_env(path):
    from dotenv import dotenv_values

    e = dotenv_values(path)

    # Read the file without changing the process environment.
    def get(*keys, default=""):
        return next((e[k] for k in keys if e.get(k)), default)

    anthropic = bool(get("ANTHROPIC_API_KEY") and not get("OPENAI_API_KEY", "OPENROUTER_API_KEY"))
    base = get(
        "OPENAI_API_BASE",
        "OPENROUTER_BASE_URL",
        "OPENAI_BASE_URL",
        default=(
            "https://api.openai.com/v1" if get("OPENAI_API_KEY") else "https://openrouter.ai/api/v1"
        ),
    )
    s = Settings(
        provider="Anthropic"
        if anthropic
        else ("OpenRouter" if "openrouter.ai" in base else "OpenAI-compatible"),
        base_url="https://api.anthropic.com" if anthropic else base,
        model=get(
            "ANKI_LLM_MODEL" if anthropic else "OPENAI_API_MODEL", "OPENAI_MODEL", default=""
        ),
        deck=get("ANKI_DECK", default="English"),
        note_type=get("ANKI_MODEL", default="AutoVocab"),
        anki_url=get("ANKI_URL", default="http://127.0.0.1:8765"),
        voice=get("ANKI_TTS_VOICE", default="en-US-AriaNeural"),
        language=get("ANKI_TARGET_LANG", default="Ukrainian"),
        max_tokens=int(get("ANKI_MAX_TOKENS", default="2000")),
    )
    key = get("ANTHROPIC_API_KEY") if anthropic else get("OPENAI_API_KEY", "OPENROUTER_API_KEY")
    return s, key


def load():
    if CONFIG.exists():
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        allowed = {f.name for f in fields(Settings)}
        s = Settings(**{k: v for k, v in data.items() if k in allowed})
        return s, get_key(s)
    if (ROOT / ".env").exists():
        return import_env(ROOT / ".env")
    return Settings(), ""


def account(s):
    return s.provider + "|" + s.base_url.rstrip("/")


def get_key(s):
    try:
        import keyring

        return keyring.get_password("AnkiAutoCards", account(s)) or ""
    except Exception:
        return ""


def save(s, key, remember):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(s), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG)
    try:
        import keyring

        if remember and key:
            keyring.set_password("AnkiAutoCards", account(s), key)
        elif keyring.get_password("AnkiAutoCards", account(s)):
            keyring.delete_password("AnkiAutoCards", account(s))
    except Exception:
        if remember:
            return "Налаштування збережено. Сховище ключів недоступне: ключ діє лише до закриття програми."
        return "Налаштування збережено. Не вдалося видалити ключ зі сховища ОС."
    return "Налаштування збережено."
