from __future__ import annotations

import dataclasses
import datetime as dt
import fcntl
import json
import logging
import re
import tempfile
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

from .circleback_client import (
    TranscriptDoc,
    Utterance,
    clean_text,
    format_timestamp,
    timestamp_to_seconds,
)
from .codex_exec import (
    DEFAULT_CODEX_BIN,
    DEFAULT_CODEX_EFFORT,
    DEFAULT_CODEX_MODEL,
    artifact_slug,
    make_unique_artifact_path,
    private_temp_parent,
    run_codex_text,
    write_private_text,
)
from .distill import format_transcript
from .paths import (
    APP_ROOT,
    BRAIN_ROOT,
    DEFAULT_ARTIFACT_ROOT,
    KNOWLEDGE_DIR,
    REVIEW_INBOX_PATH,
    TERMS_CANDIDATES_PATH,
    sanitize_filename_stem,
)
from .planning import MeetingPlan
from .prompt_templates import render_prompt_template

logger = logging.getLogger(__name__)

DEFAULT_RESTORATION_TIMEOUT_SECONDS = 600
LONG_RESTORER_MIN_UTTERANCES = 500
MAX_RESTORATION_CORE_UTTERANCES = 400
RESTORATION_CONTEXT_UTTERANCES = 100
MAX_PARALLEL_RESTORERS = 4
REVIEW_INBOX_FILENAME = "transcription-review-inbox.md"
TERMS_CANDIDATES_FILENAME = "transcription-terms-candidates.md"
REVIEW_STATUS_PENDING = "要確認"
REVIEW_MARKER = "[要確認]"
JST = dt.timezone(dt.timedelta(hours=9), name="JST")
# Restored markdown lives in a TemporaryDirectory that vanishes when validation
# rejects the Codex output, so persist the rejected output here for diagnosis.
RESTORER_REJECTION_ARTIFACT_DIR = APP_ROOT / "var" / "run" / "restorer-rejections"


@dataclasses.dataclass(frozen=True)
class ReviewItem:
    id: str
    before: str
    after: str
    type: str
    context_block: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", required_inline(self.id, "review item id"))
        object.__setattr__(self, "before", required_inline(self.before, "review item before"))
        object.__setattr__(self, "after", clean_inline(self.after))
        object.__setattr__(self, "type", required_inline(self.type, "review item type"))
        object.__setattr__(self, "context_block", clean_block(self.context_block))


@dataclasses.dataclass(frozen=True)
class TermsCandidate:
    id: str
    target: str
    client: str
    term: str
    variant: str
    cue: str
    caution: str
    evidence: str

    def __post_init__(self) -> None:
        target = required_inline(self.target, "terms candidate target")
        if target not in {"common", "client"}:
            raise ValueError(f"unknown terms candidate target: {target}")
        object.__setattr__(self, "id", required_inline(self.id, "terms candidate id"))
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "client", clean_inline(self.client))
        object.__setattr__(self, "term", required_inline(self.term, "terms candidate term"))
        object.__setattr__(
            self, "variant", required_inline(self.variant, "terms candidate variant")
        )
        object.__setattr__(self, "cue", required_inline(self.cue, "terms candidate cue"))
        object.__setattr__(
            self, "caution", required_inline(self.caution, "terms candidate caution")
        )
        object.__setattr__(
            self, "evidence", required_block(self.evidence, "terms candidate evidence")
        )


@dataclasses.dataclass(frozen=True)
class ReferenceText:
    url: str
    source_type: str
    hint: str
    text: str


@dataclasses.dataclass(frozen=True)
class TranscriptRestoration:
    transcript: TranscriptDoc
    restored_markdown: str
    needs_review_items: tuple[ReviewItem, ...] = ()
    terms_candidates: tuple[TermsCandidate, ...] = ()
    status: str = "ok"
    error: str = ""


@dataclasses.dataclass(frozen=True)
class LongRestorationChunk:
    index: int
    count: int
    core: TranscriptDoc
    context_before: tuple[Utterance, ...]
    context_after: tuple[Utterance, ...]


class TranscriptRestorer(Protocol):
    def restore(
        self,
        transcript: TranscriptDoc,
        plan: MeetingPlan,
        *,
        reference_texts: Iterable[ReferenceText | str],
    ) -> TranscriptRestoration:
        raise NotImplementedError


