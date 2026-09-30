from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import subprocess
import time
from typing import Any

from .models import circleback_source_key

DEFAULT_CIRCLEBACK_BIN = "circleback"
JST = dt.timezone(dt.timedelta(hours=9), name="JST")
HTTP_5XX_RETRY_DELAYS_SECONDS = (0.25, 0.5)
HTTP_5XX_STATUS_PATTERN = re.compile(
    r"(?:http|status(?: code)?)\s*[:=]?\s*(5\d{2})\b", re.IGNORECASE
)


@dataclasses.dataclass(frozen=True)
class MeetingCandidate:
    lookup_id: str
    title: str = ""
    started_at: str | None = None

    @property
    def meeting_id(self) -> str:
        return self.lookup_id

    @property
    def key(self) -> str:
        return circleback_source_key(self.lookup_id)


@dataclasses.dataclass(frozen=True)
class Attendee:
    name: str
    email: str | None = None


@dataclasses.dataclass(frozen=True)
class MeetingMetadata:
    meeting_id: str
    title: str | None
    started_at: str | None
    duration_seconds: float | None
    attendees: tuple[Attendee, ...] = ()


@dataclasses.dataclass(frozen=True)
class Utterance:
    seconds: int | None
    timestamp: str
    speaker: str
    text: str


@dataclasses.dataclass(frozen=True)
class TranscriptDoc:
    meeting_id: str
    title: str
    started_at: str | None
    duration_seconds: float | None
    utterances: tuple[Utterance, ...]
    attendees: tuple[Attendee, ...] = ()

    @property
    def key(self) -> str:
        return circleback_source_key(self.meeting_id)

    @property
    def date(self) -> str:
        if not self.started_at:
            raise ValueError(f"Circleback transcript started_at is required: {self.meeting_id}")
        try:
            parsed = dt.datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"invalid Circleback transcript started_at: {self.started_at}"
            ) from exc
        if parsed.tzinfo is None:
            return parsed.date().isoformat()
        return parsed.astimezone(JST).date().isoformat()

    def with_metadata(self, metadata: MeetingMetadata) -> TranscriptDoc:
        if not metadata.title:
            raise ValueError(f"Circleback meeting metadata missing title: {metadata.meeting_id}")
        if not metadata.started_at:
            raise ValueError(
                f"Circleback meeting metadata missing started_at: {metadata.meeting_id}"
            )
        return dataclasses.replace(
            self,
            meeting_id=metadata.meeting_id,
            title=metadata.title,
            started_at=metadata.started_at,
            duration_seconds=metadata.duration_seconds,
            attendees=metadata.attendees,
        )


COMMAND_FAILURE_CATEGORIES = frozenset(
    {"network", "auth_expired", "rate_limit", "http_5xx", "cli_validation", "unknown_nonzero"}
)


class CirclebackCommandError(RuntimeError):
    def __init__(self, message: str, *, category: str = "unknown_nonzero") -> None:
        if category not in COMMAND_FAILURE_CATEGORIES:
            raise ValueError(f"unknown Circleback command failure category: {category}")
        super().__init__(message)
        self.category = category


class CirclebackRateLimitError(CirclebackCommandError):
    def __init__(self, message: str) -> None:
        super().__init__(message, category="rate_limit")


COMMAND_FAILURE_PATTERNS = (
    (
        "rate_limit",
        re.compile(
            r"\brate[ _-]?limit|too many requests|"
            r"(?:http|status(?: code)?)\s*[:=]?\s*429\b"
        ),
    ),
    (
        "auth_expired",
        re.compile(
            r"unauthori[sz]ed|forbidden|not (?:logged|signed) in|"
            r"(?:token|session) (?:has )?expired|authentication (?:failed|required)|"
            r"(?:http|status(?: code)?)\s*[:=]?\s*(?:401|403)\b"
        ),
    ),
    (
        "http_5xx",
        re.compile(
            r"(?:http|status(?: code)?)\s*[:=]?\s*5\d{2}\b|internal server error|"
            r"bad gateway|service unavailable|gateway timeout"
        ),
    ),
    (
        "cli_validation",
        re.compile(
            r"unknown (?:argument|command|option)|invalid (?:argument|command|option|value)|"
            r"missing required|usage:"
        ),
    ),
    (
        "network",
        re.compile(
            r"\b(?:econnrefused|econnreset|enotfound|etimedout)\b|dns|fetch failed|"
            r"network (?:error|failure)|unable to connect|"
            r"connection (?:refused|reset|timed out)|socket hang up"
        ),
    ),
)


