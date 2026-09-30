from __future__ import annotations

import dataclasses
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .frontmatter import MEETING_SHARE_VALUES
from .paths import BRAIN_ROOT, TRANSCRIPTION_TERMS_DIR, sanitize_filename_stem

logger = logging.getLogger(__name__)

CLIENTS_OVERVIEW_SOURCE_PATH = str(BRAIN_ROOT / "30_案件" / "clients-overview.md")
REFERENCE_FILE_ROOT = BRAIN_ROOT
MEETING_TYPES = ("定例-社内", "定例-外部", "単発MTG-社内", "単発MTG-外部", "商談", "採用")
TRANSCRIPTION_TERMS_ROOT_URL = TRANSCRIPTION_TERMS_DIR.as_uri()
COMMON_TRANSCRIPTION_TERMS_URL = f"{TRANSCRIPTION_TERMS_ROOT_URL}/common.md"
MAX_ADDITIONAL_REFERENCE_FILE_URLS = 12
MAX_CONTEXT_KEYWORDS = 10


@dataclasses.dataclass(frozen=True)
class MeetingPlanFilename:
    subject: str
    summary: str
    stem: str


@dataclasses.dataclass(frozen=True)
class MeetingPlanSpeaker:
    name: str
    raw: tuple[str, ...] = ()
    url: str = ""


@dataclasses.dataclass(frozen=True)
class MeetingPlanMeetingContext:
    overview: str
    main_topics: tuple[str, ...] = ()
    context_keywords: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class MeetingPlanAdditionalReference:
    url: str
    hint: str


@dataclasses.dataclass(frozen=True)
class MeetingPlan:
    client: str
    client_url: str
    meeting_type: str
    speakers: tuple[MeetingPlanSpeaker, ...]
    meeting_context: MeetingPlanMeetingContext
    additional_references: tuple[MeetingPlanAdditionalReference, ...]
    reference_file_urls: tuple[str, ...]
    filename: MeetingPlanFilename
    share: str
    # サブエージェント調査が返した関連会議候補の件数（採用数ではない）。導入前のplanは0
    related_meetings_found: int = 0


def meeting_plan_output_schema() -> dict[str, Any]:
    string_array_schema = {"type": "array", "items": {"type": "string"}}
    meeting_context_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "overview": {"type": "string"},
            "main_topics": string_array_schema,
            "context_keywords": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_CONTEXT_KEYWORDS,
            },
        },
        "required": ["overview", "main_topics", "context_keywords"],
    }
    filename_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "subject": {"type": "string"},
            "summary": {"type": "string"},
        },
        "required": ["subject", "summary"],
    }
    additional_reference_schema = {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "url": {"type": "string"},
                "hint": {"type": "string"},
            },
            "required": ["url", "hint"],
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "client": {"type": "string"},
            "client_url": {"type": "string"},
            "meeting_type": {"type": "string", "enum": list(MEETING_TYPES)},
            "share": {"type": "string", "enum": list(MEETING_SHARE_VALUES)},
            "speakers": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string", "minLength": 1},
                        "raw": {"type": "array", "items": {"type": "string"}},
                        "url": {"type": "string"},
                    },
                    "required": ["name", "raw", "url"],
                },
            },
            "meeting_context": meeting_context_schema,
            "additional_references": additional_reference_schema,
            "filename": filename_schema,
            "related_meetings_found": {"type": "integer", "minimum": 0},
        },
        "required": [
            "client",
            "client_url",
            "meeting_type",
            "share",
            "speakers",
            "meeting_context",
            "additional_references",
            "filename",
            "related_meetings_found",
        ],
    }