class CodexTranscriptRestorer:
    name = "codex"

    def __init__(
        self,
        *,
        codex_bin: str = DEFAULT_CODEX_BIN,
        timeout_seconds: int = DEFAULT_RESTORATION_TIMEOUT_SECONDS,
        model: str = DEFAULT_CODEX_MODEL,
        effort: str = DEFAULT_CODEX_EFFORT,
        inline_effort: str | None = None,
    ) -> None:
        self.codex_bin = codex_bin
        self.timeout_seconds = timeout_seconds
        self.model = model
        self.effort = effort
        self.inline_effort = inline_effort

    def restore(
        self,
        transcript: TranscriptDoc,
        plan: MeetingPlan,
        *,
        reference_texts: Iterable[ReferenceText | str],
    ) -> TranscriptRestoration:
        loaded_reference_texts = tuple(reference_texts)
        if len(transcript.utterances) >= LONG_RESTORER_MIN_UTTERANCES:
            return self._restore_long(transcript, plan, loaded_reference_texts)
        return self._restore_inline(
            transcript,
            plan,
            loaded_reference_texts,
            build_transcript_restorer_inline_prompt(transcript, plan, loaded_reference_texts),
            effort=self.inline_effort or self.effort,
        )

    def _restore_long(
        self,
        transcript: TranscriptDoc,
        plan: MeetingPlan,
        reference_texts: tuple[ReferenceText | str, ...],
    ) -> TranscriptRestoration:
        chunks = build_long_restoration_chunks(transcript)
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_RESTORERS, len(chunks))) as executor:
            futures = [
                executor.submit(
                    self._restore_long_chunk,
                    chunk,
                    plan,
                    reference_texts,
                )
                for chunk in chunks
            ]
            restorations = tuple(future.result() for future in futures)
        return merge_long_restorations(transcript, restorations)

    def _restore_long_chunk(
        self,
        chunk: LongRestorationChunk,
        plan: MeetingPlan,
        reference_texts: tuple[ReferenceText | str, ...],
    ) -> TranscriptRestoration:
        restoration = self._restore_inline(
            chunk.core,
            plan,
            reference_texts,
            build_transcript_restorer_chunk_prompt(chunk, plan, reference_texts),
            effort=self.effort,
        )
        return restoration

    def _restore_inline(
        self,
        transcript: TranscriptDoc,
        plan: MeetingPlan,
        reference_texts: tuple[ReferenceText | str, ...],
        prompt: str,
        *,
        effort: str,
    ) -> TranscriptRestoration:
        with tempfile.TemporaryDirectory(
            prefix="circleback-restorer-input-", dir=private_temp_parent()
        ) as tmp_dir:
            tmp_path = Path(tmp_dir)
            restored_transcript_path = tmp_path / "restored_transcript.md"
            output = run_codex_text(
                prompt,
                codex_bin=self.codex_bin,
                model=self.model,
                effort=effort,
                timeout_seconds=self.timeout_seconds,
                output_schema=inline_restoration_output_schema(),
                failure_label="Codex transcript restoration",
                sandbox="read-only",
                cwd=tmp_path,
            )
            try:
                return parse_inline_restorer_json(output, transcript, plan=plan)
            except ValueError as exc:
                persist_inline_rejected_markdown(output, restored_transcript_path)
                rejection_path = write_restorer_rejection_artifact(
                    transcript.meeting_id,
                    output=output,
                    restored_transcript_path=restored_transcript_path,
                    exc=exc,
                )
                if rejection_path is None:
                    raise
                raise ValueError(f"{exc} rejection_artifacts={rejection_path}") from exc


def write_restorer_rejection_artifact(
    meeting_id: str,
    *,
    output: str,
    restored_transcript_path: Path,
    exc: BaseException,
) -> Path | None:
    try:
        root = RESTORER_REJECTION_ARTIFACT_DIR
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        path = make_unique_artifact_path(root / f"{timestamp}-{artifact_slug(meeting_id)}")
        write_private_text(path / "output.json", output)
        if restored_transcript_path.exists():
            write_private_text(
                path / "restored_transcript.md",
                restored_transcript_path.read_text(encoding="utf-8"),
            )
        write_private_text(path / "error.txt", str(exc) + "\n")
        return path
    except OSError:
        return None


def persist_inline_rejected_markdown(output: str, restored_transcript_path: Path) -> None:
    try:
        payload = json.loads(output.strip())
    except json.JSONDecodeError:
        logger.warning("restorer output was not JSON; skipped rejected markdown persistence")
        return
    restored_markdown = payload.get("restored_transcript_markdown")
    if isinstance(restored_markdown, str) and restored_markdown.strip():
        write_private_text(restored_transcript_path, restored_markdown)


def inline_restoration_output_schema() -> dict[str, Any]:
    slack_visible_text_schema = {
        "type": "string",
        "description": "Use fullwidth slash (／) instead of halfwidth slash (/).",
        "pattern": r"^[^/]*$",
    }
    review_item_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "before": {"type": "string"},
            "after": {"type": "string"},
            "type": {
                "type": "string",
                "enum": ["person", "organization", "term", "other"],
            },
            "context_block": {"type": "string"},
        },
        "required": ["id", "before", "after", "type", "context_block"],
    }
    terms_candidate_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "target": {"type": "string", "enum": ["common", "client"]},
            "client": slack_visible_text_schema,
            "term": slack_visible_text_schema,
            "variant": slack_visible_text_schema,
            "cue": {"type": "string"},
            "caution": {"type": "string"},
            "evidence": {"type": "string"},
        },
        "required": [
            "id",
            "target",
            "client",
            "term",
            "variant",
            "cue",
            "caution",
            "evidence",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "restored_transcript_markdown": {"type": "string"},
            "needs_review_items": {"type": "array", "items": review_item_schema},
            "terms_candidates": {"type": "array", "items": terms_candidate_schema},
        },
        "required": [
            "restored_transcript_markdown",
            "needs_review_items",
            "terms_candidates",
        ],
    }


