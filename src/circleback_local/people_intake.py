from __future__ import annotations

import argparse
import dataclasses
import json
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .codex_exec import DEFAULT_CODEX_BIN, run_codex_text
from .execution_gate import add_execution_gate_arguments, apply_execution_gate, parse_execution_gate
from .frontmatter import meeting_circleback_id, render_list, render_scalar, split_frontmatter
from .paths import BRAIN_ROOT
from .people import (
    DEFAULT_SPEAKER_ALIASES_PATH,
    load_speaker_aliases,
    normalize_person_name,
)

DEFAULT_PERSONS_DIR = BRAIN_ROOT / "40_人物組織" / "persons"
DEFAULT_MINUTES_DIR = BRAIN_ROOT / "50_議事録"
DEFAULT_INTAKE_BLOCKLIST_PATH = DEFAULT_SPEAKER_ALIASES_PATH.with_name("intake-blocklist.md")
JAPANESE_FULL_NAME_RE = re.compile(
    r"^(?=[一-龯々〆〇ヵヶ髙﨑神塚德ぁ-んァ-ヶー]{3,12}$)"
    r"(?=.*[一-龯々〆〇ヵヶ髙﨑神塚德])"
    r"[一-龯々〆〇ヵヶ髙﨑神塚德ぁ-んァ-ヶー]+$"
)
UNKNOWN_SPEAKER_LABEL_RE = re.compile(r"^不明話者\d*$")
HONORIFIC_SUFFIXES = ("さん", "くん", "君", "ちゃん", "先生", "氏")


@dataclasses.dataclass(frozen=True)
class IntakeMeeting:
    meeting_id: str
    meeting_title: str
    meeting_path: Path
    attendees: tuple[str, ...]
    context: str


@dataclasses.dataclass(frozen=True)
class IntakeSkip:
    name: str
    reason: str
    detail: str = ""


@dataclasses.dataclass(frozen=True)
class IntakeReport:
    meeting: IntakeMeeting
    would_create: tuple[str, ...]
    created: tuple[Path, ...]
    skipped: tuple[IntakeSkip, ...]
    read_back: tuple[Path, ...] = ()
    warnings: tuple[IntakeSkip, ...] = ()


@dataclasses.dataclass(frozen=True)
class PersonContextMemo:
    affiliation: str
    role: str
    context_memo: str
    confirmed_facts: tuple[str, ...]
    related_topics: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class BackfillReport:
    meetings: int
    reports: tuple[IntakeReport, ...]

    @property
    def created_count(self) -> int:
        return sum(len(report.created) for report in self.reports)

    @property
    def would_create_count(self) -> int:
        return sum(len(report.would_create) for report in self.reports)

    @property
    def skipped_count(self) -> int:
        return sum(len(report.skipped) for report in self.reports)

    @property
    def warning_count(self) -> int:
        return sum(len(report.warnings) for report in self.reports)


MemoGenerator = Callable[[str, IntakeMeeting], PersonContextMemo]


def is_confirmed_japanese_full_name(name: str) -> bool:
    text = name.strip()
    return bool(JAPANESE_FULL_NAME_RE.fullmatch(text)) and not text.endswith(HONORIFIC_SUFFIXES)


