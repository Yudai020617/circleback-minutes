from __future__ import annotations

from dataclasses import dataclass, field

CIRCLEBACK_SOURCE_PREFIX = "circleback:"


def circleback_lookup_id(meeting_id: str) -> str:
    text = meeting_id.strip()
    if text.startswith(CIRCLEBACK_SOURCE_PREFIX):
        return text.removeprefix(CIRCLEBACK_SOURCE_PREFIX).strip()
    return text


def circleback_source_key(meeting_id: str) -> str:
    return f"{CIRCLEBACK_SOURCE_PREFIX}{circleback_lookup_id(meeting_id)}"


@dataclass(frozen=True)
class Attendee:
    name: str
    email: str | None = None


@dataclass(frozen=True)
class Utterance:
    timestamp: str
    speaker: str
    text: str


@dataclass(frozen=True)
class Transcript:
    meeting_id: str
    title: str
    date: str
    client: str
    meeting_type: str
    attendees: tuple[Attendee, ...] = ()
    tags: tuple[str, ...] = ()
    utterances: tuple[Utterance, ...] = ()
    duration_seconds: float | None = None

    @property
    def source_key(self) -> str:
        return circleback_source_key(self.meeting_id)


@dataclass(frozen=True)
class Minutes:
    meeting_id: str
    title: str
    date: str
    client: str
    meeting_type: str
    attendees: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    summary: str = ""
    markdown_body: str = ""
    notable_quotes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def source_key(self) -> str:
        return circleback_source_key(self.meeting_id)