INLINE_RESTORER_OUTPUT_KEYS = frozenset(
    {"restored_transcript_markdown", "needs_review_items", "terms_candidates"}
)
RESTORER_REVIEW_ITEM_KEYS = frozenset({"id", "before", "after", "type", "context_block"})
RESTORER_TERMS_CANDIDATE_KEYS = frozenset(
    {"id", "target", "client", "term", "variant", "cue", "caution", "evidence"}
)


def parse_inline_restorer_json(
    output: str,
    original_transcript: TranscriptDoc,
    *,
    plan: MeetingPlan | None = None,
) -> TranscriptRestoration:
    payload = json.loads(output.strip())
    require_json_object_keys(payload, INLINE_RESTORER_OUTPUT_KEYS, "inline restorer output")
    require_string_fields(
        payload, frozenset({"restored_transcript_markdown"}), "inline restorer output"
    )
    restored_markdown = required_block(
        payload.get("restored_transcript_markdown"), "restored transcript markdown"
    )
    return transcript_restoration_from_payload(
        payload, restored_markdown, original_transcript, plan=plan
    )


def transcript_restoration_from_payload(
    payload: dict[str, Any],
    restored_markdown: str,
    original_transcript: TranscriptDoc,
    *,
    plan: MeetingPlan | None = None,
) -> TranscriptRestoration:
    review_items_payload = payload.get("needs_review_items", [])
    if not isinstance(review_items_payload, list):
        raise ValueError("needs_review_items must be an array")
    review_items = tuple(review_item_from_payload(item) for item in review_items_payload)
    terms_candidates_payload = payload.get("terms_candidates", [])
    if not isinstance(terms_candidates_payload, list):
        raise ValueError("terms_candidates must be an array")
    terms_candidates: list[TermsCandidate] = []
    for index, item in enumerate(terms_candidates_payload):
        try:
            terms_candidates.append(terms_candidate_from_payload(item, plan=plan))
        except ValueError as exc:
            write_terms_candidate_rejection_artifact(
                original_transcript,
                candidate_index=index,
                payload=item,
                error=exc,
                plan=plan,
            )

    return TranscriptRestoration(
        transcript=parse_restored_transcript_markdown(restored_markdown, original_transcript),
        restored_markdown=restored_markdown,
        needs_review_items=review_items,
        terms_candidates=tuple(terms_candidates),
        status="ok",
    )


def parse_restored_transcript_markdown(
    markdown: str,
    original_transcript: TranscriptDoc,
) -> TranscriptDoc:
    utterances = tuple(parse_restored_utterances(markdown))
    if not utterances:
        raise ValueError("restored transcript markdown did not contain transcript utterances")
    validate_source_timestamp_order(utterances, original_transcript.utterances)
    return dataclasses.replace(original_transcript, utterances=utterances)


def validate_source_timestamp_order(
    restored: tuple[Utterance, ...], source: tuple[Utterance, ...]
) -> None:
    positions: dict[int | None, list[int]] = {}
    for index, utterance in enumerate(source):
        positions.setdefault(utterance.seconds, []).append(index)

    last_position = -1
    for utterance in restored:
        candidates = positions.get(utterance.seconds)
        if not candidates:
            raise ValueError("restored transcript contains timestamp absent from source transcript")
        if last_position >= 0 and source[last_position].seconds == utterance.seconds:
            continue
        next_position = next(
            (position for position in candidates if position > last_position), None
        )
        if next_position is None:
            raise ValueError("restored transcript timestamp order differs from source transcript")
        last_position = next_position


def parse_restored_utterances(markdown: str) -> list[Utterance]:
    text = str(markdown or "")
    if any(line.strip().startswith("```") for line in text.splitlines()):
        raise ValueError("restored transcript markdown must not include code fences")
    utterances: list[Utterance] = []
    lines = [
        (line_number, raw_line.strip())
        for line_number, raw_line in enumerate(text.splitlines(), start=1)
        if raw_line.strip()
    ]
    parsed_by_line = [
        (line_number, line, parse_bracket_utterance(line)) for line_number, line in lines
    ]
    first_utterance_index = next(
        (index for index, (_, _, parsed) in enumerate(parsed_by_line) if parsed is not None), None
    )
    last_utterance_index = next(
        (
            len(parsed_by_line) - index - 1
            for index, (_, _, parsed) in enumerate(reversed(parsed_by_line))
            if parsed is not None
        ),
        None,
    )
    if first_utterance_index is None or last_utterance_index is None:
        raise ValueError("restored transcript markdown did not contain transcript utterances")
    for line_number, _line, parsed in parsed_by_line[:first_utterance_index]:
        if parsed is None:
            raise ValueError(
                f"restored transcript markdown contains text outside transcript: line {line_number}"
            )
    for line_number, _line, parsed in parsed_by_line[last_utterance_index + 1 :]:
        if parsed is None:
            raise ValueError(
                f"restored transcript markdown contains text outside transcript: line {line_number}"
            )

    for line_number, _stripped, parsed in parsed_by_line[
        first_utterance_index : last_utterance_index + 1
    ]:
        if parsed is None:
            raise ValueError(
                f"restored transcript line must be '[00:00] speaker: text' format: line {line_number}"
            )
        utterances.append(parsed)

    return utterances


