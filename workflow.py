"""Generate a card, check it, then send it to Anki."""

from __future__ import annotations
import html
import time
import re
from typing import TypedDict
import requests
from rate_limits import rate_limit_info
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langgraph.graph import StateGraph, START, END
from anki_core import (
    AnkiError,
    WordInfo,
    build_prompt,
    parse_llm_json,
    make_info,
    normalize_text,
    is_phrase,
    parse_word_input,
    highlight,
    safe_filename,
    edge_tts_synthesize,
)


class LLMRateLimiter:
    def __init__(self, max_requests=5, cooldown=61):
        self.max_requests = max_requests
        self.cooldown = cooldown
        self.requests = 0
        self.window_start = time.monotonic()

    def wait_if_needed(self, stopped, notify):
        # Short waits let the Stop button work during a cooldown.
        remaining = self.cooldown - (time.monotonic() - self.window_start)
        if self.requests >= self.max_requests and remaining > 0:
            notify(f"Ліміт LLM: пауза до {remaining:.0f} секунд…")
            while time.monotonic() - self.window_start < self.cooldown:
                if stopped():
                    return False
                time.sleep(0.1)
        if time.monotonic() - self.window_start >= self.cooldown:
            self.requests = 0
            self.window_start = time.monotonic()
        self.requests += 1
        return not stopped()


SYSTEM_PROMPT = "Create accurate English flashcards. Return only a valid JSON object, no markdown."


def error_message(exc, key=""):
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if response is not None:
        status = response.status_code
    friendly = {
        401: "API-ключ недійсний.",
        402: "Недостатньо кредитів у провайдера.",
        403: "Провайдер відхилив доступ до моделі.",
        404: "Модель або адреса API недоступна. Онови список моделей.",
        429: "Ліміт запитів API (429). Час очікування дивись у рядку лімітів над таблицею.",
    }
    if status in friendly:
        return friendly[status]
    if isinstance(exc, (requests.Timeout, TimeoutError)) or "timeout" in type(exc).__name__.lower():
        return "Час очікування вичерпано. Спробуй ще раз."
    text = str(exc)
    if key:
        text = text.replace(key, "[ключ приховано]")
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "[ключ приховано]", text)
    return text[:600] or type(exc).__name__


def make_chain(s, key):
    if s.provider == "Anthropic":
        from langchain_anthropic import ChatAnthropic

        model = ChatAnthropic(
            model=s.model,
            api_key=key,
            base_url=s.base_url,
            max_tokens=s.max_tokens,
            timeout=60,
            max_retries=1,
        )
    else:
        from langchain_openai import ChatOpenAI

        model = ChatOpenAI(
            model=s.model,
            api_key=key,
            base_url=s.base_url,
            max_tokens=s.max_tokens,
            timeout=60,
            max_retries=1,
        )
    prompt = ChatPromptTemplate.from_messages([("system", SYSTEM_PROMPT), ("human", "{request}")])
    return prompt | model | StrOutputParser()


def list_models(s, key, free_only=False):
    if s.provider == "Anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        url = s.base_url.rstrip("/") + "/v1/models"
    else:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        url = s.base_url.rstrip("/") + "/models"
    response = requests.get(url, headers=headers, timeout=25)
    response.raise_for_status()
    rows = response.json()["data"]
    if free_only and "openrouter.ai" in s.base_url:

        def free(row):
            try:
                return (
                    float(row["pricing"]["prompt"]) == 0
                    and float(row["pricing"]["completion"]) == 0
                )
            except (KeyError, ValueError, TypeError):
                return False

        rows = [r for r in rows if free(r)]
    return sorted(r["id"] for r in rows)


def validate_info(raw, word):
    if not isinstance(raw, dict):
        raise ValueError("Відповідь має бути JSON-об’єктом.")
    for name in ("translation", "sentence"):
        if not isinstance(raw.get(name), str) or not raw[name].strip():
            raise ValueError(f"Модель не повернула текстове поле {name}.")
    for name in ("lemma", "form"):
        if name in raw and (not isinstance(raw[name], str) or not raw[name].strip()):
            raise ValueError(f"Модель повернула некоректне поле {name}.")
    info = make_info(raw, word)
    if is_phrase(word):
        info.lemma = word
    if "<b>" not in highlight(info.sentence, info.form, info.lemma):
        raise ValueError("У реченні немає цільового слова / виразу.")
    return info


class CardState(TypedDict, total=False):
    word: str
    preferred_translation: str
    info: WordInfo
    status: str
    error: str
    attempts: int
    check_anki: bool
    mode: str
    fields: dict
    note_id: int
    usage: dict
    usage_requests: int
    usage_records: list[dict]
    rate_limit: dict


