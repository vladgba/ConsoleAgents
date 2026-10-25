"""Recognise usage-limit / auth errors in CLI output and work out when a limit resets."""
from __future__ import annotations

import datetime as dt
import re

LIMIT_PATTERNS = [
    r"usage limit",
    r"rate[ _-]?limit",
    r"limit reached",
    r"limit (?:has been )?exceeded",
    r"hit your (?:usage )?limit",
    r"quota (?:exceeded|exhausted|reached)",
    r"exceeded your (?:current )?quota",
    r"resource[_ ]exhausted",
    r"too many requests",
    r"\b429\b",
    r"out of (?:extra )?usage",
    r"credit balance is too low",
    r"insufficient[_ ]quota",
]
AUTH_PATTERNS = [
    r"not logged in",
    r"please (?:log ?in|login|run /login|sign in)",
    r"invalid (?:api[_ ]?key|x-api-key|credentials)",
    r"api[_ ]?key (?:not valid|invalid|expired)",
    r"API_KEY_INVALID",
    r"error authenticating",
    r"authentication (?:failed|required|error)",
    r"unauthori[sz]ed",
    r"\b401\b",
    r"ineligible ?tier",
    r"no longer supported",
]

_LIMIT_RE = re.compile("|".join(LIMIT_PATTERNS), re.I)
_AUTH_RE = re.compile("|".join(AUTH_PATTERNS), re.I)

_UNIT_S = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_DURATION_RE = re.compile(
    r"(?:in|after|retry in|try again in|resets? in)\s+"
    r"((?:\d+(?:\.\d+)?\s*(?:days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])[\s,]*(?:and\s+)?)+)",
    re.I,
)
# "(?![a-z])" instead of a word break: compact forms like "42h18m8s" have a digit right after the unit.
_PART_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])", re.I)
_EPOCH_RE = re.compile(r"(?:\||resets?_?at\"?\s*[:=]?\s*\"?|reset(?:s)?\s+at\s+)(1\d{9})\b", re.I)
_CLOCK_RE = re.compile(r"(?:at|resets?)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|(?:at|resets?)\s+(\d{1,2}):(\d{2})\b", re.I)


def is_limit_error(text: str) -> bool:
    return bool(text and _LIMIT_RE.search(text))


def is_auth_error(text: str) -> bool:
    return bool(text and _AUTH_RE.search(text))


def parse_reset(text: str, now: float) -> float | None:
    """Best-effort reset timestamp from messages like 'resets 3pm', 'try again in 2 hours 5 minutes',
    'Claude AI usage limit reached|1792843200', 'Please retry in 23.4s'."""
    if not text:
        return None
    m = _EPOCH_RE.search(text)
    if m:
        ts = float(m.group(1))
        if ts > now:
            return ts
    m = _DURATION_RE.search(text)
    if m:
        total = 0.0
        for num, unit in _PART_RE.findall(m.group(1)):
            total += float(num) * _UNIT_S[unit[0].lower()]
        if total > 0:
            return now + total
    m = _CLOCK_RE.search(text)
    if m:
        if m.group(1):
            hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
            hour = hour % 12 + (12 if ampm == "pm" else 0)
        else:
            hour, minute = int(m.group(4)), int(m.group(5))
        if hour < 24 and minute < 60:
            base = dt.datetime.fromtimestamp(now)
            target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target.timestamp() <= now:
                target += dt.timedelta(days=1)
            return target.timestamp()
    return None

