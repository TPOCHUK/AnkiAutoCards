#!/usr/bin/env python3
"""Anki API, card templates, text helpers and audio."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import re
from dataclasses import dataclass
from typing import Iterable

import requests

# Anki checks duplicates using the first field.
FIELDS = ["Word", "Sentence", "Image", "Meaning", "Audio"]
# Image stays empty.
REQUIRED_FIELDS = ["Word", "Sentence", "Meaning", "Audio"]

CSS = """\
b {
font-weight: 400;
color: #BD100D;
}

b a {
font-weight: 400;
color: #840b09;
}

a {
color: inherit;
}

li, ol, ul {
margin-bottom: 4px;
font-size: 24px;
list-style: none;
padding: 0;
text-align: center;

}

code {
font-size: 20px;
}

img {
display: block;
width: 100%;
max-width: 520px;
margin: 0 auto;
}

.image-container {
display: flex;
margin-bottom: 20px;
}

.card {
font-size: 20px;
text-align: center;
color: #45363B;
background-color: #D4CCB9;
}

.front {
max-width: 700px;
margin: 0 auto;
font-size: 36px;
}

.front_sentence {
margin-top: 40px;
margin-bottom: 40px;
font-size: 28px;
}

.front_sentence b {
font-size: 36px;
}

.front_sentence i {
color: #0189BC;
font-style: normal;
}

.back {
max-width: 700px;
margin: 0 auto;
}

.back_content {
text-align: left;
margin-top: 30px;
}

.word-container {
text-align: center;
margin-top: 10px;
}

.word {
font-size: 30px;
color: #BD100D;
font-weight: bold;
}

.meaning {
margin-top: 20px;
color: #FFFFFF !important;
font-size: 20px;
text-align: center;
}

.furigana {
font-size: 36px;
color: #BD100D;
}

.pitch {
color: #BD100D;
}

.pitch li b {
color: #BD100D;
}

.pitch li {
color: #45363B;
}

.tags {
font-style: italic;
}

.audio-container {
margin-top: 20px;
text-align: left;
color: #45363B;
display: flex;
align-items: center;
}
"""

FRONT_TEMPLATE = """\
<div class="front">
<div class="front_sentence">{{Sentence}}</div>
</div>
<div class="word-container">
<div class="word">{{Word}}</div>
</div>
"""

BACK_TEMPLATE = """\
<div class="back">
{{FrontSide}}

<hr id=answer>

