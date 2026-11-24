"""Read rate-limit hints without displaying raw provider errors or credentials."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import time


def rate_limit_info(exc, now=None):
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) if response is not None else None
    if (status or getattr(exc, "status_code", None)) != 429:
        return None
    now = time.time() if now is None else now
    body = getattr(exc, "body", None)
    if not isinstance(body, dict) and response is not None:
        try:
            body = response.json()
        except (ValueError, TypeError):
            body = {}
    error = body.get("error", body) if isinstance(body, dict) else {}
    error = error if isinstance(error, dict) else {}
    metadata = error.get("metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    headers = metadata.get("headers", {})
    headers = (
        {str(k).lower(): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {}
    )
    if response is not None:
        headers.update({str(k).lower(): str(v) for k, v in response.headers.items()})

    deadlines = []
    for name in ("retry-after", "x-ratelimit-reset", "anthropic-ratelimit-requests-reset"):
        value = headers.get(name)
        if value is None:
            continue
        try:
            number = float(value)
            if not math.isfinite(number):
                continue
            deadline = now + max(0, number) if name == "retry-after" else number
            if name != "retry-after" and deadline > 100_000_000_000:
                deadline /= 1000  # OpenRouter also returns Unix milliseconds.
        except ValueError:
            try:
                date = (
                    parsedate_to_datetime(value)
                    if name == "retry-after"
                    else datetime.fromisoformat(value.replace("Z", "+00:00"))
                )
                deadline = date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp()
            except (ValueError, TypeError, OverflowError):
                continue
        if math.isfinite(deadline) and 0 <= deadline <= 253_402_214_400:
            deadlines.append(deadline)

    message = str(error.get("message", "")).lower()
    source = metadata.get("limit_source")
    if "free-models-per-day" in message or source == "openrouter_free_tier_per_day":
        scope = "Денна квота безкоштовних моделей"
    elif "free-models-per-min" in message or source == "openrouter_free_tier_per_minute":
        scope = "Хвилинна квота безкоштовних моделей"
    elif source == "upstream_provider_shared_pool" or metadata.get("provider_name"):
        scope = "Обмеження провайдера моделі"
    else:
        scope = "Ліміт запитів API (429)"
    result = {"scope": scope, "reset_at": max(deadlines) if deadlines else None}
    for name in ("limit", "remaining"):
        value = headers.get("x-ratelimit-" + name, "")
        if value.isascii() and value.isdigit() and len(value) <= 12:
            result[name] = int(value)
    return result


def format_rate_limit(info, now=None):
    now = time.time() if now is None else now
    text = info["scope"]
    if "limit" in info:
        text += f" · Ліміт: {info['limit']}"
    if "remaining" in info:
        text += f" · Залишилось: {info['remaining']}"
    reset = info.get("reset_at")
    if reset is None:
        return text + " · API не повідомив час скидання."
    seconds = max(0, math.ceil(reset - now))
    if seconds == 0:
        return text + " · Час очікування минув; можна повторити запит."
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    # Explicit UTC keeps the timestamp unambiguous on any Windows time-zone setting.
    stamp = datetime.fromtimestamp(reset, timezone.utc).strftime("%d.%m.%Y %H:%M:%S UTC")
    return text + f" · Повторити після {stamp} · Залишилось {hours:02}:{minutes:02}:{seconds:02}"
