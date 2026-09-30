from __future__ import annotations

import dataclasses
import re

from .circleback_client import TranscriptDoc, Utterance, format_timestamp, timestamp_to_seconds


def normalize_transcript_spacing(transcript: TranscriptDoc) -> TranscriptDoc:
    return dataclasses.replace(
        transcript,
        attendees=tuple(
            dataclasses.replace(attendee, name=normalize_text_spacing(attendee.name))
            for attendee in transcript.attendees
        ),
        utterances=tuple(
            dataclasses.replace(
                utterance,
                speaker=normalize_text_spacing(utterance.speaker),
                text=normalize_text_spacing(utterance.text),
            )
            for utterance in transcript.utterances
        ),
    )


def normalize_text_spacing(value: str) -> str:
    parts = re.split(r"(\s+)", value.strip())
    normalized: list[str] = []
    previous_token = ""
    pending_space = False
    for part in parts:
        if not part:
            continue
        if part.isspace():
            pending_space = True
            continue
        if pending_space and should_keep_space(previous_token, part):
            normalized.append(" ")
        normalized.append(part)
        previous_token = part
        pending_space = False
    return "".join(normalized)


_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+@:/-]*")
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff々〆〤]")
_UNREADABLE_MARKER_RE = re.compile(r"⟦U\+([0-9A-F]{4})⟧")
_LEGEND_ROW_RE = re.compile(r"\| (?P<token>S\d+) \| (?P<raw>.*) \|$")
_JUDGE_INPUT_UTTERANCE_RE = re.compile(
    r"^\[line_id=[1-9]\d*\] \[(?P<timestamp>(?:\d{2}:\d{2}(?::\d{2})?)?)\] "
    r"(?P<token>S\d+): (?P<text>.*)$"
)


def should_keep_space(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if _CJK_RE.search(left) or _CJK_RE.search(right):
        return False
    return bool(_ASCII_TOKEN_RE.fullmatch(left) and _ASCII_TOKEN_RE.fullmatch(right))


def build_speaker_legend(transcript: TranscriptDoc) -> tuple[tuple[str, str], ...]:
    speaker_tokens: dict[str, str] = {}
    for utterance in transcript.utterances:
        if utterance.speaker not in speaker_tokens:
            speaker_tokens[utterance.speaker] = f"S{len(speaker_tokens) + 1}"
    return tuple((token, raw_label) for raw_label, token in speaker_tokens.items())


def mark_unreadable_text(text: str) -> str:
    marked: list[str] = []
    for character in text:
        codepoint = ord(character)
        if (
            character == "⟦"
            or character == "\ufffd"
            or (codepoint <= 0x1F and character != "\t")
            or 0x7F <= codepoint <= 0x9F
        ):
            marked.append(f"⟦U+{codepoint:04X}⟧")
        else:
            marked.append(character)
    return "".join(marked)


def unmark_unreadable_text(text: str) -> str:
    return _UNREADABLE_MARKER_RE.sub(lambda match: chr(int(match.group(1), 16)), text)


def render_judge_input_markdown(
    transcript: TranscriptDoc, legend: tuple[tuple[str, str], ...]
) -> str:
    speaker_tokens = {raw_label: token for token, raw_label in legend}
    participant_lines = [
        f"- {attendee.name}" + (f" <{attendee.email}>" if attendee.email else "")
        for attendee in transcript.attendees
    ]
    utterance_lines = [
        f"[line_id={line_id}] [{format_timestamp(utterance.seconds)}] "
        f"{speaker_tokens[utterance.speaker]}: "
        f"{mark_unreadable_text(utterance.text)}"
        for line_id, utterance in enumerate(transcript.utterances, start=1)
    ]
    return "\n".join(
        (
            "| token | raw |",
            "| --- | --- |",
            *(f"| {token} | {raw_label} |" for token, raw_label in legend),
            "",
            "# Meeting Metadata",
            "",
            f"- title: {transcript.title}",
            f"- started_at: {transcript.started_at or ''}",
            f"- circleback_id: {transcript.meeting_id}",
            f"- circleback_key: {transcript.key}",
            "- participants:",
            *(participant_lines or ["  - なし"]),
            "",
            "# Transcript",
            "",
            *utterance_lines,
        )
    )


def legend_payload(legend: tuple[tuple[str, str], ...]) -> list[dict[str, str]]:
    return [{"token": token, "raw": raw_label} for token, raw_label in legend]


def restore_utterances_from_judge_input(markdown: str) -> tuple[Utterance, ...]:
    legend = {
        match.group("token"): match.group("raw")
        for line in markdown.splitlines()
        if (match := _LEGEND_ROW_RE.fullmatch(line))
    }
    return tuple(
        Utterance(
            seconds=timestamp_to_seconds(match.group("timestamp")),
            timestamp=match.group("timestamp"),
            speaker=legend[match.group("token")],
            text=unmark_unreadable_text(match.group("text")),
        )
        for line in markdown.splitlines()
        if (match := _JUDGE_INPUT_UTTERANCE_RE.fullmatch(line))
    )