<div class="back_content">
<div class="image-container">{{Image}}</div>
<div class="word-container">
<div class="word">{{Word}}</div>
</div>
<div class="meaning">{{Meaning}}</div>
<div class="audio-container">
{{#Audio}}
Word:
{{Audio}}
{{/Audio}}
</div>
</div>
</div>
"""


class AnkiError(RuntimeError):
    pass


# AnkiConnect
class AnkiConnect:
    def __init__(
        self,
        url: str = "http://127.0.0.1:8765",
        session: requests.Session | None = None,
        model: str = "AutoVocab",
    ):
        self.url = url
        self.model = model
        self.http = session or requests.Session()
        self.model_fields: list[str] = list(FIELDS)

    def invoke(self, action: str, **params):
        payload = {"action": action, "version": 6, "params": params}
        try:
            resp = self.http.post(self.url, json=payload, timeout=30)
        except requests.ConnectionError as e:
            raise AnkiError(
                "Не можу підключитись до Anki. Запусти Anki і перевір, що "
                "встановлений плагін AnkiConnect (код 2055492159)."
            ) from e
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            raise AnkiError(f"AnkiConnect [{action}]: {data['error']}")
        return data.get("result")

    def ensure_deck(self, deck: str) -> None:
        # Existing decks stay unchanged.
        self.invoke("createDeck", deck=deck)

    def ensure_model(self) -> list[str]:
        """Створює тип нотатки, якщо його нема; перевіряє, що потрібні поля є."""
        if self.model not in self.invoke("modelNames"):
            self.invoke(
                "createModel",
                modelName=self.model,
                inOrderFields=FIELDS,
                css=CSS,
                cardTemplates=[{"Name": "Card 1", "Front": FRONT_TEMPLATE, "Back": BACK_TEMPLATE}],
            )
        self.model_fields = self.invoke("modelFieldNames", modelName=self.model)
        missing = [f for f in REQUIRED_FIELDS if f not in self.model_fields]
        if missing:
            raise AnkiError(
                f"У типі нотатки '{self.model}' немає полів: {', '.join(missing)}. "
                f"Є: {', '.join(self.model_fields)}."
            )
        return self.model_fields

    def word_exists(self, deck: str, word: str) -> bool:
        # Escape special characters in Anki search.
        deck = deck.replace("\\", "\\\\").replace('"', '\\"')
        w = re.sub(r'([\\"*_:])', r"\\\1", word)
        ids = self.invoke("findNotes", query=f'deck:"{deck}" "Word:{w}"')
        return bool(ids)

    def store_media(self, filename: str, data: bytes) -> str:
        self.invoke(
            "storeMediaFile",
            filename=filename,
            data=base64.b64encode(data).decode("ascii"),
        )
        return filename

    def add_note(self, deck: str, fields: dict, tags: list[str]) -> int:
        # Leave unused fields empty.
        fields = {name: fields.get(name, "") for name in self.model_fields}
        return self.invoke(
            "addNote",
            note={
                "deckName": deck,
                "modelName": self.model,
                "fields": fields,
                "tags": tags,
                "options": {
                    "allowDuplicate": False,
                    "duplicateScope": "deck",
                },
            },
        )


# LLM: переклад + речення
@dataclass
class WordInfo:
    lemma: str  # dictionary form
    form: str  # form used in the sentence
    translation: str
    sentence: str


PROMPT = """English flashcard for "{word}". JSON fields:
lemma: dictionary form, lowercase except proper nouns
form: exact form used in the sentence
translation: most common {lang} meaning, 1-3 words
sentence: natural English, 8-14 words, medium difficulty, vivid context that helps picture the word
"""

PHRASE_PROMPT = """English flashcard for the spoken expression "{word}". JSON fields:
translation: natural {lang} equivalent, not literal, 2-6 words
sentence: grammatical, conversational English, 6-16 words, context showing the expression's meaning
Use the expression naturally; inflect verbs as needed.
Parentheses mark optional or variable words: adapt them naturally. Choose one / alternative.
Do not copy parentheses or / alternatives unless they are part of the actual expression.
"""


def normalize_text(text: str) -> str:
    """Єдині апострофи (`, ’, ‘, ´ -> ') і один пробіл між словами."""
    for ch in "`’‘´":
        text = text.replace(ch, "'")
    return " ".join(text.split())


def parse_word_input(text: str) -> tuple[str, str]:
    """Розділяє слово/вираз і власні переклади."""
    text = normalize_text(text)

    parts = re.split(r"\s+[—–-]\s+", text, maxsplit=1)

    if len(parts) == 2:
        word = parts[0].strip()
        translations = [item.strip() for item in parts[1].split(",") if item.strip()]

        if word and translations:
            return word, ", ".join(translations)

    return text, ""


def is_phrase(text: str) -> bool:
    return len(text.split()) > 1


def build_prompt(
    word: str,
    lang: str = "Ukrainian",
    preferred_translation: str = "",
) -> str:
    template = PHRASE_PROMPT if is_phrase(word) else PROMPT
    prompt = template.format(word=word, lang=lang)

    if preferred_translation:
        translations = [item.strip() for item in preferred_translation.split(",") if item.strip()]

        meanings = "; ".join(translations)

        prompt += f"""
Use only these meanings: {meanings}.
The sentence must clearly show ONE of them.
translation: ALL supplied meanings, unchanged, in order, comma-separated; ignore the length limit.
"""

    return prompt


def parse_llm_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            raise ValueError(f"LLM не повернув JSON: {text[:200]!r}")
        return json.loads(m.group(0))


def make_info(raw: dict, word: str) -> WordInfo:
    for key in ("translation", "sentence"):
        if not str(raw.get(key, "")).strip():
            raise ValueError(f"У відповіді LLM немає поля '{key}'")
    return WordInfo(
        lemma=str(raw.get("lemma") or word).strip(),
        form=str(raw.get("form") or word).strip(),
        translation=str(raw["translation"]).strip(),
        sentence=str(raw["sentence"]).strip(),
    )


# Речення з виділенням слова
def highlight(sentence: str, form: str, lemma: str = "") -> str:
    """Екранує HTML і виділяє цільове слово або вираз."""
    normalized_sentence = normalize_text(sentence)
    safe = html.escape(normalized_sentence, quote=False)

    def expand_expression(expression: str) -> list[str]:
        expression = re.sub(r"\s*(?:…|\.{3})\s*", " ", expression).strip()
        expression = normalize_text(expression)
        if not expression:
            return []

        # Alternatives: "Despite / In spite of"
        alternatives = [
            normalize_text(part)
            for part in re.split(r"\s*/\s*", expression)
            if normalize_text(part)
        ]

        results = []

        for alternative in alternatives:
            # Optional words: "Nerd out (about something)"
            optional_parts = re.findall(r"\(([^()]*)\)", alternative)

            base = re.sub(r"\s*\([^()]*\)", "", alternative).strip()
            variants = [base]

            for optional in optional_parts:
                variants.append(normalize_text(base + " " + optional))

            results.extend(v for v in variants if v)

        # Try longer expressions first.
        return sorted(set(results), key=len, reverse=True)

    candidates = []

    for expression in (form, lemma):
        candidates.extend(expand_expression(expression or ""))

    # Verb forms: "be supposed to"
    for candidate in list(candidates):
        if candidate.lower() == "be supposed to":
            candidates.extend(
                [
                    "am supposed to",
                    "is supposed to",
                    "are supposed to",
                    "was supposed to",
                    "were supposed to",
                    "been supposed to",
                    "being supposed to",
                ]
            )

    for candidate in dict.fromkeys(candidates):
        parts = html.escape(candidate, quote=False).split()

        if not parts:
            continue

        pattern = re.compile(
            r"(?<!\w)" + r"\s+".join(map(re.escape, parts)) + r"(?!\w)",
            re.IGNORECASE,
        )

        match = pattern.search(safe)

        if match:
            return safe[: match.start()] + "<b>" + match.group(0) + "</b>" + safe[match.end() :]

    return safe


# TTS
def edge_tts_synthesize(text: str, voice: str = "en-US-AriaNeural") -> bytes:
    import edge_tts

    async def run() -> bytes:
        chunks = bytearray()
        async for chunk in edge_tts.Communicate(text, voice).stream():
            if chunk["type"] == "audio":
                chunks += chunk["data"]
        return bytes(chunks)

    data = asyncio.run(run())
    if not data:
        raise RuntimeError(f"TTS повернув порожнє аудіо для {text!r}")
    return data


def safe_filename(prefix: str, text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "x"
    # Different sentences need different filenames.
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
    return f"autovocab_{prefix}_{slug}_{digest}.mp3"


# Конвеєр
def read_words(lines: Iterable[str]) -> list[str]:
    seen, out = set(), []
    for line in lines:
        line = normalize_text(line.split("#", 1)[0])
        if not line:
            continue
        key = line.lower()
        if key not in seen:
            seen.add(key)
            out.append(line)
    return out