def build_graph(s, key, anki, *, chain=None, tts=None, notify=None, stopped=None):
    """Граф однієї картки: перевірка, генерація, озвучка й додавання."""
    notify = notify or (lambda msg: None)
    stopped = stopped or (lambda: False)
    tts = tts or (lambda text: edge_tts_synthesize(text, s.voice))
    runtime_chain = chain
    rate_limiter = LLMRateLimiter()

    def guard(fn):
        def node(state):
            if stopped():
                return {"status": "cancelled"}
            try:
                return fn(state)
            except Exception as exc:
                return {"status": "failed", "error": error_message(exc, key)}

        return node

    def duplicate(state, word):
        if not state.get("check_anki", True):
            return False
        if anki is None:
            raise ValueError("Anki недоступний для перевірки дублікатів.")
        return anki.word_exists(s.deck, word)

    @guard
    def check(state):
        word, preferred_translation = parse_word_input(state["word"])
        word = normalize_text(word)
        if not word:
            raise ValueError("Не вказано слово або вираз.")
        notify("Перевірка: " + word)
        return {
            "word": word,
            "preferred_translation": preferred_translation,
            "status": "duplicate" if duplicate(state, word) else "working",
            "error": "",
            "attempts": 0,
            "usage_requests": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "usage_records": [],
        }

    @guard
    def generate(state):
        nonlocal runtime_chain
        if not rate_limiter.wait_if_needed(stopped, notify):
            return {"status": "cancelled"}
        notify("Генерація: " + state["word"])
        if runtime_chain is None:
            runtime_chain = make_chain(s, key)
        request = build_prompt(state["word"], s.language, state.get("preferred_translation", ""))
        if state.get("error"):
            request += "\nPrevious response failed validation. Fix this: " + state["error"]

        # Capture usage before StrOutputParser turns the message into text.
        callback = UsageMetadataCallbackHandler()
        attempt = state.get("attempts", 0) + 1
        record = {"word": state["word"], "attempt": attempt, "status": "failed", "usage": None}
        result = {"attempts": attempt, "status": "failed"}
        try:
            response = runtime_chain.invoke({"request": request}, config={"callbacks": [callback]})
            values = list(callback.usage_metadata.values())
            if values:
                record["usage"] = {
                    name: sum(value.get(name, 0) for value in values)
                    for name in ("input_tokens", "output_tokens", "total_tokens")
                }
            try:
                info = validate_info(parse_llm_json(response), state["word"])
                if state.get("preferred_translation"):
                    info.translation = state["preferred_translation"]
                result.update(info=info, status="working", error="")
                record["status"] = "valid"
            except (ValueError, TypeError) as exc:
                result.update(status="invalid", error=str(exc))
                record["status"] = "invalid"
        except Exception as exc:
            result["error"] = error_message(exc, key)
            limit = rate_limit_info(exc)
            if limit is not None:
                result["rate_limit"] = limit
        totals = dict(state.get("usage", {}))
        if record["usage"] is not None:
            for name, count in record["usage"].items():
                totals[name] = totals.get(name, 0) + count
        result.update(
            usage=totals,
            usage_requests=state.get("usage_requests", 0) + int(record["usage"] is not None),
            usage_records=state.get("usage_records", []) + [record],
        )
        return result

    @guard
    def validate(state):
        info = validate_info(vars(state["info"]), state["word"])
        return {"info": info, "status": "duplicate" if duplicate(state, info.lemma) else "ready"}

    @guard
    def audio(state):
        notify("Озвучка: " + state["word"])
        info = state["info"]
        sounds = ""
        if s.audio:
            # Make both files before uploading either one.
            word_audio = tts(info.lemma)
            if stopped():
                return {"status": "cancelled"}
            sentence_audio = tts(info.sentence)
            if stopped():
                return {"status": "cancelled"}
            word_file = anki.store_media(safe_filename("w", info.lemma), word_audio)
            sentence_file = anki.store_media(safe_filename("s", info.sentence), sentence_audio)
            sounds = f"[sound:{word_file}] [sound:{sentence_file}]"
        return {
            "fields": {
                "Word": html.escape(info.lemma, quote=False),
                "Sentence": highlight(info.sentence, info.form, info.lemma),
                "Meaning": html.escape(info.translation, quote=False),
                "Audio": sounds,
            }
        }

    @guard
    def add(state):
        notify("Додавання: " + state["word"])
        try:
            note_id = anki.add_note(
                s.deck,
                state["fields"],
                ["autovocab"] + (["phrase"] if is_phrase(state["word"]) else []),
            )
        except AnkiError as exc:
            if "duplicate" in str(exc).lower():
                return {"status": "duplicate"}
            raise
        return {"status": "added", "note_id": note_id}

    def after_check(state):
        if state["status"] != "working":
            return END
        return "validate" if "info" in state else "generate"

    def after_generate(state):
        if state["status"] == "invalid" and state.get("attempts", 0) < 2:
            return "generate"
        return "validate" if state["status"] == "working" else END

    def after_validate(state):
        return "audio" if state["status"] == "ready" and state.get("mode") == "add" else END

    graph = StateGraph(CardState)
    for name, fn in [
        ("check", check),
        ("generate", generate),
        ("validate", validate),
        ("audio", audio),
        ("add", add),
    ]:
        graph.add_node(name, fn)
    graph.add_edge(START, "check")
    graph.add_conditional_edges("check", after_check, [END, "validate", "generate"])
    graph.add_conditional_edges("generate", after_generate, ["generate", "validate", END])
    graph.add_conditional_edges("validate", after_validate, ["audio", END])
    graph.add_conditional_edges(
        "audio", lambda st: END if st["status"] in ("failed", "cancelled") else "add", [END, "add"]
    )
    graph.add_edge("add", END)
    return graph.compile()