BRACKET_TRANSCRIPT_LINE_RE = re.compile(
    r"^\[(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\]\s*(?P<speaker>[^:：]{1,80})[:：]\s*(?P<text>.*)$"
)


def parse_bracket_utterance(line: str) -> Utterance | None:
    match = BRACKET_TRANSCRIPT_LINE_RE.match(line)
    if not match:
        return None
    return make_utterance(match.group("time"), match.group("speaker"), match.group("text"))


def make_utterance(timestamp_text: Any, speaker: Any, text: Any) -> Utterance:
    seconds = timestamp_to_seconds(clean_inline(timestamp_text))
    speaker_text = clean_text(speaker)
    if not speaker_text:
        raise ValueError("restored transcript utterance speaker must not be empty")
    utterance_text = clean_text(text)
    if not utterance_text:
        raise ValueError("restored transcript utterance text must not be empty")
    return Utterance(
        seconds=seconds,
        timestamp=format_timestamp(seconds)
        if seconds is not None
        else clean_inline(timestamp_text),
        speaker=speaker_text,
        text=utterance_text,
    )


def render_transcript_input_markdown(transcript: TranscriptDoc) -> str:
    return f"""# {transcript.title}

- meeting_id: {transcript.meeting_id}
- source_key: {transcript.key}
- date: {transcript.date}

## Transcript

{format_transcript(transcript)}
"""


def meeting_plan_prompt_payload(transcript: TranscriptDoc, plan: MeetingPlan) -> dict[str, Any]:
    return {
        "meeting_id": transcript.meeting_id,
        "title": transcript.title,
        "date": transcript.date,
        "client": clean_inline(plan.client),
        "meeting_type": required_inline(plan.meeting_type, "meeting_type"),
        "speakers": [
            {
                "name": clean_inline(speaker.name),
                "raw": [clean_inline(raw) for raw in speaker.raw if clean_inline(raw)],
            }
            for speaker in plan.speakers
        ],
        "meeting_context": {
            "overview": clean_inline(plan.meeting_context.overview),
            "main_topics": list(cleaned_sequence(plan.meeting_context.main_topics)),
            "context_keywords": list(cleaned_sequence(plan.meeting_context.context_keywords)),
        },
        "filename": {
            "subject": clean_inline(plan.filename.subject),
            "summary": clean_inline(plan.filename.summary),
            "stem": clean_inline(plan.filename.stem),
        },
    }


def build_transcript_restorer_inline_prompt(
    transcript: TranscriptDoc,
    plan: MeetingPlan,
    reference_texts: Iterable[ReferenceText | str],
) -> str:
    return (
        "\n".join(
            [
                "# Transcript Restorer Inline Prompt",
                "",
                "## 役割",
                "",
                "音声認識で崩れた発話を、音と文脈から復元する。議事録本文、要約、決定事項、ToDoは作らない。",
                "ファイル作成、ファイル編集、shell実行、検索、Web、MCP、追加ローカル参照は不要。下に渡す本文だけを使う。",
                "",
                "## 出力",
                "",
                "最終応答はJSONだけ。`restored_transcript_markdown` には `[00:00] 話者名: 復元済み本文` の発話行だけを入れる。",
                "コードフェンス、見出し、metadata、説明、ログ、ファイルパスは `restored_transcript_markdown` に入れない。",
                "`needs_review_items` と `terms_candidates` は該当がなければ空配列にする。",
                "`terms_candidates` の `client`、`term`、`variant` は語彙候補一覧に表示される。半角スラッシュ `/` は使わず、文章区切りは全角スラッシュ `／` を使う。",
                "Pythonは内容の修正や品質判定をしない。出力前に自分で全文を見直し、意味のある発話の欠落、根拠のない追加、話者の取り違え、重複ループを解消する。",
                "発話の統合・分割は意味保存に必要な場合だけ行い、timestampは入力transcriptに実在する値だけ使う。",
                "",
                "## meeting_context_json",
                "",
                "```json",
                json.dumps(
                    meeting_plan_prompt_payload(transcript, plan), ensure_ascii=False, indent=2
                ),
                "```",
                "",
                "## reference_knowledge",
                "",
                "```text",
                format_reference_texts(reference_texts),
                "```",
                "",
                "## transcript",
                "",
                "```text",
                render_transcript_input_markdown(transcript),
                "```",
            ]
        ).strip()
        + "\n"
    )