def classify_command_failure(stdout: str, stderr: str) -> str:
    process_output = f"{stdout}\n{stderr}".casefold()
    for category, pattern in COMMAND_FAILURE_PATTERNS:
        if pattern.search(process_output):
            return category
    return "unknown_nonzero"


class CirclebackClient:
    def __init__(
        self, circleback_bin: str = DEFAULT_CIRCLEBACK_BIN, timeout_seconds: int = 180
    ) -> None:
        self.circleback_bin = circleback_bin
        self.timeout_seconds = timeout_seconds

    def search_meetings(
        self,
        *,
        query: str | None = None,
        last_days: int | None = 14,
        from_date: str | None = None,
        to_date: str | None = None,
        page: int = 1,
        pages: int | None = None,
        limit: int | None = 20,
    ) -> list[MeetingCandidate]:
        candidates: list[MeetingCandidate] = []
        max_page = page + pages if pages is not None else page + 1
        for page_number in range(page, max_page):
            command = [self.circleback_bin, "meetings", "search", "--json"]
            if pages is not None or page != 1:
                command.extend(["-p", str(page_number)])
            if last_days is not None:
                command.extend(["--last", str(last_days)])
            if from_date:
                command.extend(["--from", from_date])
            if to_date:
                command.extend(["--to", to_date])
            if query:
                command.append(query)

            items = _as_items(self._run_json(command))
            if not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                response_id = item.get("id")
                lookup_id = clean_text(response_id) if isinstance(response_id, str) else ""
                if not lookup_id:
                    raise ValueError(
                        "Circleback meeting search result missing canonical meeting id"
                    )
                candidates.append(
                    MeetingCandidate(
                        lookup_id=lookup_id,
                        title=clean_text(item.get("title") or item.get("name") or ""),
                        started_at=optional_clean_text(
                            item.get("startedAt") or item.get("createdAt")
                        ),
                    )
                )
                if limit is not None and len(candidates) >= limit:
                    return sort_candidates_date_desc(candidates)
        return sort_candidates_date_desc(candidates)

    def read_meeting(self, meeting_id: str) -> MeetingMetadata:
        item = _first_object(
            self._run_json([self.circleback_bin, "meetings", "read", "--json", meeting_id])
        )
        if item is None:
            raise ValueError(f"Circleback meeting read returned no results: {meeting_id}")
        raise_if_cli_validation_error(item, command_name="meeting read", meeting_id=meeting_id)
        response_id = item.get("id")
        canonical_meeting_id = clean_text(response_id) if isinstance(response_id, str) else ""
        title = optional_clean_text(item.get("title") or item.get("name"))
        started_at = optional_clean_text(item.get("startedAt") or item.get("createdAt"))
        if not canonical_meeting_id:
            raise ValueError(
                f"Circleback meeting metadata missing canonical meeting id: {meeting_id}"
            )
        if not title:
            raise ValueError(f"Circleback meeting metadata missing title: {meeting_id}")
        if not started_at:
            raise ValueError(f"Circleback meeting metadata missing started_at: {meeting_id}")
        return MeetingMetadata(
            meeting_id=canonical_meeting_id,
            title=title,
            started_at=started_at,
            duration_seconds=duration_to_seconds(
                item.get("duration") or item.get("durationSeconds")
            ),
            attendees=parse_attendees(item.get("attendees") or item.get("participants")),
        )

    def read_transcript(self, candidate: MeetingCandidate | str) -> TranscriptDoc:
        meeting_id = candidate.lookup_id if isinstance(candidate, MeetingCandidate) else candidate
        item = _first_object(
            self._run_json([self.circleback_bin, "transcripts", "read", "--json", meeting_id])
        )
        if item is None:
            raise ValueError(f"Circleback transcript read returned no results: {meeting_id}")
        raise_if_cli_validation_error(item, command_name="transcript read", meeting_id=meeting_id)

        transcript = (
            item.get("transcript")
            or item.get("utterances")
            or item.get("segments")
            or item.get("entries")
            or item.get("text")
        )
        return TranscriptDoc(
            meeting_id=meeting_id,
            title=clean_text(
                item.get("title") or item.get("meetingName") or item.get("name") or ""
            ),
            started_at=optional_clean_text(item.get("startedAt") or item.get("createdAt")),
            duration_seconds=duration_to_seconds(
                item.get("duration") or item.get("durationSeconds")
            ),
            utterances=tuple(parse_transcript(transcript)),
            attendees=parse_attendees(item.get("attendees") or item.get("participants")),
        )

    def fetch_transcript_with_metadata(self, candidate: MeetingCandidate) -> TranscriptDoc:
        metadata = self.read_meeting(candidate.lookup_id)
        return self.read_transcript(candidate).with_metadata(metadata)

    def _run_json(self, command: list[str]) -> Any:
        for attempt in range(1, len(HTTP_5XX_RETRY_DELAYS_SECONDS) + 2):
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            if result.returncode == 0 and "rate limit" in result.stdout.casefold():
                raise CirclebackRateLimitError(
                    f"Circleback rate limited: {' '.join(command)}; "
                    f"stdout_bytes={len(result.stdout.encode('utf-8'))}; "
                    f"stderr_bytes={len(result.stderr.encode('utf-8'))}"
                )
            if result.returncode != 0:
                category = classify_command_failure(result.stdout, result.stderr)
                if category == "http_5xx" and attempt <= len(HTTP_5XX_RETRY_DELAYS_SECONDS):
                    time.sleep(HTTP_5XX_RETRY_DELAYS_SECONDS[attempt - 1])
                    continue
                failure_detail = ""
                if category == "http_5xx":
                    failure_detail = f"; attempts={attempt}"
                    status_match = HTTP_5XX_STATUS_PATTERN.search(
                        f"{result.stdout}\n{result.stderr}"
                    )
                    if status_match:
                        failure_detail += f"; http_status={status_match.group(1)}"
                raise CirclebackCommandError(
                    f"command failed: {' '.join(command)}: exit code {result.returncode}; "
                    f"stdout_bytes={len(result.stdout.encode('utf-8'))}; "
                    f"stderr_bytes={len(result.stderr.encode('utf-8'))}{failure_detail}",
                    category=category,
                )
            try:
                return _loads_json_stdout(result.stdout)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"command did not return JSON: {' '.join(command)}; "
                    f"stdout_bytes={len(result.stdout.encode('utf-8'))}; "
                    f"stderr_bytes={len(result.stderr.encode('utf-8'))}"
                ) from exc