def intake_people_for_meeting(
    meeting: IntakeMeeting,
    *,
    persons_dir: Path = DEFAULT_PERSONS_DIR,
    aliases_path: Path = DEFAULT_SPEAKER_ALIASES_PATH,
    aliases: dict[str, str] | None = None,
    blocklist_path: Path = DEFAULT_INTAKE_BLOCKLIST_PATH,
    blocklist: frozenset[str] | None = None,
    execute: bool = False,
    memo_generator: MemoGenerator | None = None,
) -> IntakeReport:
    persons_dir = Path(persons_dir)
    existing_names = existing_person_names(persons_dir)
    speaker_aliases = aliases if aliases is not None else load_speaker_aliases(aliases_path)
    intake_blocklist = blocklist if blocklist is not None else load_intake_blocklist(blocklist_path)
    would_create: list[str] = []
    created: list[Path] = []
    skipped: list[IntakeSkip] = []
    read_back: list[Path] = []
    warnings: list[IntakeSkip] = []
    generator = memo_generator or generate_context_memo
    candidate_names = normalized_unique_names(meeting.attendees, speaker_aliases)

    if not persons_dir.is_dir():
        return IntakeReport(
            meeting=meeting,
            would_create=(),
            created=(),
            skipped=tuple(
                IntakeSkip(name, "missing_persons_dir", str(persons_dir))
                for name in candidate_names
            ),
        )

    for name in candidate_names:
        if UNKNOWN_SPEAKER_LABEL_RE.fullmatch(name):
            skipped.append(IntakeSkip(name, "unknown_speaker_label"))
            continue
        if name in intake_blocklist:
            skipped.append(IntakeSkip(name, "blocklisted"))
            continue
        if name in existing_names:
            skipped.append(IntakeSkip(name, "existing_person"))
            continue
        if not is_confirmed_japanese_full_name(name):
            skipped.append(IntakeSkip(name, "not_confirmed_japanese_full_name"))
            continue
        target_path = persons_dir / f"{name}.md"
        if target_path.exists():
            skipped.append(IntakeSkip(name, "existing_person_file", str(target_path)))
            continue
        if not execute:
            would_create.append(name)
            continue
        try:
            memo = generator(name, meeting)
            if not isinstance(memo, PersonContextMemo):
                raise TypeError("people intake memo generator returned unexpected payload")
        except Exception as exc:  # noqa: BLE001 - memo failures skip the affected person.
            warnings.append(IntakeSkip(name, "memo_generation_failed", str(exc)))
            continue
        markdown = render_person_draft(name, meeting, memo)
        target_path.write_text(markdown, encoding="utf-8")
        target_path.chmod(0o600)
        if not target_path.is_file() or target_path.read_text(encoding="utf-8") != markdown:
            raise RuntimeError(f"people intake read-back failed: {target_path}")
        read_back.append(target_path)
        created.append(target_path)
        existing_names.add(name)

    return IntakeReport(
        meeting=meeting,
        would_create=tuple(would_create),
        created=tuple(created),
        skipped=tuple(skipped),
        read_back=tuple(read_back),
        warnings=tuple(warnings),
    )


def run_backfill(
    *,
    minutes_dir: Path = DEFAULT_MINUTES_DIR,
    persons_dir: Path = DEFAULT_PERSONS_DIR,
    aliases_path: Path = DEFAULT_SPEAKER_ALIASES_PATH,
    blocklist_path: Path = DEFAULT_INTAKE_BLOCKLIST_PATH,
    execute: bool = False,
    memo_generator: MemoGenerator | None = None,
) -> BackfillReport:
    reports: list[IntakeReport] = []
    planned_names: set[str] = set()
    aliases = load_speaker_aliases(aliases_path)
    blocklist = load_intake_blocklist(blocklist_path)
    for path in sorted(Path(minutes_dir).glob("*/minutes.md")):
        meeting = intake_meeting_from_minutes(path)
        if meeting is None:
            continue
        report = intake_people_for_meeting(
            meeting,
            persons_dir=persons_dir,
            aliases=aliases,
            blocklist=blocklist,
            execute=execute,
            memo_generator=memo_generator,
        )
        if not execute:
            unique_would_create = tuple(
                name for name in report.would_create if name not in planned_names
            )
            duplicate_plans = tuple(
                IntakeSkip(name, "already_planned")
                for name in report.would_create
                if name in planned_names
            )
            planned_names.update(unique_would_create)
            report = dataclasses.replace(
                report,
                would_create=unique_would_create,
                skipped=report.skipped + duplicate_plans,
            )
        reports.append(report)
    return BackfillReport(meetings=len(reports), reports=tuple(reports))