def build_long_restoration_chunks(
    transcript: TranscriptDoc,
) -> tuple[LongRestorationChunk, ...]:
    utterances = transcript.utterances
    chunk_count = (
        len(utterances) + MAX_RESTORATION_CORE_UTTERANCES - 1
    ) // MAX_RESTORATION_CORE_UTTERANCES
    base_size, larger_chunk_count = divmod(len(utterances), chunk_count)
    chunks: list[LongRestorationChunk] = []
    core_start = 0
    for index in range(chunk_count):
        core_size = base_size + (index < larger_chunk_count)
        core_end = core_start + core_size
        context_start = max(0, core_start - RESTORATION_CONTEXT_UTTERANCES)
        context_end = min(len(utterances), core_end + RESTORATION_CONTEXT_UTTERANCES)
        core_utterances = utterances[core_start:core_end]
        core_identities = {utterance_timestamp_identity(utterance) for utterance in core_utterances}
        chunks.append(
            LongRestorationChunk(
                index=index,
                count=chunk_count,
                core=dataclasses.replace(transcript, utterances=core_utterances),
                context_before=tuple(
                    utterance
                    for utterance in utterances[context_start:core_start]
                    if utterance_timestamp_identity(utterance) not in core_identities
                ),
                context_after=tuple(
                    utterance
                    for utterance in utterances[core_end:context_end]
                    if utterance_timestamp_identity(utterance) not in core_identities
                ),
            )
        )
        core_start = core_end
    return tuple(chunks)


def build_transcript_restorer_chunk_prompt(
    chunk: LongRestorationChunk,
    plan: MeetingPlan,
    reference_texts: Iterable[ReferenceText | str],
) -> str:
    return render_prompt_template(
        "transcript_restorer_chunk",
        chunk_index=chunk.index + 1,
        chunk_count=chunk.count,
        meeting_context_json=json.dumps(
            meeting_plan_prompt_payload(chunk.core, plan), ensure_ascii=False, indent=2
        ),
        reference_knowledge=format_reference_texts(reference_texts),
        context_before=format_utterances(chunk.core, chunk.context_before),
        core_transcript=format_transcript(chunk.core),
        context_after=format_utterances(chunk.core, chunk.context_after),
    )


def format_utterances(transcript: TranscriptDoc, utterances: tuple[Utterance, ...]) -> str:
    if not utterances:
        return "(none)"
    return format_transcript(dataclasses.replace(transcript, utterances=utterances))


def utterance_timestamp_identity(utterance: Utterance) -> tuple[int | None, str]:
    return utterance.seconds, utterance.timestamp


def merge_long_restorations(
    original: TranscriptDoc,
    restorations: tuple[TranscriptRestoration, ...],
) -> TranscriptRestoration:
    merged_utterances = tuple(
        utterance for restoration in restorations for utterance in restoration.transcript.utterances
    )
    return TranscriptRestoration(
        transcript=dataclasses.replace(original, utterances=merged_utterances),
        restored_markdown="\n".join(restoration.restored_markdown for restoration in restorations),
        needs_review_items=merge_review_items(restorations),
        terms_candidates=merge_terms_candidates(restorations),
    )


def merge_review_items(
    restorations: tuple[TranscriptRestoration, ...],
) -> tuple[ReviewItem, ...]:
    unique: dict[tuple[str, str, str, str], ReviewItem] = {}
    for restoration in restorations:
        for item in restoration.needs_review_items:
            key = (item.before, item.after, item.type, item.context_block)
            unique.setdefault(key, item)
    return tuple(
        dataclasses.replace(item, id=f"R-{index:04d}")
        for index, item in enumerate(unique.values(), start=1)
    )


def merge_terms_candidates(
    restorations: tuple[TranscriptRestoration, ...],
) -> tuple[TermsCandidate, ...]:
    unique: dict[tuple[str, str, str, str], TermsCandidate] = {}
    for restoration in restorations:
        for candidate in restoration.terms_candidates:
            key = (
                candidate.target,
                candidate.client,
                candidate.term,
                candidate.variant,
            )
            unique.setdefault(key, candidate)
    return tuple(
        dataclasses.replace(candidate, id=f"TC-{index:04d}")
        for index, candidate in enumerate(unique.values(), start=1)
    )


def load_reference_texts(
    urls: Iterable[str] | str, *, hints_by_url: dict[str, str] | None = None
) -> tuple[ReferenceText, ...]:
    texts: list[ReferenceText] = []
    hints = hints_by_url or {}
    for url in coerce_urls(urls):
        try:
            path = reference_file_url_to_path(url)
        except FileNotFoundError:
            continue
        body = clean_block(path.read_text(encoding="utf-8"))
        if body:
            texts.append(
                ReferenceText(
                    url=clean_inline(url),
                    source_type=reference_source_type(path),
                    hint=clean_inline(hints.get(url, "")),
                    text=body,
                )
            )
    return tuple(texts)