def _loads_json_stdout(stdout: str) -> Any:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as original_error:
        for opener in json_opener_positions(stdout):
            try:
                return json.loads(stdout[opener:])
            except json.JSONDecodeError:
                continue
        raise original_error


def json_opener_positions(text: str) -> list[int]:
    return sorted(index for index, char in enumerate(text) if char in "[{")


def raise_if_cli_validation_error(
    item: dict[str, Any], *, command_name: str, meeting_id: str
) -> None:
    if not is_cli_validation_error(item):
        return
    code = clean_text(item.get("code")) or "unknown"
    path = format_validation_path(item.get("path"))
    raise CirclebackCommandError(
        f"Circleback {command_name} returned validation error: "
        f"meeting_id={meeting_id}; code={code}; path={path}",
        category="cli_validation",
    )


def is_cli_validation_error(item: dict[str, Any]) -> bool:
    return {"code", "expected", "received", "path", "message"}.issubset(item)


def format_validation_path(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return "unknown"
    parts = [str(part).strip() for part in value if str(part).strip()]
    return ".".join(parts) or "unknown"


def _as_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("meetings", "transcripts", "results", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return [payload]
    return []


def _first_object(payload: Any) -> dict[str, Any] | None:
    for item in _as_items(payload):
        if isinstance(item, dict):
            return item
    return None


def parse_attendees(value: Any) -> tuple[Attendee, ...]:
    if not isinstance(value, list):
        return ()
    attendees: list[Attendee] = []
    seen: set[tuple[str, str | None]] = set()
    for item in value:
        if isinstance(item, dict):
            name = clean_text(
                item.get("name") or item.get("displayName") or item.get("fullName") or ""
            )
            email = optional_clean_text(item.get("email"))
        elif isinstance(item, str):
            name = clean_text(item)
            email = None
        else:
            continue
        if not name and not email:
            continue
        key = (name.casefold(), email.casefold() if email else None)
        if key in seen:
            continue
        seen.add(key)
        attendees.append(Attendee(name=name or email or "", email=email))
    return tuple(attendees)


def parse_transcript(transcript: Any) -> list[Utterance]:
    if isinstance(transcript, str):
        return parse_text_transcript(transcript)
    if not isinstance(transcript, list):
        raise ValueError("unsupported Circleback transcript shape")

    utterances: list[Utterance] = []
    for item in transcript:
        if not isinstance(item, dict):
            continue
        text = clean_text(item.get("text") or item.get("content") or "")
        if not text:
            continue
        seconds = timestamp_to_seconds(
            item.get("timestamp") or item.get("time") or item.get("start") or item.get("startTime")
        )
        speaker = item.get("speaker") or item.get("speakerName") or item.get("name")
        if isinstance(speaker, dict):
            speaker = speaker.get("name") or speaker.get("displayName")
        speaker_text = clean_text(speaker)
        if not speaker_text:
            raise ValueError("Circleback transcript utterance missing speaker")
        utterances.append(
            Utterance(
                seconds=seconds,
                timestamp=format_timestamp(seconds),
                speaker=speaker_text,
                text=text,
            )
        )
    return utterances


def parse_text_transcript(transcript: str) -> list[Utterance]:
    pattern = re.compile(
        r"^\[?(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\]?\s+(?P<speaker>[^:：]{1,80})[:：]\s*(?P<text>.*)$"
    )
    utterances: list[Utterance] = []
    previous: Utterance | None = None
    for line in transcript.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = pattern.match(stripped)
        if match:
            seconds = timestamp_to_seconds(match.group("time"))
            previous = Utterance(
                seconds=seconds,
                timestamp=format_timestamp(seconds),
                speaker=clean_text(match.group("speaker")),
                text=clean_text(match.group("text")),
            )
            utterances.append(previous)
        elif previous is not None:
            previous = dataclasses.replace(previous, text=clean_text(f"{previous.text} {stripped}"))
            utterances[-1] = previous
        else:
            raise ValueError("Circleback text transcript line missing timestamp and speaker")
    return utterances


def sort_candidates_date_desc(candidates: list[MeetingCandidate]) -> list[MeetingCandidate]:
    return sorted(
        candidates, key=lambda candidate: iso_timestamp(candidate.started_at), reverse=True
    )


def iso_timestamp(value: str | None) -> float:
    if not value:
        return float("-inf")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return float("-inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.timestamp()


def timestamp_to_seconds(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        raw = float(value)
        return int(raw / 1000) if raw > 10000 else int(raw)
    text = str(value).strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) in {2, 3} and all(part.isdigit() for part in parts):
        numbers = [int(part) for part in parts]
        if len(numbers) == 2:
            minutes, seconds = numbers
            return minutes * 60 + seconds
        hours, minutes, seconds = numbers
        return hours * 3600 + minutes * 60 + seconds
    try:
        return int(float(text))
    except ValueError:
        return None


def format_timestamp(seconds: int | None) -> str:
    if seconds is None:
        return ""
    hours, remainder = divmod(max(seconds, 0), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def duration_to_seconds(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    seconds = timestamp_to_seconds(text)
    if seconds is not None:
        return float(seconds)
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else None


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def optional_clean_text(value: Any) -> str | None:
    text = clean_text(value) if value is not None else ""
    return text or None
