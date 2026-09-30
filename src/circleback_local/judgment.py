from __future__ import annotations

import dataclasses
import datetime as dt
import json
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .circleback_client import TranscriptDoc
from .claude_exec import (
    DEFAULT_CLAUDE_BIN,
    DEFAULT_CLAUDE_EFFORT,
    DEFAULT_CLAUDE_MODEL,
    private_temp_parent,
    run_claude_text,
    write_private_text,
)
from .codex_exec import make_unique_artifact_path
from .edit_apply import (
    EditTargetResolutionError,
    apply_edit_instructions,
    validate_edit_instructions,
)
from .frontmatter import split_frontmatter
from .local_store import StateStore
from .paths import APP_ROOT, BRAIN_ROOT
from .planning import (
    CLIENTS_OVERVIEW_SOURCE_PATH,
    COMMON_TRANSCRIPTION_TERMS_URL,
    MeetingPlan,
    parse_meeting_plan_json,
    validate_client_url_from_overview,
)
from .preprocess import unmark_unreadable_text
from .prompt_templates import render_prompt_template

DEFAULT_JUDGE_TIMEOUT_SECONDS = 3600
JUDGE_REJECTION_ARTIFACT_DIR = APP_ROOT / "var" / "run" / "judge-rejections"


@dataclasses.dataclass(frozen=True)
class MeetingJudgment:
    plan: MeetingPlan
    edits: tuple[dict[str, Any], ...]
    doubt_flags: tuple[dict[str, Any], ...]
    speaker_names_by_token: dict[str, str]


class ClaudeMeetingJudge:
    name = "claude-meeting-judge"

    def __init__(
        self,
        *,
        claude_bin: str = DEFAULT_CLAUDE_BIN,
        timeout_seconds: int = DEFAULT_JUDGE_TIMEOUT_SECONDS,
        model: str = DEFAULT_CLAUDE_MODEL,
        effort: str = DEFAULT_CLAUDE_EFFORT,
    ) -> None:
        self.claude_bin = claude_bin
        self.timeout_seconds = timeout_seconds
        self.model = model
        self.effort = effort

    def judge(
        self,
        judge_input_markdown: str,
        clients_overview_text: str,
        people_index_text: str,
        recent_meetings_index_text: str,
        legend: Mapping[str, str],
    ) -> MeetingJudgment:
        with tempfile.TemporaryDirectory(
            prefix="circleback-judge-input-", dir=private_temp_parent()
        ) as tmp_dir:
            tmp_path = Path(tmp_dir)
            input_paths = {
                "judge_input_path": tmp_path / "judge_input.md",
                "clients_overview_path": tmp_path / "clients-overview.md",
                "people_index_path": tmp_path / "people-index.md",
                "recent_meetings_index_path": tmp_path / "recent-meetings-index.md",
            }
            write_private_text(input_paths["judge_input_path"], judge_input_markdown)
            write_private_text(input_paths["clients_overview_path"], clients_overview_text)
            write_private_text(input_paths["people_index_path"], people_index_text)
            write_private_text(
                input_paths["recent_meetings_index_path"], recent_meetings_index_text
            )
            prompt = render_prompt_template(
                "meeting_judge",
                **input_paths,
                clients_overview_source_path=CLIENTS_OVERVIEW_SOURCE_PATH,
                common_terms_url=COMMON_TRANSCRIPTION_TERMS_URL,
                meetings_root=BRAIN_ROOT / "50_議事録",
            )
            output = run_claude_text(
                prompt,
                claude_bin=self.claude_bin,
                model=self.model,
                effort=self.effort,
                timeout_seconds=self.timeout_seconds,
                failure_label="Claude meeting judgment",
                cwd=tmp_path,
            )
        try:
            judgment = parse_meeting_judgment_json(output, legend)
        except ValueError as exc:
            artifact_path = write_judge_rejection_artifact(output)
            raise RuntimeError(
                f"meeting judge output rejected: {exc} artifacts={artifact_path}"
            ) from exc
        validate_client_url_from_overview(judgment.plan.client_url, clients_overview_text)
        return judgment