def reference_source_type(path: Path) -> str:
    path_text = path.resolve().as_posix()
    if path_text.endswith("/knowledge/transcription-terms/common.md"):
        return "common_terms"
    if "/knowledge/transcription-terms/clients/" in path_text:
        return "client_terms"
    if ("/30_案件/" in path_text or "/04_クライアント/" in path_text) and path.name == "general.md":
        return "client_context"
    if "/40_人物組織/persons/" in path_text:
        return "person_context"
    if "/40_人物組織/organizations/" in path_text:
        return "organization_context"
    if "/50_議事録/" in path_text:
        return "meeting_context"
    return "context"


def append_review_inbox(
    root_path: Path | str,
    plan: MeetingPlan,
    items: Iterable[ReviewItem],
    transcript: TranscriptDoc,
) -> Path:
    path = review_inbox_path(Path(root_path))
    review_items = tuple(items)
    if not review_items:
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        needs_header = not handle.read().strip()
        handle.seek(0, 2)
        if needs_header:
            handle.write(
                "# 文字起こし復元レビューInbox\n\n"
                "- ステータス: 要確認 = 人間が確認する未処理項目\n"
                "- 本文反映: 反映済み = transcriptには反映済み。確認結果は確認メモに残す\n"
                "- 本文反映: 要確認付きで反映 = transcript本文に[要確認]が残っている。確認結果は確認メモに残す\n"
            )
        handle.write(render_review_inbox_entry(plan, review_items, transcript))
    path.chmod(0o600)
    return path


def review_inbox_path(root_path: Path = BRAIN_ROOT) -> Path:
    if Path(root_path) == BRAIN_ROOT:
        return REVIEW_INBOX_PATH
    return Path(root_path) / "knowledge" / REVIEW_INBOX_FILENAME


def append_terms_candidates(
    root_path: Path | str,
    plan: MeetingPlan,
    candidates: Iterable[TermsCandidate],
    transcript: TranscriptDoc,
) -> Path:
    path = terms_candidates_path(Path(root_path))
    terms_candidates = tuple(candidates)
    if not terms_candidates:
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        needs_header = not handle.read().strip()
        handle.seek(0, 2)
        if needs_header:
            handle.write(
                "# 文字起こし復元Terms候補\n\n"
                "- このファイルは候補置き場。appはtranscription-terms 配下のmdを直接変更しない\n"
                "- 4列スキーマ: 正規表記 | 揺れ | 復元する文脈 | 混同・維持\n"
            )
        handle.write(render_terms_candidates_entry(plan, terms_candidates, transcript))
    path.chmod(0o600)
    return path


def terms_candidates_path(root_path: Path = BRAIN_ROOT) -> Path:
    if Path(root_path) == BRAIN_ROOT:
        return TERMS_CANDIDATES_PATH
    return Path(root_path) / "knowledge" / TERMS_CANDIDATES_FILENAME