def parse_meeting_plan_json(output: str) -> MeetingPlan:
    payload = json.loads(output.strip())
    if not isinstance(payload, dict):
        raise ValueError("meeting plan JSON must be an object")
    filename_payload = payload.get("filename")
    if not isinstance(filename_payload, dict):
        raise ValueError("meeting plan JSON missing filename")
    unexpected_filename_fields = sorted(set(filename_payload) - {"subject", "summary"})
    if unexpected_filename_fields:
        raise ValueError(
            f"meeting plan JSON unexpected filename field: {', '.join(unexpected_filename_fields)}"
        )

    meeting_type = required_string(payload.get("meeting_type"), "meeting_type")
    if meeting_type not in MEETING_TYPES:
        raise ValueError(f"unknown meeting_type: {meeting_type}")

    share = required_string(payload.get("share"), "share")
    if share not in MEETING_SHARE_VALUES:
        raise ValueError(f"unknown share: {share}")

    filename = build_meeting_plan_filename(
        "",
        filename_payload.get("subject"),
        meeting_type,
        filename_payload.get("summary"),
    )

    client = clean_string(payload.get("client"))
    client_url = clean_string(payload.get("client_url"))
    validate_file_url(client_url, "client_url", allow_empty=True)

    speakers = tuple(parse_speakers(payload.get("speakers")))
    if not speakers:
        raise ValueError("meeting plan JSON missing speakers")
    meeting_context = parse_meeting_context(payload.get("meeting_context"))
    additional_references = tuple(parse_additional_references(payload.get("additional_references")))
    seed_reference_urls = unique_strings(
        [
            client_url,
            *(speaker.url for speaker in speakers),
            COMMON_TRANSCRIPTION_TERMS_URL,
        ]
    )
    additional_references = trim_additional_references(
        additional_references,
        seed_reference_urls,
        max_additional_references=MAX_ADDITIONAL_REFERENCE_FILE_URLS,
    )
    reference_file_urls = tuple(
        unique_strings(
            [
                *seed_reference_urls,
                *(reference.url for reference in additional_references),
            ]
        )
    )
    for url in reference_file_urls:
        validate_file_url(url, "reference_file_urls")

    related_meetings_found = payload.get("related_meetings_found", 0)
    if not isinstance(related_meetings_found, int) or isinstance(related_meetings_found, bool):
        raise ValueError("related_meetings_found must be an integer")
    if related_meetings_found < 0:
        raise ValueError("related_meetings_found must be >= 0")

    plan = MeetingPlan(
        client=client,
        client_url=client_url,
        meeting_type=meeting_type,
        speakers=speakers,
        meeting_context=meeting_context,
        additional_references=additional_references,
        reference_file_urls=reference_file_urls,
        filename=filename,
        related_meetings_found=related_meetings_found,
        share=share,
    )
    return plan


def parse_speakers(value: Any) -> list[MeetingPlanSpeaker]:
    if not isinstance(value, list):
        raise ValueError("meeting plan speakers must be an array")
    speakers: list[MeetingPlanSpeaker] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("meeting plan speakers item must be an object")
        name = required_string(item.get("name"), "speakers.name")
        raw = tuple(string_list(item.get("raw"), "speakers.raw"))
        url = clean_string(item.get("url"))
        if name in seen:
            raise ValueError(f"duplicate meeting plan speaker: {name}")
        validate_file_url(url, "speakers.url", allow_empty=True)
        seen.add(name)
        speakers.append(MeetingPlanSpeaker(name=name, raw=raw, url=url))
    return speakers


def parse_meeting_context(value: Any) -> MeetingPlanMeetingContext:
    if not isinstance(value, dict):
        raise ValueError("meeting plan meeting_context must be an object")
    return MeetingPlanMeetingContext(
        overview=required_string(value.get("overview"), "meeting_context.overview"),
        main_topics=tuple(string_list(value.get("main_topics"), "meeting_context.main_topics")),
        context_keywords=tuple(
            string_list(value.get("context_keywords"), "meeting_context.context_keywords")[
                :MAX_CONTEXT_KEYWORDS
            ]
        ),
    )


def parse_additional_references(value: Any) -> list[MeetingPlanAdditionalReference]:
    if not isinstance(value, list):
        raise ValueError("meeting plan additional_references must be an array")
    references: list[MeetingPlanAdditionalReference] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("meeting plan additional_references item must be an object")
        url = required_string(item.get("url"), "additional_references.url")
        hint = required_string(item.get("hint"), "additional_references.hint")
        validate_file_url(url, "additional_references.url")
        references.append(MeetingPlanAdditionalReference(url=url, hint=hint))
    return references


def trim_additional_references(
    references: tuple[MeetingPlanAdditionalReference, ...],
    seed_urls: list[str],
    *,
    max_additional_references: int,
) -> tuple[MeetingPlanAdditionalReference, ...]:
    seen = set(seed_urls)
    kept: list[MeetingPlanAdditionalReference] = []
    for reference in references:
        if reference.url in seen or len(kept) >= max_additional_references:
            continue
        seen.add(reference.url)
        kept.append(reference)
    return tuple(kept)


def unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = clean_string(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def build_filename_stem(date: str, subject: str, filename_type: str, summary: str) -> str:
    return sanitize_filename_stem(
        "_".join(clean_string(part) for part in (date, subject, filename_type, summary))
    )


def build_meeting_plan_filename(
    date: str, subject: Any, meeting_type: Any, summary: Any
) -> MeetingPlanFilename:
    filename_date = clean_string(date)
    filename_type = required_string(meeting_type, "meeting_type")
    if filename_type not in MEETING_TYPES:
        raise ValueError(f"unknown meeting_type: {filename_type}")
    if filename_date and not re.fullmatch(r"\d{8}", filename_date):
        raise ValueError(f"local filename date must be YYYYMMDD: {filename_date}")
    filename_subject = required_string(subject, "filename.subject")
    filename_summary = required_string(summary, "filename.summary")
    return MeetingPlanFilename(
        subject=filename_subject,
        summary=filename_summary,
        stem=build_filename_stem(filename_date, filename_subject, filename_type, filename_summary)
        if filename_date
        else "",
    )


def string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"meeting plan {label} must be an array")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = clean_string(item)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def clean_string(value: Any) -> str:
    return " ".join(str(value or "").split())


def required_string(value: Any, label: str) -> str:
    text = clean_string(value)
    if not text:
        raise ValueError(f"meeting plan JSON missing {label}")
    return text


def validate_file_url(value: str, label: str, *, allow_empty: bool = False) -> None:
    if allow_empty and not value:
        return
    # scheme/root は検証する。存在は金庫移動を許容し fatal でなく skip+警告記録にする。
    path = file_url_path(value, label)
    if not path.is_file():
        logger.warning(
            "meeting plan file reference unavailable: reason=missing_file count=1 label=%s",
            label,
        )


MARKDOWN_CLIENT_LINK_RE = re.compile(r"\]\((?P<target>[^)]+)\)")


def validate_client_url_from_overview(client_url: str, clients_overview_text: str) -> None:
    cleaned_client_url = clean_string(client_url)
    if not cleaned_client_url:
        return
    client_path = file_url_path(cleaned_client_url, "client_url")
    if not client_path.is_file():
        logger.warning(
            "meeting plan file reference unavailable: reason=missing_file count=1 label=client_url"
        )
    # 存在は許容(skip+警告)。overview掲載チェックは維持する。
    listed_paths = overview_client_link_paths(clients_overview_text)
    if str(client_path) not in listed_paths:
        raise ValueError(
            f"meeting plan client_url must be listed in clients overview: {cleaned_client_url}"
        )


def overview_client_link_paths(clients_overview_text: str) -> set[str]:
    paths: set[str] = set()
    for link_target in MARKDOWN_CLIENT_LINK_RE.findall(clients_overview_text):
        path = overview_client_link_path(link_target)
        if path is not None:
            paths.add(str(path))
    return paths


def overview_client_link_path(link_target: str) -> Path | None:
    target = clean_string(link_target)
    if not target or "..." in target:
        return None
    parsed = urlparse(target)
    if parsed.scheme and parsed.scheme != "file":
        return None
    if parsed.scheme == "file":
        try:
            path = file_url_path(target, "client_url")
        except ValueError:
            return None
    else:
        if parsed.netloc or not parsed.path:
            return None
        path = Path(unquote(parsed.path))
        if not path.is_absolute():
            path = Path(CLIENTS_OVERVIEW_SOURCE_PATH).parent / path
    path = path.resolve(strict=False)
    if not path.is_absolute():
        return None
    if not is_relative_to(path, REFERENCE_FILE_ROOT):
        return None
    return path


def file_url_path(value: str, label: str) -> Path:
    if "..." in value:
        raise ValueError(f"meeting plan {label} must be a real file URL, not a placeholder")
    parsed = urlparse(value)
    if parsed.scheme != "file" or parsed.netloc or not Path(unquote(parsed.path)).is_absolute():
        raise ValueError(f"meeting plan {label} must be a absolute local file URL")
    path = Path(unquote(parsed.path)).resolve()
    allowed_roots = (REFERENCE_FILE_ROOT, TRANSCRIPTION_TERMS_DIR)
    if not any(is_relative_to(path, root) for root in allowed_roots):
        allowed = ", ".join(str(root) for root in allowed_roots)
        raise ValueError(f"meeting plan {label} must point under one of: {allowed}")
    return path


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


__all__ = [
    "CLIENTS_OVERVIEW_SOURCE_PATH",
    "MEETING_TYPES",
    "MeetingPlan",
    "MeetingPlanFilename",
    "MeetingPlanMeetingContext",
    "MeetingPlanSpeaker",
    "build_meeting_plan_filename",
    "meeting_plan_output_schema",
    "parse_meeting_plan_json",
    "validate_client_url_from_overview",
]
