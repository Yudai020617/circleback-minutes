from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import APP_ROOT, BRAIN_ROOT
DEFAULT_SPEAKER_ALIASES_PATH = APP_ROOT / "knowledge" / "speaker-aliases.md"
DEFAULT_INTERNAL_PEOPLE_PATH = APP_ROOT / "knowledge" / "internal-people.md"
DEFAULT_MEMBERS_JSON_PATH = BRAIN_ROOT / "40_人物組織" / "members.json"
DEFAULT_PERSONS_DIR_PATH = BRAIN_ROOT / "40_人物組織" / "persons"


@dataclass(frozen=True)
class SpeakerAliasRow:
    raw: str
    canonical: str
    note: str


def load_roster(
    members_json_path: Path | str,
    internal_people_path: Path | str,
) -> frozenset[str]:
    names = set(load_member_names(members_json_path))
    names.update(load_internal_people_names(Path(internal_people_path)))
    return frozenset(names)


def load_member_names(members_json_path: Path | str) -> frozenset[str]:
    names: set[str] = set()
    members_path = Path(members_json_path)
    if members_path.is_file():
        payload = json.loads(members_path.read_text(encoding="utf-8"))
        members = payload.get("members") if isinstance(payload, dict) else None
        if isinstance(members, list):
            for item in members:
                if not isinstance(item, dict):
                    continue
                name = clean_name(item.get("name"))
                if name:
                    names.add(name)
    return frozenset(names)


def load_speaker_aliases(aliases_path: Path | str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for row in load_speaker_alias_rows(aliases_path):
        for raw_name in split_raw_aliases(row.raw):
            add_alias(aliases, raw_name, row.canonical)
    return aliases


def append_speaker_alias_row(
    aliases_path: Path | str,
    *,
    raw: str,
    canonical: str,
    note: str,
) -> bool:
    path = Path(aliases_path)
    if any(raw in split_raw_aliases(row.raw) for row in load_speaker_alias_rows(path)):
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    insert_index = next(
        (index for index, line in enumerate(lines) if line.startswith("## 未登録")),
        len(lines),
    )
    while insert_index > 0 and not lines[insert_index - 1].strip():
        insert_index -= 1
    lines.insert(insert_index, f"| {raw} | {canonical} | {note} |")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return True


def load_person_name_stems(root_path: Path | str) -> frozenset[str]:
    persons_dir = resolve_persons_dir(root_path)
    if not persons_dir.is_dir():
        return frozenset()
    return frozenset(path.stem for path in sorted(persons_dir.glob("*.md")) if path.is_file())


def resolve_persons_dir(root_path: Path | str) -> Path:
    persons_dir = Path(root_path) / "40_人物組織" / "persons"
    if not persons_dir.exists():
        return DEFAULT_PERSONS_DIR_PATH
    return persons_dir


def normalize_person_name(name: str, aliases: dict[str, str]) -> str:
    cleaned = clean_name(name)
    if not cleaned:
        return ""
    if cleaned in aliases:
        return aliases[cleaned]
    folded = cleaned.casefold()
    return aliases.get(folded, cleaned)


def load_speaker_alias_rows(aliases_path: Path | str) -> tuple[SpeakerAliasRow, ...]:
    path = Path(aliases_path)
    if not path.is_file():
        return ()
    rows: list[SpeakerAliasRow] = []
    for row in markdown_table_rows(path.read_text(encoding="utf-8")):
        raw = clean_name(row.get("raw"))
        canonical = clean_name(row.get("正規名"))
        if not raw or not canonical:
            continue
        rows.append(SpeakerAliasRow(raw=raw, canonical=canonical, note=clean_name(row.get("備考"))))
    return tuple(rows)


def speaker_alias_index_lines(aliases_path: Path | str) -> tuple[str, ...]:
    rows = load_speaker_alias_rows(aliases_path)
    if not rows:
        return ("- なし",)
    lines: list[str] = []
    for row in rows:
        note = f" | {row.note}" if row.note else ""
        lines.append(f"- {row.raw}: {row.canonical}{note}")
    return tuple(lines)


def load_internal_people_names(internal_people_path: Path) -> frozenset[str]:
    if not internal_people_path.is_file():
        return frozenset()
    text = internal_people_path.read_text(encoding="utf-8")
    names: set[str] = set()
    for row in markdown_table_rows(text):
        name = clean_name(row.get("name") or row.get("名前") or row.get("正規名"))
        if name and name != "なし":
            names.add(name)
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        value = clean_name(stripped[2:])
        if value and ":" not in value and "：" not in value:
            names.add(value)
    return frozenset(names)


def split_raw_aliases(raw: str) -> tuple[str, ...]:
    return tuple(clean_name(part) for part in raw.split("/") if clean_name(part))


def add_alias(aliases: dict[str, str], raw_name: str, canonical: str) -> None:
    raw = clean_name(raw_name)
    normalized = clean_name(canonical)
    if not raw or not normalized:
        return
    aliases[raw] = normalized
    folded = raw.casefold()
    if folded != raw:
        aliases[folded] = normalized


def markdown_table_rows(text: str) -> tuple[dict[str, str], ...]:
    header: tuple[str, ...] | None = None
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        cells = split_markdown_table_line(line)
        if cells is None:
            continue
        if is_separator_row(cells):
            continue
        if header is None:
            header = cells
            continue
        if len(cells) < len(header):
            cells = (*cells, *(("",) * (len(header) - len(cells))))
        rows.append(dict(zip(header, cells, strict=False)))
    return tuple(rows)


def split_markdown_table_line(line: str) -> tuple[str, ...] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    return tuple(cell.strip() for cell in stripped.strip("|").split("|"))


def is_separator_row(cells: tuple[str, ...]) -> bool:
    return all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells if cell.strip())


def clean_name(value: Any) -> str:
    return str(value or "").strip()


__all__ = [
    "DEFAULT_INTERNAL_PEOPLE_PATH",
    "DEFAULT_MEMBERS_JSON_PATH",
    "DEFAULT_PERSONS_DIR_PATH",
    "DEFAULT_SPEAKER_ALIASES_PATH",
    "SpeakerAliasRow",
    "append_speaker_alias_row",
    "load_member_names",
    "load_person_name_stems",
    "load_roster",
    "load_speaker_alias_rows",
    "load_speaker_aliases",
    "normalize_person_name",
    "resolve_persons_dir",
    "speaker_alias_index_lines",
]
