from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

ALLOWED_KEYS = (
    "title",
    "date",
    "circleback_id",
    "attendees",
    "client",
    "type",
    "tags",
    "share",
)
MEETING_SHARE_VALUES = ("open", "confidential")
# Circleback新世代IDは - と _ を含む(実例 0lqlgiUNNN2zN9GS6e-86、2026-08-11実測)
CIRCLEBACK_ID_RE = re.compile(r"[A-Za-z0-9_-]+$")


def render_frontmatter(values: dict[str, Any]) -> str:
    extra = set(values) - set(ALLOWED_KEYS)
    if extra:
        raise ValueError(f"frontmatter has unsupported keys: {', '.join(sorted(extra))}")
    lines = ["---"]
    for key in ALLOWED_KEYS:
        if key == "circleback_id":
            value = values.get(key)
            if not isinstance(value, str) or not CIRCLEBACK_ID_RE.fullmatch(value):
                raise ValueError("frontmatter circleback_id must be ASCII alphanumeric")
            lines.append(f"{key}: {render_scalar(value)}")
            continue
        if key == "share":
            if key not in values:
                continue
            share = values[key]
            if not isinstance(share, str) or share not in MEETING_SHARE_VALUES:
                raise ValueError("frontmatter share must be open or confidential")
            lines.append(f"share: {share}")
            continue
        value = values.get(key)
        if key in {"attendees", "tags"}:
            lines.append(f"{key}: {render_list(value or [])}")
        else:
            lines.append(f"{key}: {render_scalar(value or '')}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def split_frontmatter(markdown: str) -> tuple[dict[str, Any], str]:
    if not markdown.startswith("---\n"):
        return {}, markdown
    end = markdown.find("\n---\n", 4)
    if end == -1:
        raise ValueError("frontmatter closing marker not found")
    raw = markdown[4:end].splitlines()
    body = markdown[end + 5 :]
    parsed: dict[str, Any] = {}
    index = 0
    while index < len(raw):
        line = raw[index]
        index += 1
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        text = value.strip()
        if key in {"attendees", "tags"}:
            if text:
                parsed[key] = parse_inline_list(text)
                continue
            items: list[str] = []
            while index < len(raw):
                item_match = re.fullmatch(r"\s+-\s+(.+?)\s*", raw[index])
                if item_match is None:
                    break
                item = unquote(item_match.group(1))
                if item:
                    items.append(item)
                index += 1
            parsed[key] = items
        else:
            parsed[key] = unquote(text)
    return parsed, body


def meeting_share(frontmatter: Mapping[str, Any]) -> str:
    if "share" not in frontmatter:
        raise ValueError("frontmatter share must be open or confidential")
    value = frontmatter["share"]
    if not isinstance(value, str) or value not in MEETING_SHARE_VALUES:
        raise ValueError("frontmatter share must be open or confidential")
    return value


def meeting_circleback_id(frontmatter: Mapping[str, Any]) -> str:
    value = frontmatter.get("circleback_id")
    if not isinstance(value, str) or not CIRCLEBACK_ID_RE.fullmatch(value):
        raise ValueError("frontmatter circleback_id must be ASCII alphanumeric")
    return value


def render_scalar(value: Any) -> str:
    text = str(value).replace("\n", " ").strip()
    if not text:
        return '""'
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_list(values: Iterable[Any]) -> str:
    return "[" + ", ".join(render_scalar(value) for value in values) + "]"


def parse_inline_list(text: str) -> list[str]:
    if not text.startswith("[") or not text.endswith("]"):
        return []
    inner = text[1:-1].strip()
    if not inner:
        return []
    items: list[str] = []
    current = []
    in_quote = False
    escape = False
    for char in inner:
        if escape:
            current.append(char)
            escape = False
        elif char == "\\":
            escape = True
        elif char == '"':
            in_quote = not in_quote
        elif char == "," and not in_quote:
            items.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    items.append("".join(current).strip())
    return [unquote(item) for item in items if unquote(item)]


def unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return text