def write_judge_rejection_artifact(output: str) -> Path:
    # 復元不能な再現しない出力の診断用にreject原文を退避する(restorer-rejectionsと同型)
    JUDGE_REJECTION_ARTIFACT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = make_unique_artifact_path(
        JUDGE_REJECTION_ARTIFACT_DIR / dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    write_private_text(path / "rejected_output.txt", output)
    return path


def apply_meeting_judgment(
    transcript: TranscriptDoc,
    judgment: MeetingJudgment,
    legend: Mapping[str, str],
) -> TranscriptDoc:
    try:
        utterances = apply_edit_instructions(
            transcript.utterances,
            judgment.edits,
            legend=legend,
            speaker_names_by_token=judgment.speaker_names_by_token,
            unmark=unmark_unreadable_text,
        )
    except EditTargetResolutionError as exc:
        artifact_path = write_judge_rejection_artifact(
            json.dumps(dataclasses.asdict(judgment), ensure_ascii=False)
        )
        write_private_text(
            artifact_path / "target_resolution.json",
            json.dumps(
                {
                    "meeting_id": transcript.meeting_id,
                    "edit_id": exc.edit_id,
                    "line_id": exc.line_id,
                },
                ensure_ascii=False,
            ),
        )
        raise RuntimeError(
            f"meeting judge edits rejected: {exc} artifacts={artifact_path}"
        ) from exc
    return dataclasses.replace(transcript, utterances=utterances)


def strip_markdown_fence(output: str) -> str:
    # 実障害: Fableが禁止指定を無視しJSONを```jsonフェンスで包む(2026-08-11実測2件、judge-rejections退避で確定)
    text = output.strip()
    if text.startswith("```") and text.endswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            return text[first_newline + 1 : -3].strip()
    return text


def parse_meeting_judgment_json(output: str, legend: Mapping[str, str]) -> MeetingJudgment:
    text = strip_markdown_fence(output)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # 実障害: 短い前置き文+完全なJSON(2026-08-11退避2件で確定)。先頭{〜末尾}を切り出して再パース
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("meeting judgment JSON must be an object")
    speakers_payload = payload.get("speakers")
    if not isinstance(speakers_payload, list):
        raise ValueError("meeting judgment speakers must be an array")

    plan_speakers: list[dict[str, Any]] = []
    token_speaker_indexes: list[tuple[str, int]] = []
    for speaker in speakers_payload:
        if not isinstance(speaker, dict):
            raise ValueError("meeting judgment speaker must be an object")
        tokens = speaker.get("tokens")
        if not isinstance(tokens, list) or not tokens:
            raise ValueError("meeting judgment speakers.tokens must be a non-empty array")
        raw: list[str] = []
        for token in tokens:
            if not isinstance(token, str) or not token:
                raise ValueError("meeting judgment speaker token must be a non-empty string")
            if token not in legend:
                raise ValueError(f"unknown speaker token: {token}")
            if any(seen_token == token for seen_token, _ in token_speaker_indexes):
                raise ValueError(f"duplicate speaker token: {token}")
            raw.append(legend[token])
            token_speaker_indexes.append((token, len(plan_speakers)))
        plan_speaker = {key: value for key, value in speaker.items() if key != "tokens"}
        plan_speaker["raw"] = raw
        plan_speakers.append(plan_speaker)

    edits = normalize_edit_instructions(payload.get("edits"))
    doubt_flags = parse_instruction_list(payload.get("doubt_flags"), "doubt_flags")
    plan_payload = dict(payload)
    plan_payload["speakers"] = plan_speakers
    plan = parse_meeting_plan_json(json.dumps(plan_payload, ensure_ascii=False))
    speaker_names_by_token = {
        token: plan.speakers[speaker_index].name for token, speaker_index in token_speaker_indexes
    }
    return MeetingJudgment(
        plan=plan,
        edits=edits,
        doubt_flags=doubt_flags,
        speaker_names_by_token=speaker_names_by_token,
    )


def parse_instruction_list(value: Any, label: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"meeting judgment {label} must be an array of objects")
    return tuple(value)


def normalize_edit_instructions(value: Any) -> tuple[dict[str, Any], ...]:
    edits = parse_instruction_list(value, "edits")
    sortable: list[tuple[int, dict[str, Any]]] = []
    for edit in edits:
        target = edit.get("target")
        if not isinstance(target, dict):
            raise ValueError("edit target must be an object")
        line_id = target.get("line_id")
        if type(line_id) is not int or line_id < 1:
            raise ValueError("target line_id must be a positive integer")
        sortable.append((line_id, edit))
    normalized = [
        {**edit, "id": edit_id}
        for edit_id, (_line_id, edit) in enumerate(
            sorted(sortable, key=lambda item: item[0]), start=1
        )
    ]
    return validate_edit_instructions(normalized)


def build_recent_meetings_index(
    state_path: Path, *, exclude_meeting_id: str = "", per_client_limit: int = 3
) -> str:
    state = StateStore(state_path)
    meetings = state.data.get("meetings")
    if not isinstance(meetings, dict):
        return ""
    entries: list[tuple[str, Path]] = []
    for meeting_id, record in meetings.items():
        if meeting_id == exclude_meeting_id or not isinstance(record, dict):
            continue
        minutes_path_value = record.get("minutes_path")
        if not isinstance(minutes_path_value, str):
            continue
        minutes_path = Path(minutes_path_value)
        if minutes_path.is_file():
            entries.append((str(record.get("slug") or ""), minutes_path.resolve()))

    lines: list[str] = []
    per_client_counts: dict[str, int] = {}
    for _slug, minutes_path in sorted(entries, key=lambda entry: entry[0][:8], reverse=True):
        frontmatter, _body = split_frontmatter(minutes_path.read_text(encoding="utf-8"))
        date = str(frontmatter.get("date") or "")
        client = str(frontmatter.get("client") or "").strip() or "社内"
        if per_client_counts.get(client, 0) >= per_client_limit:
            continue
        per_client_counts[client] = per_client_counts.get(client, 0) + 1
        meeting_type = str(frontmatter.get("type") or "")
        attendees_value = frontmatter.get("attendees")
        attendees = (
            ",".join(str(attendee) for attendee in attendees_value)
            if isinstance(attendees_value, list)
            else ""
        )
        lines.append(
            f"- {date} | client={client} | {meeting_type} | attendees={attendees} | {minutes_path}"
        )
    return "\n".join(lines)


__all__ = [
    "ClaudeMeetingJudge",
    "DEFAULT_JUDGE_TIMEOUT_SECONDS",
    "MeetingJudgment",
    "build_recent_meetings_index",
    "parse_meeting_judgment_json",
]