def render_review_inbox_entry(
    plan: MeetingPlan, items: tuple[ReviewItem, ...], transcript: TranscriptDoc
) -> str:
    client = clean_inline(plan.client)
    meeting_type = required_inline(plan.meeting_type, "meeting_type")
    created_at = dt.datetime.now(JST).isoformat(timespec="seconds")
    lines = [
        "",
        f"## {transcript.date} {transcript.title}",
        "",
        f"- 追記日時: {created_at}",
        f"- meeting_id: {transcript.meeting_id}",
        f"- source_key: {transcript.key}",
        f"- client: {client}",
        f"- meeting_type: {meeting_type}",
        "",
    ]
    for item in items:
        lines.extend(
            [
                f"### {item.id}",
                "",
                f"- ステータス: {REVIEW_STATUS_PENDING}",
                f"- 確認すること: {review_confirmation_question(item)}",
                f"- 本文反映: {review_body_application(item)}",
                f"- 種別: {item.type}",
                f"- 原文/曖昧語: {item.before}",
                f"- 復元/候補: {item.after}",
                "",
                "```text",
                fence_safe(item.context_block),
                "```",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_terms_candidates_entry(
    plan: MeetingPlan, candidates: tuple[TermsCandidate, ...], transcript: TranscriptDoc
) -> str:
    client = clean_inline(plan.client)
    meeting_type = required_inline(plan.meeting_type, "meeting_type")
    created_at = dt.datetime.now(JST).isoformat(timespec="seconds")
    lines = [
        "",
        f"## {transcript.date} {transcript.title}",
        "",
        f"- 追記日時: {created_at}",
        f"- meeting_id: {transcript.meeting_id}",
        f"- source_key: {transcript.key}",
        f"- client: {client}",
        f"- meeting_type: {meeting_type}",
        "",
    ]
    for candidate in candidates:
        lines.extend(
            [
                f"### {candidate.id}",
                "",
                "- ステータス: 候補",
                f"- target: {candidate.target}",
                f"- client: {candidate.client or client}",
                f"- 正規表記: {candidate.term}",
                f"- 揺れ: {candidate.variant}",
                f"- 復元する文脈: {candidate.cue}",
                f"- 混同・維持: {candidate.caution}",
                "",
                "```text",
                fence_safe(candidate.evidence),
                "```",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def review_confirmation_question(item: ReviewItem) -> str:
    if REVIEW_MARKER in item.after:
        candidate = item.after.replace(REVIEW_MARKER, "").strip()
        if candidate:
            return f"`{candidate}` を採用してよいか。違う場合は正しい表記に直す"
        return "本文中の要確認箇所を正しい表記に直す"
    return f"`{item.after}` の復元を採用してよいか判断する"


def review_body_application(item: ReviewItem) -> str:
    return "要確認付きで反映" if REVIEW_MARKER in item.after else "反映済み"


def reference_file_url_to_path(url: str) -> Path:
    cleaned_url = clean_inline(url)
    if not cleaned_url:
        raise ValueError("reference url must not be empty")
    parsed = urlparse(cleaned_url)
    if parsed.scheme != "file":
        raise ValueError(f"reference file URLs must use file:// URLs: {cleaned_url}")
    if parsed.netloc not in {"", "localhost"}:
        raise ValueError(f"file:// reference must be local: {cleaned_url}")
    path = Path(unquote(parsed.path))
    if not path.is_absolute():
        raise ValueError(f"file:// reference must be absolute: {cleaned_url}")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        raise FileNotFoundError(f"reference file does not exist: {path}") from None

    allowed_roots = (BRAIN_ROOT.resolve(strict=True), KNOWLEDGE_DIR.resolve(strict=True))
    if not any(resolved == root or root in resolved.parents for root in allowed_roots):
        allowed = ", ".join(str(root) for root in allowed_roots)
        raise ValueError(f"reference file must be under one of: {allowed}: {resolved}")
    if not resolved.is_file():
        raise ValueError(f"reference path must be a file: {resolved}")
    return resolved


def coerce_urls(urls: Iterable[str] | str) -> tuple[str, ...]:
    if isinstance(urls, str):
        return (urls,)
    return tuple(str(url) for url in urls)


def format_reference_texts(reference_texts: Iterable[ReferenceText | str]) -> str:
    references = tuple(coerce_reference_text(reference) for reference in reference_texts)
    if not references:
        return "なし"
    grouped_references = (
        (
            "Restoration Terms",
            [
                reference
                for reference in references
                if not reference.hint and reference.source_type in {"common_terms", "client_terms"}
            ],
        ),
        (
            "Core Context",
            [
                reference
                for reference in references
                if not reference.hint
                and reference.source_type not in {"common_terms", "client_terms"}
            ],
        ),
        ("Additional Context", [reference for reference in references if reference.hint]),
    )
    sections: list[str] = [
        "\n".join(
            (
                "# Reference Knowledge",
                "",
                "- meeting_plan.json と合わせて復元判断に使う",
                "- source_url / hint は出典と補助文脈の手がかり",
                "- source_url や本文中のローカル参照は追加で開かず、このファイル内の本文だけを使う",
                "- Restoration Terms → Core Context → Additional Context の順に読む",
            )
        )
    ]
    reference_index = 1
    for group_name, group_references in grouped_references:
        if not group_references:
            continue
        sections.append(f"## {group_name}")
        for reference in group_references:
            sections.append(format_reference_section(reference_index, reference))
            reference_index += 1
    return "\n\n".join(sections) if sections else "なし"


def coerce_reference_text(value: ReferenceText | str) -> ReferenceText:
    if isinstance(value, ReferenceText):
        return value
    return ReferenceText(url="", source_type="context", hint="", text=clean_block(value))


def format_reference_section(index: int, reference: ReferenceText) -> str:
    lines = [
        f"### Reference {index}: {reference.source_type}",
        "",
        f"- source_type: {reference.source_type}",
    ]
    if reference.url:
        lines.append(f"- source_url: {reference.url}")
    if reference.hint:
        lines.append(f"- hint: {reference.hint}")
    lines.extend(("", reference.text))
    return "\n".join(lines).strip()


def review_item_from_payload(value: Any) -> ReviewItem:
    require_json_object_keys(value, RESTORER_REVIEW_ITEM_KEYS, "review item")
    require_string_fields(value, RESTORER_REVIEW_ITEM_KEYS, "review item")
    return ReviewItem(
        id=required_inline(value.get("id"), "review item id"),
        before=required_inline(value.get("before"), "review item before"),
        after=clean_inline(value.get("after")),
        type=required_inline(value.get("type"), "review item type"),
        context_block=clean_block(value.get("context_block")),
    )


def terms_candidate_from_payload(value: Any, *, plan: MeetingPlan | None = None) -> TermsCandidate:
    require_json_object_keys(value, RESTORER_TERMS_CANDIDATE_KEYS, "terms candidate")
    require_string_fields(value, RESTORER_TERMS_CANDIDATE_KEYS, "terms candidate")
    target = required_inline(value.get("target"), "terms candidate target")
    client = clean_inline(value.get("client")) or (
        clean_inline(plan.client) if plan is not None and target == "client" else ""
    )
    if target == "client" and not client:
        raise ValueError("missing terms candidate client")
    return TermsCandidate(
        id=required_inline(value.get("id"), "terms candidate id"),
        target=target,
        client=client,
        term=required_inline(value.get("term"), "terms candidate term"),
        variant=required_inline(value.get("variant"), "terms candidate variant"),
        cue=required_inline(value.get("cue"), "terms candidate cue"),
        caution=required_inline(value.get("caution"), "terms candidate caution"),
        evidence=required_block(value.get("evidence"), "terms candidate evidence"),
    )


def write_terms_candidate_rejection_artifact(
    transcript: TranscriptDoc,
    *,
    candidate_index: int,
    payload: Any,
    error: ValueError,
    plan: MeetingPlan | None = None,
) -> Path:
    rejected_at = dt.datetime.now(JST)
    path = (
        DEFAULT_ARTIFACT_ROOT
        / sanitize_filename_stem(transcript.meeting_id)
        / "03_restoration"
        / "terms_candidate_rejections"
        / f"{rejected_at.strftime('%Y%m%dT%H%M%S%f%z')}-{candidate_index:03d}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "meeting_id": transcript.meeting_id,
                "candidate_index": candidate_index,
                "rejected_at": rejected_at.isoformat(),
                "missing_fields": missing_terms_candidate_fields(payload, plan=plan),
                "error": str(error),
                "payload": payload,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def missing_terms_candidate_fields(value: Any, *, plan: MeetingPlan | None = None) -> list[str]:
    if not isinstance(value, dict):
        return sorted(RESTORER_TERMS_CANDIDATE_KEYS)
    missing = set(RESTORER_TERMS_CANDIDATE_KEYS - set(value))
    for field_name in RESTORER_TERMS_CANDIDATE_KEYS:
        if field_name in missing:
            continue
        field_value = value.get(field_name)
        if not isinstance(field_value, str):
            missing.add(field_name)
            continue
        if field_name == "evidence":
            if not clean_block(field_value):
                missing.add(field_name)
        elif field_name != "client" and not clean_inline(field_value):
            missing.add(field_name)
    target = clean_inline(value.get("target"))
    if target == "client":
        client = clean_inline(value.get("client")) or (
            clean_inline(plan.client) if plan is not None else ""
        )
        if not client:
            missing.add("client")
    elif target and target not in {"common", "client"}:
        missing.add("target")
    return sorted(missing)


def require_json_object_keys(value: Any, required_keys: frozenset[str], label: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    actual_keys = set(value)
    missing = required_keys - actual_keys
    if missing:
        raise ValueError(f"{label} JSON schema missing fields: {', '.join(sorted(missing))}")
    unexpected = actual_keys - required_keys
    if unexpected:
        raise ValueError(f"{label} JSON schema unexpected fields: {', '.join(sorted(unexpected))}")


def require_string_fields(value: dict[str, Any], field_names: frozenset[str], label: str) -> None:
    non_strings = sorted(
        field_name for field_name in field_names if not isinstance(value.get(field_name), str)
    )
    if non_strings:
        raise ValueError(f"{label} JSON schema non-string fields: {', '.join(non_strings)}")


def cleaned_sequence(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if clean_inline(value) else ()
    try:
        iterator = iter(value)
    except TypeError:
        text = clean_plan_item(value)
        return (text,) if text else ()
    result: list[str] = []
    for item in iterator:
        text = clean_plan_item(item)
        if text:
            result.append(text)
    return tuple(result)


def clean_plan_item(value: Any) -> str:
    if hasattr(value, "name"):
        return clean_inline(value.name)
    return clean_inline(value)


def clean_inline(value: Any) -> str:
    return " ".join(str(value or "").split())


def clean_block(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def required_inline(value: Any, label: str) -> str:
    text = clean_inline(value)
    if not text:
        raise ValueError(f"missing {label}")
    return text


def required_block(value: Any, label: str) -> str:
    text = clean_block(value)
    if not text:
        raise ValueError(f"missing {label}")
    return text


def fence_safe(value: str) -> str:
    return value.replace("```", "``\u200b`")


__all__ = [
    "CodexTranscriptRestorer",
    "LONG_RESTORER_MIN_UTTERANCES",
    "LongRestorationChunk",
    "ReviewItem",
    "TermsCandidate",
    "TranscriptRestoration",
    "TranscriptRestorer",
    "append_review_inbox",
    "append_terms_candidates",
    "build_long_restoration_chunks",
    "load_reference_texts",
    "parse_restored_transcript_markdown",
    "review_inbox_path",
    "terms_candidates_path",
]
