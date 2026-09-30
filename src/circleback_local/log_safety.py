from __future__ import annotations

import re
import sys
from pathlib import Path

def redact_sensitive(value: str) -> str:
    value = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "<redacted-email>", value)
    value = re.sub(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?\S+", r"\1<redacted>", value)
    return re.sub(r"(?i)((?:password|token|secret|api[_-]?key)\s*[:=]\s*)\S+", r"\1<redacted>", value)


GENERIC_URL_RE = re.compile(r"https?://\S+")
MANIFEST_PATH_SUFFIXES = (".jsonl", ".ndjson")
MANIFEST_PATH_TOKEN_RE = re.compile(
    r"(?P<prefix>^|[=\s])(?P<path>/.*?(?:\.jsonl|\.ndjson)[.,)\]}]*)(?=$|\s|;)"
)


def one_line(value: str) -> str:
    return " ".join(value.split())


def redact_sensitive_text(value: str) -> str:
    redacted = redact_manifest_path_tokens(value)
    redacted = GENERIC_URL_RE.sub("<redacted-url>", redact_sensitive(redacted))
    return redact_manifest_path_tokens(redacted)


def short_log_detail(value: str | None, limit: int = 240) -> str:
    cleaned = one_line(redact_sensitive_text(value or ""))
    if not cleaned:
        return "(empty)"
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def redact_manifest_path_tokens(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        raw_path = match.group("path")
        path_text, trailing = split_trailing_punctuation(raw_path)
        path = Path(path_text)
        if path.name and should_hide_path_parent(path):
            return f"{match.group('prefix')}{path.name}{trailing}"
        return match.group(0)

    return MANIFEST_PATH_TOKEN_RE.sub(replace, value)


def split_trailing_punctuation(value: str) -> tuple[str, str]:
    trailing = ""
    while value and value[-1] in ".,)]}":
        trailing = value[-1] + trailing
        value = value[:-1]
    return value, trailing


def should_hide_path_parent(path: Path) -> bool:
    name = path.name.lower()
    return "manifest" in name or name.endswith(MANIFEST_PATH_SUFFIXES)
