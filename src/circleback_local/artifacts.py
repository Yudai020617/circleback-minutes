from __future__ import annotations

import dataclasses
import json
import re
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .circleback_client import (
    Attendee,
    MeetingCandidate,
    MeetingMetadata,
    TranscriptDoc,
    Utterance,
    format_timestamp,
    timestamp_to_seconds,
)
from .distill import DistilledMinutes, format_transcript
from .judgment import MeetingJudgment
from .log_safety import short_log_detail
from .paths import DEFAULT_ARTIFACT_ROOT, DEFAULT_STATE_PATH, sanitize_filename_stem
from .planning import (
    MEETING_SHARE_VALUES,
    MeetingPlan,
    MeetingPlanAdditionalReference,
    MeetingPlanFilename,
    MeetingPlanMeetingContext,
    MeetingPlanSpeaker,
)
from .restoration import (
    ReferenceText,
    TranscriptRestoration,
    format_reference_texts,
    meeting_plan_prompt_payload,
)


def default_artifact_root(state_path: Path = DEFAULT_STATE_PATH) -> Path:
    if state_path == DEFAULT_STATE_PATH:
        return DEFAULT_ARTIFACT_ROOT
    return state_path.parent / "meetings"


class MeetingArtifacts:
    def __init__(self, root: Path, meeting_id: str) -> None:
        self.root = root
        self.meeting_id = meeting_id
        self.path = root / sanitize_filename_stem(meeting_id)

    def reset(self) -> None:
        root = self.root.resolve(strict=False)
        target = self.path.resolve(strict=False)
        if target == root or root not in target.parents:
            raise ValueError(f"Refusing to reset artifact path outside root: {self.path}")
        if self.path.exists():
            if self.path.is_symlink() or not self.path.is_dir():
                raise ValueError(f"Refusing to reset non-directory artifact path: {self.path}")
            shutil.rmtree(self.path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.path.chmod(0o700)

    def write_status(self, stage: str, payload: dict[str, Any] | None = None) -> None:
        self.write_json(
            "status.json",
            {
                "meeting_id": self.meeting_id,
                "stage": stage,
                **(payload or {}),
            },
        )

    def save_circleback(
        self,
        *,
        candidate: MeetingCandidate,
        metadata: MeetingMetadata,
        raw_transcript: TranscriptDoc,
        preprocessed_transcript: TranscriptDoc,
        judge_input_markdown: str,
        legend: list[dict[str, str]],
    ) -> None:
        self.write_json(
            "01_circleback/metadata.json",
            {
                "candidate": meeting_candidate_payload(candidate),
                "metadata": meeting_metadata_payload(metadata),
                "raw": transcript_payload(raw_transcript),
                "preprocessed": transcript_payload(preprocessed_transcript),
            },
        )
        self.write_text("01_circleback/raw_transcript.md", transcript_markdown(raw_transcript))
        self.write_text(
            "01_circleback/preprocessed_transcript.md", transcript_markdown(preprocessed_transcript)
        )
        self.write_text("01_circleback/judge_input.md", judge_input_markdown)
        self.write_json("01_circleback/legend.json", legend)
        self.write_status("01_circleback")

    def save_judgment(
        self,
        *,
        plan: MeetingPlan,
        edits: Iterable[dict[str, Any]],
        doubt_flags: Iterable[dict[str, Any]],
        speaker_names_by_token: Mapping[str, str],
        edited_transcript: TranscriptDoc | None = None,
    ) -> None:
        self.write_json("02_judgment/meeting_plan_full.json", meeting_plan_payload(plan))
        self.write_json(
            "02_judgment/edit_instructions.json",
            {
                "speaker_names_by_token": dict(speaker_names_by_token),
                "edits": list(edits),
                "doubt_flags": list(doubt_flags),
            },
        )
        if edited_transcript is not None:
            self.save_edited_transcript(edited_transcript)
        self.write_status("02_judgment", {"client": plan.client, "meeting_type": plan.meeting_type})

    def save_edited_transcript(self, edited_transcript: TranscriptDoc) -> None:
        self.write_text("02_judgment/edited_transcript.md", transcript_markdown(edited_transcript))

    def save_restoration_inputs(
        self,
        *,
        transcript: TranscriptDoc,
        plan: MeetingPlan,
        reference_texts: Iterable[ReferenceText | str],
    ) -> None:
        references = tuple(reference_texts)
        self.write_text("03_restoration/original_transcript.md", transcript_markdown(transcript))
        self.write_json(
            "03_restoration/meeting_plan.json", meeting_plan_prompt_payload(transcript, plan)
        )
        self.write_text(
            "03_restoration/reference_texts.md", format_reference_texts(references) + "\n"
        )
        self.write_status("03_restoration_inputs")

    def save_restoration_output(self, restoration: TranscriptRestoration) -> None:
        self.write_text(
            "03_restoration/restored_transcript.md",
            restoration.restored_markdown or transcript_markdown(restoration.transcript),
        )
        self.write_json(
            "03_restoration/restoration_result.json",
            {
                "status": restoration.status,
                "error": restoration.error,
                "needs_review_items": [
                    dataclasses.asdict(item) for item in restoration.needs_review_items
                ],
                "terms_candidates": [
                    dataclasses.asdict(item) for item in restoration.terms_candidates
                ],
            },
        )
        self.write_status(
            "03_restoration_output",
            {
                "status": restoration.status,
                "needs_review_count": len(restoration.needs_review_items),
                "terms_candidate_count": len(restoration.terms_candidates),
            },
        )

    def save_transcript_stages(
        self,
        *,
        original_transcript: TranscriptDoc,
        restored_transcript: TranscriptDoc,
        stored_transcript_markdown: str | None = None,
    ) -> None:
        self.write_text(
            "04_store/transcripts/01_original_transcript.md",
            transcript_markdown(original_transcript),
        )
        self.write_text(
            "04_store/transcripts/02_restored_transcript.md",
            transcript_markdown(restored_transcript),
        )
        if stored_transcript_markdown is not None:
            self.write_text(
                "04_store/transcripts/03_saved_transcript.md", stored_transcript_markdown
            )
        self.write_status("04_transcripts")

    def save_minutes(
        self, *, minutes: DistilledMinutes, stored_minutes_markdown: str | None = None
    ) -> None:
        self.write_text("04_store/minutes_writer_output.md", minutes.markdown)
        if stored_minutes_markdown is not None:
            self.write_text("04_store/saved_minutes.md", stored_minutes_markdown)
        self.write_json(
            "04_store/minutes_metadata.json",
            {
                "source_key": minutes.source_key,
                "title": minutes.title,
                "distiller": minutes.distiller,
            },
        )
        self.write_status("04_minutes", {"distiller": minutes.distiller})

    def save_store_result(self, result: dict[str, Any]) -> None:
        self.write_json("04_store/store_result.json", result)
        self.write_status("04_store", {"operation": result.get("operation")})

    def write_failure(self, stage: str, exc: BaseException) -> None:
        self.write_status(
            "failed",
            {
                "failed_stage": stage,
                "error_type": type(exc).__name__,
                "error_summary": safe_error_summary(exc),
            },
        )

    def load_preprocessed_transcript(self) -> TranscriptDoc:
        metadata_payload = self.read_json("01_circleback/metadata.json")
        preprocessed = (
            metadata_payload.get("preprocessed") if isinstance(metadata_payload, dict) else None
        )
        if not isinstance(preprocessed, dict):
            raise ValueError(f"artifact missing preprocessed transcript metadata: {self.path}")
        transcript_markdown_text = self.read_text("01_circleback/preprocessed_transcript.md")
        utterances = tuple(parse_artifact_transcript_utterances(transcript_markdown_text))
        if not utterances:
            raise ValueError(f"artifact preprocessed transcript has no utterances: {self.path}")
        return TranscriptDoc(
            meeting_id=require_artifact_string(preprocessed.get("meeting_id"), "meeting_id"),
            title=require_artifact_string(preprocessed.get("title"), "title"),
            started_at=optional_string(preprocessed.get("started_at")),
            duration_seconds=optional_float(preprocessed.get("duration_seconds")),
            attendees=tuple(
                attendee_from_payload(item)
                for item in preprocessed.get("attendees") or ()
                if isinstance(item, dict)
            ),
            utterances=utterances,
        )

    def load_restored_transcript(self) -> TranscriptDoc:
        metadata_payload = self.read_json("01_circleback/metadata.json")
        metadata = metadata_payload.get("metadata") if isinstance(metadata_payload, dict) else None
        if not isinstance(metadata, dict):
            raise ValueError(f"artifact missing Circleback metadata: {self.path}")
        transcript_markdown_text = self.read_text("04_store/transcripts/02_restored_transcript.md")
        utterances = tuple(parse_artifact_transcript_utterances(transcript_markdown_text))
        if not utterances:
            raise ValueError(f"artifact restored transcript has no utterances: {self.path}")
        return TranscriptDoc(
            meeting_id=require_artifact_string(metadata.get("meeting_id"), "meeting_id"),
            title=require_artifact_string(metadata.get("title"), "title"),
            started_at=optional_string(metadata.get("started_at")),
            duration_seconds=optional_float(metadata.get("duration_seconds")),
            attendees=tuple(
                attendee_from_payload(item)
                for item in metadata.get("attendees") or ()
                if isinstance(item, dict)
            ),
            utterances=utterances,
        )

    def load_plan(self) -> MeetingPlan:
        payload = self.read_json("02_judgment/meeting_plan_full.json")
        if not isinstance(payload, dict):
            raise ValueError(f"artifact meeting plan must be an object: {self.path}")
        return meeting_plan_from_payload(payload)

    def load_judgment(self) -> MeetingJudgment:
        payload = self.read_json("02_judgment/edit_instructions.json")
        if not isinstance(payload, dict):
            raise ValueError(f"artifact edit instructions must be an object: {self.path}")
        edits = payload.get("edits")
        doubt_flags = payload.get("doubt_flags")
        speaker_names_by_token = payload.get("speaker_names_by_token")
        if (
            not isinstance(edits, list)
            or not all(isinstance(item, dict) for item in edits)
            or not isinstance(doubt_flags, list)
            or not all(isinstance(item, dict) for item in doubt_flags)
            or not isinstance(speaker_names_by_token, dict)
        ):
            raise ValueError(f"artifact edit instructions are invalid: {self.path}")
        return MeetingJudgment(
            plan=self.load_plan(),
            edits=tuple(edits),
            doubt_flags=tuple(doubt_flags),
            speaker_names_by_token={
                str(token): str(name) for token, name in speaker_names_by_token.items()
            },
        )

    def read_json(self, relative_path: str) -> Any:
        return json.loads(self.read_text(relative_path))

    def read_text(self, relative_path: str) -> str:
        path = self.path / relative_path
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise FileNotFoundError(f"missing artifact file: {path}") from None

    def write_json(self, relative_path: str, payload: Any) -> Path:
        return self.write_text(
            relative_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        )

    def write_text(self, relative_path: str, text: str) -> Path:
        path = self.path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        return path


def meeting_artifacts(
    meeting_id: str, *, artifact_root: Path | None, state_path: Path
) -> MeetingArtifacts:
    return MeetingArtifacts(artifact_root or default_artifact_root(state_path), meeting_id)


def safe_error_summary(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {short_log_detail(str(exc), limit=500)}"


def meeting_candidate_payload(candidate: MeetingCandidate) -> dict[str, Any]:
    return {
        "meeting_id": candidate.lookup_id,
        "source_key": candidate.key,
        "title": candidate.title,
        "started_at": candidate.started_at,
    }


def meeting_metadata_payload(metadata: MeetingMetadata) -> dict[str, Any]:
    return {
        "meeting_id": metadata.meeting_id,
        "title": metadata.title,
        "started_at": metadata.started_at,
        "duration_seconds": metadata.duration_seconds,
        "attendees": [dataclasses.asdict(attendee) for attendee in metadata.attendees],
    }


def transcript_payload(transcript: TranscriptDoc) -> dict[str, Any]:
    return {
        "meeting_id": transcript.meeting_id,
        "source_key": transcript.key,
        "title": transcript.title,
        "date": transcript.date,
        "started_at": transcript.started_at,
        "duration_seconds": transcript.duration_seconds,
        "attendees": [dataclasses.asdict(attendee) for attendee in transcript.attendees],
        "utterance_count": len(transcript.utterances),
    }


def transcript_markdown(transcript: TranscriptDoc) -> str:
    return "\n".join(
        (
            f"# {transcript.title}",
            "",
            f"- meeting_id: {transcript.meeting_id}",
            f"- source_key: {transcript.key}",
            f"- date: {transcript.date}",
            "",
            "## Transcript",
            "",
            format_transcript(transcript),
            "",
        )
    )


def parse_artifact_transcript_utterances(markdown: str) -> list[Utterance]:
    lines = markdown.splitlines()
    try:
        start = lines.index("## Transcript") + 1
    except ValueError as exc:
        raise ValueError("artifact transcript missing ## Transcript") from exc
    utterances: list[Utterance] = []
    for raw_line in lines[start:]:
        line = raw_line.strip()
        if not line:
            continue
        parsed = parse_artifact_bracket_utterance(line)
        if parsed is None:
            raise ValueError("artifact transcript line must be '[00:00] speaker: text' format")
        utterances.append(parsed)
    return utterances


ARTIFACT_BRACKET_TRANSCRIPT_LINE_RE = re.compile(
    r"^\[(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\]\s*(?P<speaker>[^:：]{1,80})[:：]\s*(?P<text>.+)$"
)


def parse_artifact_bracket_utterance(line: str) -> Utterance | None:
    match = ARTIFACT_BRACKET_TRANSCRIPT_LINE_RE.match(line)
    if match is None:
        return None
    seconds = timestamp_to_seconds(match.group("time"))
    return Utterance(
        seconds=seconds,
        timestamp=format_timestamp(seconds) if seconds is not None else match.group("time"),
        speaker=match.group("speaker").strip(),
        text=match.group("text").strip(),
    )


def meeting_plan_from_payload(payload: dict[str, Any]) -> MeetingPlan:
    filename = payload.get("filename")
    context = payload.get("meeting_context")
    if not isinstance(filename, dict) or not isinstance(context, dict):
        raise ValueError("artifact meeting plan missing filename or meeting_context")
    share = payload.get("share")
    if not isinstance(share, str) or share not in MEETING_SHARE_VALUES:
        raise ValueError("artifact meeting plan share must be open or confidential")
    return MeetingPlan(
        client=str(payload.get("client") or ""),
        client_url=str(payload.get("client_url") or ""),
        meeting_type=require_artifact_string(payload.get("meeting_type"), "meeting_type"),
        related_meetings_found=int(payload.get("related_meetings_found") or 0),
        speakers=tuple(
            speaker_from_payload(item)
            for item in payload.get("speakers") or ()
            if isinstance(item, dict)
        ),
        meeting_context=MeetingPlanMeetingContext(
            overview=str(context.get("overview") or ""),
            main_topics=tuple(
                str(item) for item in context.get("main_topics") or () if str(item).strip()
            ),
            context_keywords=tuple(
                str(item) for item in context.get("context_keywords") or () if str(item).strip()
            ),
        ),
        additional_references=tuple(
            MeetingPlanAdditionalReference(
                url=str(item.get("url") or ""), hint=str(item.get("hint") or "")
            )
            for item in payload.get("additional_references") or ()
            if isinstance(item, dict)
        ),
        reference_file_urls=tuple(
            str(item) for item in payload.get("reference_file_urls") or () if str(item).strip()
        ),
        filename=MeetingPlanFilename(
            subject=require_artifact_string(filename.get("subject"), "filename.subject"),
            summary=require_artifact_string(filename.get("summary"), "filename.summary"),
            stem=str(filename.get("stem") or ""),
        ),
        share=share,
    )


def speaker_from_payload(payload: dict[str, Any]) -> MeetingPlanSpeaker:
    return MeetingPlanSpeaker(
        name=str(payload.get("name") or ""),
        raw=tuple(str(item) for item in payload.get("raw") or () if str(item).strip()),
        url=str(payload.get("url") or ""),
    )


def attendee_from_payload(payload: dict[str, Any]) -> Attendee:
    return Attendee(
        name=str(payload.get("name") or ""), email=optional_string(payload.get("email"))
    )


def optional_string(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def require_artifact_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"artifact {label} must be a non-empty string")
    return value


def optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def meeting_plan_payload(plan: MeetingPlan) -> dict[str, Any]:
    return {
        "client": plan.client,
        "client_url": plan.client_url,
        "meeting_type": plan.meeting_type,
        "share": plan.share,
        "speakers": [dataclasses.asdict(speaker) for speaker in plan.speakers],
        "meeting_context": dataclasses.asdict(plan.meeting_context),
        "additional_references": [
            dataclasses.asdict(reference) for reference in plan.additional_references
        ],
        "reference_file_urls": list(plan.reference_file_urls),
        "filename": dataclasses.asdict(plan.filename),
        "related_meetings_found": plan.related_meetings_found,
    }