def intake_meeting_from_minutes(path: Path) -> IntakeMeeting | None:
    frontmatter, body = split_frontmatter(path.read_text(encoding="utf-8"))
    attendees = frontmatter.get("attendees")
    if not isinstance(attendees, list) or not attendees:
        return None
    title = str(frontmatter.get("title") or path.stem).strip()
    meeting_id = meeting_circleback_id(frontmatter)
    return IntakeMeeting(
        meeting_id=meeting_id,
        meeting_title=title,
        meeting_path=path,
        attendees=tuple(str(attendee).strip() for attendee in attendees if str(attendee).strip()),
        context=trim_context(body),
    )


def existing_person_names(persons_dir: Path) -> set[str]:
    if not persons_dir.is_dir():
        return set()
    names: set[str] = set()
    for path in sorted(persons_dir.glob("*.md")):
        names.add(path.stem)
        try:
            frontmatter, _body = split_frontmatter(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        title = frontmatter.get("title")
        if isinstance(title, str) and title.strip():
            names.add(title.strip())
    return names


def unique_names(values: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = str(value).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return tuple(result)


def normalized_unique_names(values: tuple[str, ...], aliases: dict[str, str]) -> tuple[str, ...]:
    return unique_names(tuple(normalize_person_name(value, aliases) for value in values))


def load_intake_blocklist(blocklist_path: Path | str) -> frozenset[str]:
    path = Path(blocklist_path)
    if not path.is_file():
        return frozenset()
    names = {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    return frozenset(names)


def generate_context_memo(
    name: str,
    meeting: IntakeMeeting,
    *,
    codex_bin: str = DEFAULT_CODEX_BIN,
    timeout_seconds: int = 600,
) -> PersonContextMemo:
    prompt = "\n".join(
        (
            "人物インテーク用の文脈メモを作る。",
            "入力にない所属・役割を断定しない。",
            "次のJSONオブジェクトだけを返す。",
            "確認できない affiliation / role は null にする。",
            "fields: affiliation, role, context_memo, confirmed_facts, related_topics",
            "",
            f"name: {name}",
            f"meeting_id: {meeting.meeting_id}",
            f"meeting_title: {meeting.meeting_title}",
            f"meeting_path: {meeting.meeting_path}",
            "context:",
            meeting.context,
        )
    )
    output = run_codex_text(
        prompt,
        codex_bin=codex_bin,
        timeout_seconds=timeout_seconds,
        output_schema=context_memo_output_schema(),
        failure_label="Codex people intake memo",
        sandbox="read-only",
        cwd=meeting.meeting_path.parent if meeting.meeting_path.parent.exists() else Path.cwd(),
        failure_artifact_dir=None,
    )
    return person_context_memo_from_payload(extract_first_json_object(output))


def context_memo_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "affiliation": {"type": ["string", "null"]},
            "role": {"type": ["string", "null"]},
            "context_memo": {"type": "string"},
            "confirmed_facts": {
                "type": "array",
                "items": {"type": "string"},
            },
            "related_topics": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": [
            "affiliation",
            "role",
            "context_memo",
            "confirmed_facts",
            "related_topics",
        ],
    }


def extract_first_json_object(output: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            payload, _end = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("people intake memo JSON object not found")


def person_context_memo_from_payload(payload: dict[str, Any]) -> PersonContextMemo:
    return PersonContextMemo(
        affiliation=text_field_or_default(payload.get("affiliation"), "未確認"),
        role=text_field_or_default(payload.get("role"), "未確認"),
        context_memo=text_field_or_default(payload.get("context_memo"), ""),
        confirmed_facts=text_list_field(payload.get("confirmed_facts")),
        related_topics=text_list_field(payload.get("related_topics")),
    )


def text_field_or_default(value: Any, default: str) -> str:
    if not isinstance(value, str):
        return default
    text = clean_markdown_text(value)
    return text or default


def text_list_field(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        values: Sequence[Any] = (value,)
    elif isinstance(value, list | tuple):
        values = value
    else:
        return ()
    return tuple(
        text for item in values if isinstance(item, str) and (text := clean_markdown_text(item))
    )


def clean_markdown_text(value: str) -> str:
    text = value.strip()
    if text.startswith("{"):
        return ""
    if text.startswith("```"):
        return ""
    return text


def render_person_draft(name: str, meeting: IntakeMeeting, memo: PersonContextMemo) -> str:
    meeting_reference = (
        meeting.meeting_id
        if meeting.meeting_id.startswith("circleback:")
        else f"circleback:{meeting.meeting_id}"
    )
    frontmatter = "\n".join(
        (
            "---",
            f"title: {render_scalar(name)}",
            f"tags: {render_list(('people', 'person'))}",
            'status: "auto-draft"',
            f"first_meeting: {render_scalar(meeting_reference)}",
            "---",
            "",
        )
    )
    lines = [
        "## 基本情報",
        "",
        f"- 所属: {memo.affiliation}",
        f"- 役割: {memo.role}",
        "",
        "## メモ",
        "",
        f"初出meeting: {meeting_reference}",
        *(("", memo.context_memo) if memo.context_memo else ()),
        *(("", *(f"- {fact}" for fact in memo.confirmed_facts)) if memo.confirmed_facts else ()),
        "",
        "## 関連トピック",
        "",
        ", ".join(memo.related_topics) if memo.related_topics else "未確認",
        "",
    ]
    return frontmatter + "\n".join(lines)


def trim_context(body: str, *, max_chars: int = 1200) -> str:
    text = " ".join(line.strip() for line in body.splitlines() if line.strip())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview or apply people auto-draft intake.")
    add_execution_gate_arguments(parser)
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--minutes-dir", type=Path, default=DEFAULT_MINUTES_DIR)
    parser.add_argument("--persons-dir", type=Path, default=DEFAULT_PERSONS_DIR)
    parser.add_argument("--meeting-file", type=Path)
    gate = parse_execution_gate(parser, argv)
    args = parser.parse_args(argv)
    apply_execution_gate(args, gate)
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.backfill:
        backfill_report = run_backfill(
            minutes_dir=args.minutes_dir,
            persons_dir=args.persons_dir,
            execute=args.execute,
        )
        print(json.dumps(backfill_report_payload(backfill_report), ensure_ascii=False, indent=2))
        return 0
    if args.meeting_file:
        meeting = intake_meeting_from_minutes(args.meeting_file)
        if meeting is None:
            print(json.dumps({"ok": True, "created": 0, "skipped": 0}, ensure_ascii=False))
            return 0
        intake_report = intake_people_for_meeting(
            meeting,
            persons_dir=args.persons_dir,
            execute=args.execute,
        )
        print(json.dumps(intake_report_payload(intake_report), ensure_ascii=False, indent=2))
        return 0
    raise SystemExit("--backfill or --meeting-file is required")


def backfill_report_payload(report: BackfillReport) -> dict[str, Any]:
    return {
        "meetings": report.meetings,
        "created_count": report.created_count,
        "would_create_count": report.would_create_count,
        "skipped_count": report.skipped_count,
        "warning_count": report.warning_count,
        "reports": [intake_report_payload(item) for item in report.reports],
    }


def intake_report_payload(report: IntakeReport) -> dict[str, Any]:
    return {
        "meeting_id": report.meeting.meeting_id,
        "meeting_path": str(report.meeting.meeting_path),
        "would_create": list(report.would_create),
        "created": [str(path) for path in report.created],
        "skipped": [dataclasses.asdict(skip) for skip in report.skipped],
        "read_back": [str(path) for path in report.read_back],
        "warnings": [dataclasses.asdict(warning) for warning in report.warnings],
    }


if __name__ == "__main__":
    raise SystemExit(main())
