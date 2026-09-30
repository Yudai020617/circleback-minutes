from __future__ import annotations

import dataclasses
import datetime as dt
import fcntl
import os
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .artifacts import meeting_artifacts
from .circleback_client import (
    Attendee,
    CirclebackClient,
    CirclebackCommandError,
    MeetingCandidate,
    MeetingMetadata,
    TranscriptDoc,
    Utterance,
)
from .claude_exec import DEFAULT_CLAUDE_BIN
from .codex_exec import DEFAULT_CODEX_BIN, DEFAULT_CODEX_MODEL, DEFAULT_CODEX_EFFORT
from .deletion_candidates import append_deletion_candidate
from .distill import DistilledMinutes, Distiller, format_transcript, get_distiller
from .frontmatter import (
    MEETING_SHARE_VALUES,
    meeting_circleback_id,
    meeting_share,
    split_frontmatter,
)
from .judgment import (
    DEFAULT_JUDGE_TIMEOUT_SECONDS,
    ClaudeMeetingJudge,
    apply_meeting_judgment,
    build_recent_meetings_index,
)
from .log_safety import short_log_detail
from .models import circleback_lookup_id
from .paths import BRAIN_ROOT, DEFAULT_STATE_PATH, MeetingPaths
from .people import (
    DEFAULT_INTERNAL_PEOPLE_PATH,
    DEFAULT_MEMBERS_JSON_PATH,
    DEFAULT_SPEAKER_ALIASES_PATH,
    load_member_names,
    load_person_name_stems,
    load_roster,
    load_speaker_aliases,
    normalize_person_name,
)
from .people_intake import IntakeMeeting, intake_people_for_meeting, intake_report_payload
from .planning import (
    CLIENTS_OVERVIEW_SOURCE_PATH,
    MeetingPlan,
    MeetingPlanSpeaker,
    build_meeting_plan_filename,
)
from .preprocess import (
    build_speaker_legend,
    legend_payload,
    normalize_transcript_spacing,
    render_judge_input_markdown,
)
from .restoration import (
    CodexTranscriptRestorer,
    ReviewItem,
    TranscriptRestoration,
    TranscriptRestorer,
    append_review_inbox,
    append_terms_candidates,
    load_reference_texts,
)
from .stage_status import STAGE_GET, STAGE_PLANNER, STAGE_RESTORER, STAGE_WRITER, StageStatusTracker

MIN_TRANSCRIPT_UTTERANCES_FOR_LLM = 10
ALL_MEETINGS_MAX_PAGES = 1000
# circleback CLI returns only its default window when --last is omitted,
# so --all must pass an explicit wide window to enumerate every meeting.
ALL_MEETINGS_LAST_DAYS = 3650
STOP_AFTER_CIRCLEBACK = "circleback"
STOP_AFTER_PLANNING = "planning"
STOP_AFTER_MINUTES = "minutes"
STOP_AFTER_RESTORATION = "restoration"
STOP_AFTER_VALUES = frozenset(
    {STOP_AFTER_CIRCLEBACK, STOP_AFTER_PLANNING, STOP_AFTER_MINUTES, STOP_AFTER_RESTORATION}
)
BACKFILL_STAGE_PREPROCESS = "preprocess"
BACKFILL_STAGE_LLM1 = "llm1"
BACKFILL_STAGE_LLM2 = "llm2"
BACKFILL_STAGE_LLM3 = "llm3"
BACKFILL_STAGE_VALUES = frozenset(
    {BACKFILL_STAGE_PREPROCESS, BACKFILL_STAGE_LLM1, BACKFILL_STAGE_LLM2, BACKFILL_STAGE_LLM3}
)
TRANSCRIPT_SECTION_HEADING = "## 文字起こし"
PEOPLE_INDEX_PERSON_KEYS = (
    "役割",
    "読み",
    "メール",
    "別名・誤読",
    "議事録での呼ばれ方/誤読パターン",
)
PEOPLE_INDEX_ORGANIZATION_KEYS = (
    "正式表記",
    "略称",
    "組織種別",
    "自社関係",
    "ステータス",
    "親会社",
    "事業内容",
    "分類シグナル",
)


@dataclasses.dataclass(frozen=True)
class StoreMetadata:
    client: str
    meeting_type: str
    tags: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class PeopleContext:
    aliases: dict[str, str]
    roster: frozenset[str]
    member_names: frozenset[str] = frozenset()
    person_name_stems: frozenset[str] = frozenset()


@dataclasses.dataclass(frozen=True)
class PipelineConfig:
    execute: bool = False
    meeting_ids: tuple[str, ...] = ()
    search: str | None = None
    last_days: int | None = 7
    from_date: str | None = None
    to_date: str | None = None
    limit: int | None = 20
    page: int = 1
    pages: int | None = None
    cutover_started_at: str | None = None
    root_path: Path = BRAIN_ROOT
    state_path: Path = DEFAULT_STATE_PATH
    artifact_root: Path | None = None
    force: bool = False
    stop_after: str = STOP_AFTER_MINUTES
    minutes_only: bool = False
    backfill_stage: str | None = None
    stage_tracker: StageStatusTracker | None = None


@contextmanager
def tracked_stage(
    tracker: StageStatusTracker | None,
    stage: str,
    *,
    meeting_id: str | None = None,
) -> Iterator[None]:
    if tracker is None:
        yield
        return
    tracker.start(stage, meeting_id=meeting_id)
    try:
        yield
    except Exception as exc:
        reason_code = f"{stage}.{exc.category}" if isinstance(exc, CirclebackCommandError) else None
        tracker.fail(stage, exc, meeting_id=meeting_id, reason_code=reason_code)
        raise
    else:
        tracker.succeed(stage)


class CirclebackLocalPipeline:
    def __init__(
        self,
        *,
        client: Any,
        distiller: Distiller | None,
        store: Any | None = None,
        judge: Any | None = None,
        restorer: TranscriptRestorer | None = None,
    ) -> None:
        self.client = client
        self.distiller = distiller
        self.store = store
        self.judge = judge
        self.restorer = restorer

    def run_once(self, config: PipelineConfig) -> dict[str, Any]:
        if config.execute and self.store is None:
            raise RuntimeError("execute requires circleback_local.local_store")
        backfill_stage = validate_backfill_stage(config.backfill_stage)
        stop_after = stop_after_for_backfill_stage(backfill_stage) or validate_stop_after(
            config.stop_after
        )
        minutes_only = config.minutes_only or backfill_stage == BACKFILL_STAGE_LLM3
        if minutes_only != config.minutes_only:
            config = dataclasses.replace(config, minutes_only=minutes_only)
        if minutes_only:
            validate_minutes_only_config(stop_after=stop_after)

        stage_tracker = config.stage_tracker
        candidate_collection_tracker = (
            stage_tracker
            if should_track_candidate_collection(minutes_only, backfill_stage)
            else None
        )
        with tracked_stage(candidate_collection_tracker, STAGE_GET):
            candidates = dedupe_candidates(self._collect_candidates(config))
        resolve_explicit_identity = (
            config.execute
            and config.meeting_ids
            and not minutes_only
            and backfill_stage in {None, BACKFILL_STAGE_PREPROCESS}
        )
        summary: dict[str, Any] = {
            "execute": config.execute,
            "candidates": 0,
            "processed": 0,
            "would_process": 0,
            "skipped": 0,
            "failed": 0,
            "results": [],
        }
        excluded_meeting_ids = excluded_meeting_ids_for_state(config.state_path)
        seen_canonical: set[str] = set()
        for candidate in candidates:
            if candidate.lookup_id in excluded_meeting_ids:
                summary["candidates"] += 1
                summary["skipped"] += 1
                summary["results"].append(
                    {
                        "operation": "skip_excluded",
                        "meeting_id": candidate.lookup_id,
                        "source_key": candidate.key,
                    }
                )
                continue
            metadata: MeetingMetadata | None = None
            if resolve_explicit_identity:
                try:
                    with tracked_stage(
                        stage_tracker,
                        STAGE_GET,
                        meeting_id=candidate.lookup_id,
                    ):
                        metadata = self.client.read_meeting(candidate.lookup_id)
                except Exception as error:  # noqa: BLE001 - one bad id must not block the batch.
                    summary["candidates"] += 1
                    summary["failed"] += 1
                    summary["results"].append(
                        failed_candidate_result(
                            candidate,
                            error,
                            artifact_dir=None,
                            failed_stage=STAGE_GET,
                        )
                    )
                    continue
                candidate = dataclasses.replace(candidate, lookup_id=metadata.meeting_id)
            if candidate.key in seen_canonical:
                continue
            seen_canonical.add(candidate.key)
            summary["candidates"] += 1
            try:
                with meeting_lock(
                    candidate.key, config.state_path, enabled=config.execute
                ) as acquired:
                    if not acquired:
                        result = {
                            "operation": "skip_locked",
                            "meeting_id": candidate.lookup_id,
                            "source_key": candidate.key,
                        }
                    else:
                        result = self._process_candidate(
                            candidate,
                            execute=config.execute,
                            root_path=config.root_path,
                            state_path=config.state_path,
                            artifact_root=config.artifact_root,
                            force=config.force,
                            stop_after=stop_after,
                            minutes_only=minutes_only,
                            backfill_stage=backfill_stage,
                            metadata=metadata,
                            stage_tracker=stage_tracker,
                        )
            except Exception as exc:  # noqa: BLE001 - batch processing must keep later meetings moving.
                artifacts = meeting_artifacts(
                    candidate.lookup_id,
                    artifact_root=config.artifact_root,
                    state_path=config.state_path,
                )
                failed_stage = (
                    stage_tracker.failure_stage_for(candidate.lookup_id)
                    if stage_tracker is not None
                    else None
                )
                artifacts.write_failure(failed_stage or "processing", exc)
                result = failed_candidate_result(
                    candidate,
                    exc,
                    artifact_dir=artifacts.path,
                    failed_stage=failed_stage or "processing",
                )
            summary["results"].append(result)
            operation = result.get("operation")
            if operation in {
                "processed",
                "restored",
                "circleback_saved",
                "planned",
            }:
                summary["processed"] += 1
            elif operation == "dry_run":
                summary["would_process"] += 1
            elif operation == "failed":
                summary["failed"] += 1
            else:
                summary["skipped"] += 1
        if stage_tracker is not None:
            stage_tracker.record_candidates(summary["candidates"])
        return summary

    def _collect_candidates(self, config: PipelineConfig) -> list[MeetingCandidate]:
        if config.minutes_only:
            return collect_saved_transcript_candidates(config)
        if config.meeting_ids:
            return dedupe_candidates(
                MeetingCandidate(lookup_id=circleback_lookup_id(meeting_id))
                for meeting_id in config.meeting_ids
            )
        candidates = self.client.search_meetings(
            query=config.search,
            last_days=config.last_days,
            from_date=config.from_date,
            to_date=config.to_date,
            page=config.page,
            pages=config.pages,
            limit=config.limit,
        )
        if config.cutover_started_at:
            marker = parse_datetime(config.cutover_started_at)
            candidates = [
                candidate
                for candidate in candidates
                if candidate.started_at and parse_datetime(candidate.started_at) >= marker
            ]
        return dedupe_candidates(candidates)

    def _process_candidate(
        self,
        candidate: MeetingCandidate,
        *,
        execute: bool,
        root_path: Path,
        state_path: Path,
        artifact_root: Path | None,
        force: bool,
        stop_after: str,
        minutes_only: bool,
        backfill_stage: str | None,
        metadata: MeetingMetadata | None = None,
        stage_tracker: StageStatusTracker | None = None,
    ) -> dict[str, Any]:
        if backfill_stage == BACKFILL_STAGE_PREPROCESS:
            stop_after = STOP_AFTER_CIRCLEBACK
        if backfill_stage == BACKFILL_STAGE_LLM1:
            return self._process_saved_preprocess_candidate(
                candidate,
                execute=execute,
                root_path=root_path,
                state_path=state_path,
                artifact_root=artifact_root,
                stage_tracker=stage_tracker,
            )
        if backfill_stage == BACKFILL_STAGE_LLM2:
            return self._process_saved_plan_candidate(
                candidate,
                execute=execute,
                root_path=root_path,
                state_path=state_path,
                artifact_root=artifact_root,
                stage_tracker=stage_tracker,
            )
        if backfill_stage == BACKFILL_STAGE_LLM3:
            return self._process_saved_restoration_candidate(
                candidate,
                execute=execute,
                root_path=root_path,
                state_path=state_path,
                artifact_root=artifact_root,
                stage_tracker=stage_tracker,
            )
        if minutes_only:
            return self._process_saved_transcript_candidate(
                candidate,
                execute=execute,
                state_path=state_path,
                artifact_root=artifact_root,
                force=force,
                stage_tracker=stage_tracker,
            )

        artifacts = meeting_artifacts(
            candidate.lookup_id, artifact_root=artifact_root, state_path=state_path
        )
        existing_record = (
            existing_minutes_record(candidate, state_path=state_path)
            if self.store is not None
            else None
        )
        if not force and existing_record is not None:
            slug = existing_record.get("slug")
            transcript_path_value = existing_record.get("transcript_path")
            minutes_path_value = existing_record.get("minutes_path")
            if (
                isinstance(slug, str)
                and Path(slug).name == slug
                and transcript_path_value
                and minutes_path_value
            ):
                meeting_dir = root_path / "50_議事録" / slug
                transcript_path = Path(str(transcript_path_value))
                minutes_path = Path(str(minutes_path_value))
                exact_pair_exists = (
                    not meeting_dir.is_symlink()
                    and meeting_dir.is_dir()
                    and transcript_path == meeting_dir / "transcript.md"
                    and minutes_path == meeting_dir / "minutes.md"
                    and not transcript_path.is_symlink()
                    and transcript_path.is_file()
                    and not minutes_path.is_symlink()
                    and minutes_path.is_file()
                )
                if exact_pair_exists:
                    transcript_frontmatter, _body = split_frontmatter(
                        transcript_path.read_text(encoding="utf-8")
                    )
                    minutes_frontmatter, _body = split_frontmatter(
                        minutes_path.read_text(encoding="utf-8")
                    )
                    if (
                        meeting_circleback_id(transcript_frontmatter) != candidate.lookup_id
                        or meeting_circleback_id(minutes_frontmatter) != candidate.lookup_id
                    ):
                        exact_pair_exists = False
                if exact_pair_exists:
                    if existing_record.get("minutes_needs_regeneration") is True:
                        return self._process_saved_transcript_candidate(
                            candidate,
                            execute=execute,
                            state_path=state_path,
                            artifact_root=artifact_root,
                            force=True,
                            stage_tracker=stage_tracker,
                        )
                    return {
                        "operation": "skip_existing",
                        "meeting_id": candidate.lookup_id,
                        "source_key": candidate.key,
                        "minutes_path": str(minutes_path),
                    }

        with tracked_stage(
            stage_tracker,
            STAGE_GET,
            meeting_id=candidate.lookup_id,
        ):
            if metadata is None:
                metadata = self.client.read_meeting(candidate.lookup_id)
            raw_transcript = self.client.read_transcript(candidate).with_metadata(metadata)
            preprocessed_transcript = normalize_transcript_spacing(raw_transcript)
            legend = build_speaker_legend(preprocessed_transcript)
            judge_input = render_judge_input_markdown(preprocessed_transcript, legend)
            # S1 raw永久保存が安全の本体。取得成功までは既存artifactを消さない
            artifacts.reset()
            artifacts.save_circleback(
                candidate=candidate,
                metadata=metadata,
                raw_transcript=raw_transcript,
                preprocessed_transcript=preprocessed_transcript,
                judge_input_markdown=judge_input,
                legend=legend_payload(legend),
            )
        short_result = skip_short_transcript_result(
            candidate, preprocessed_transcript, artifact_dir=artifacts.path
        )
        if short_result is not None:
            if execute:
                append_deletion_candidate(
                    meeting_id=candidate.lookup_id,
                    title=preprocessed_transcript.title,
                    date=preprocessed_transcript.date,
                    utterance_count=len(preprocessed_transcript.utterances),
                    duration_seconds=preprocessed_transcript.duration_seconds,
                    reason=(
                        f"発話{len(preprocessed_transcript.utterances)}件"
                        f"(<{MIN_TRANSCRIPT_UTTERANCES_FOR_LLM})で会議実体なし"
                    ),
                )
            artifacts.save_store_result(short_result)
            return short_result
        if stop_after == STOP_AFTER_CIRCLEBACK:
            result = {
                "operation": "circleback_saved",
                "meeting_id": candidate.lookup_id,
                "source_key": candidate.key,
                "title": preprocessed_transcript.title,
                "utterance_count": len(preprocessed_transcript.utterances),
                "stopped_after": STOP_AFTER_CIRCLEBACK,
                "artifact_dir": str(artifacts.path),
            }
            artifacts.save_store_result(result)
            return result
        with tracked_stage(
            stage_tracker,
            STAGE_PLANNER,
            meeting_id=candidate.lookup_id,
        ):
            legend_map = dict(legend)
            judgment = require_judge(self.judge).judge(
                judge_input,
                read_required_text(root_path / "30_案件" / "clients-overview.md"),
                build_people_index(root_path),
                build_recent_meetings_index(state_path, exclude_meeting_id=candidate.lookup_id),
                legend_map,
            )
            plan = complete_plan_filename_locally(judgment.plan, preprocessed_transcript)
            edits = judgment.edits
            edited_transcript = apply_meeting_judgment(
                preprocessed_transcript, judgment, legend_map
            )
            artifacts.save_judgment(
                plan=plan,
                edits=edits,
                doubt_flags=judgment.doubt_flags,
                speaker_names_by_token=judgment.speaker_names_by_token,
                edited_transcript=edited_transcript,
            )
        if stop_after == STOP_AFTER_PLANNING:
            result = {
                "operation": "planned",
                "meeting_id": candidate.lookup_id,
                "source_key": preprocessed_transcript.key,
                "title": preprocessed_transcript.title,
                "client": plan.client,
                "meeting_type": plan.meeting_type,
                "filename_stem": plan.filename.stem,
                "edit_count": len(edits),
                "doubt_flag_count": len(judgment.doubt_flags),
                "stopped_after": STOP_AFTER_PLANNING,
                "artifact_dir": str(artifacts.path),
            }
            artifacts.save_store_result(result)
            return result
        with tracked_stage(
            stage_tracker,
            STAGE_RESTORER,
            meeting_id=candidate.lookup_id,
        ):
            people_context = load_people_context(root_path)
            reference_texts = load_reference_texts(
                plan.reference_file_urls,
                hints_by_url={
                    reference.url: reference.hint for reference in plan.additional_references
                },
            )
            artifacts.save_restoration_inputs(
                transcript=edited_transcript,
                plan=plan,
                reference_texts=reference_texts,
            )
            restoration = require_restorer(self.restorer).restore(
                edited_transcript,
                plan,
                reference_texts=reference_texts,
            )
            artifacts.save_restoration_output(restoration)
            transcript = normalize_transcript_people(
                with_plan_speakers(restoration.transcript, plan, people_context),
                people_context.aliases,
            )
            artifacts.save_transcript_stages(
                original_transcript=edited_transcript, restored_transcript=transcript
            )
            store_metadata = store_metadata_from_plan(plan)
            result = {
                "operation": "processed" if execute else "dry_run",
                "meeting_id": candidate.lookup_id,
                "source_key": transcript.key,
                "title": transcript.title,
                "client": plan.client,
                "meeting_type": plan.meeting_type,
                "filename_stem": plan.filename.stem,
                "restoration_status": restoration.status,
                "needs_review_count": len(restoration.needs_review_items),
                "terms_candidate_count": len(restoration.terms_candidates),
                "edit_count": len(edits),
                "doubt_flag_count": len(judgment.doubt_flags),
                "artifact_dir": str(artifacts.path),
            }
            if restoration.error:
                result["restoration_error"] = restoration.error
                if stage_tracker is not None:
                    stage_tracker.warn(
                        STAGE_RESTORER, "restorer.output_warning", meeting_id=candidate.lookup_id
                    )
            if execute:
                persist_judgment_doubt_flags(
                    transcript=edited_transcript,
                    plan=plan,
                    doubt_flags=judgment.doubt_flags,
                    result=result,
                    root_path=root_path,
                )
                persist_restoration_findings(
                    transcript=transcript,
                    plan=plan,
                    restoration=restoration,
                    result=result,
                    root_path=root_path,
                    stage_tracker=stage_tracker,
                )
        if not execute:
            artifacts.save_store_result(result)
            return result

        if stop_after == STOP_AFTER_RESTORATION:
            result["operation"] = "restored"
            result["stopped_after"] = STOP_AFTER_RESTORATION
            artifacts.save_store_result(result)
            return result

        with tracked_stage(
            stage_tracker,
            STAGE_WRITER,
            meeting_id=candidate.lookup_id,
        ):
            distilled = require_distiller(self.distiller).distill(
                transcript,
                format_transcript(transcript),
                planning_context=minutes_planning_context_from_plan(plan),
            )
            result["title"] = distilled.title
            result["distiller"] = distilled.distiller
            transcript_paths = save_meeting_pair_to_store(
                self.store,
                transcript,
                distilled,
                root_path=root_path,
                state_path=state_path,
                metadata=store_metadata,
                filename_stem=plan.filename.stem,
                share=plan.share,
            )
            minutes_path = transcript_paths.minutes_path
            result["minutes_path"] = str(minutes_path)
            artifacts.save_transcript_stages(
                original_transcript=edited_transcript,
                restored_transcript=transcript,
                stored_transcript_markdown=transcript_paths.transcript_path.read_text(
                    encoding="utf-8"
                ),
            )
            artifacts.save_minutes(
                minutes=distilled, stored_minutes_markdown=minutes_path.read_text(encoding="utf-8")
            )
            people_intake = intake_people_after_store(
                plan,
                transcript,
                minutes_path,
                root_path=root_path,
                execute=execute,
            )
            if people_intake is not None:
                result["people_intake"] = people_intake
            artifacts.save_store_result(result)
        return result

    def _process_saved_preprocess_candidate(
        self,
        candidate: MeetingCandidate,
        *,
        execute: bool,
        root_path: Path,
        state_path: Path,
        artifact_root: Path | None,
        stage_tracker: StageStatusTracker | None = None,
    ) -> dict[str, Any]:
        artifacts = meeting_artifacts(
            candidate.lookup_id, artifact_root=artifact_root, state_path=state_path
        )
        preprocessed_transcript = artifacts.load_preprocessed_transcript()
        short_result = skip_short_transcript_result(
            candidate, preprocessed_transcript, artifact_dir=artifacts.path
        )
        if short_result is not None:
            if execute:
                artifacts.save_store_result(short_result)
            return short_result
        with tracked_stage(
            stage_tracker,
            STAGE_PLANNER,
            meeting_id=candidate.lookup_id,
        ):
            legend = build_speaker_legend(preprocessed_transcript)
            legend_map = dict(legend)
            judgment = require_judge(self.judge).judge(
                render_judge_input_markdown(preprocessed_transcript, legend),
                read_required_text(root_path / "30_案件" / "clients-overview.md"),
                build_people_index(root_path),
                build_recent_meetings_index(state_path, exclude_meeting_id=candidate.lookup_id),
                legend_map,
            )
            plan = complete_plan_filename_locally(judgment.plan, preprocessed_transcript)
            edits = judgment.edits
            edited_transcript = apply_meeting_judgment(
                preprocessed_transcript, judgment, legend_map
            )
            if execute:
                artifacts.save_judgment(
                    plan=plan,
                    edits=edits,
                    doubt_flags=judgment.doubt_flags,
                    speaker_names_by_token=judgment.speaker_names_by_token,
                    edited_transcript=edited_transcript,
                )
        result = {
            "operation": "planned" if execute else "dry_run",
            "meeting_id": candidate.lookup_id,
            "source_key": preprocessed_transcript.key,
            "title": preprocessed_transcript.title,
            "client": plan.client,
            "meeting_type": plan.meeting_type,
            "filename_stem": plan.filename.stem,
            "edit_count": len(edits),
            "doubt_flag_count": len(judgment.doubt_flags),
            "artifact_dir": str(artifacts.path),
        }
        if not execute:
            return result
        artifacts.save_store_result(result)
        return result

    def _process_saved_plan_candidate(
        self,
        candidate: MeetingCandidate,
        *,
        execute: bool,
        root_path: Path,
        state_path: Path,
        artifact_root: Path | None,
        stage_tracker: StageStatusTracker | None = None,
    ) -> dict[str, Any]:
        artifacts = meeting_artifacts(
            candidate.lookup_id, artifact_root=artifact_root, state_path=state_path
        )
        with tracked_stage(
            stage_tracker,
            STAGE_RESTORER,
            meeting_id=candidate.lookup_id,
        ):
            preprocessed_transcript = artifacts.load_preprocessed_transcript()
            legend = build_speaker_legend(preprocessed_transcript)
            legend_map = dict(legend)
            judgment = artifacts.load_judgment()
            plan = judgment.plan
            people_context = load_people_context(root_path)
            edits = judgment.edits
            edited_transcript = apply_meeting_judgment(
                preprocessed_transcript, judgment, legend_map
            )
            reference_texts = load_reference_texts(
                plan.reference_file_urls,
                hints_by_url={
                    reference.url: reference.hint for reference in plan.additional_references
                },
            )
            artifacts.save_restoration_inputs(
                transcript=edited_transcript,
                plan=plan,
                reference_texts=reference_texts,
            )
            restoration = require_restorer(self.restorer).restore(
                edited_transcript,
                plan,
                reference_texts=reference_texts,
            )
            artifacts.save_restoration_output(restoration)
            transcript = normalize_transcript_people(
                with_plan_speakers(restoration.transcript, plan, people_context),
                people_context.aliases,
            )
            artifacts.save_transcript_stages(
                original_transcript=edited_transcript, restored_transcript=transcript
            )
            result = {
                "operation": "restored" if execute else "dry_run",
                "meeting_id": candidate.lookup_id,
                "source_key": transcript.key,
                "title": transcript.title,
                "client": plan.client,
                "meeting_type": plan.meeting_type,
                "filename_stem": plan.filename.stem,
                "restoration_status": restoration.status,
                "needs_review_count": len(restoration.needs_review_items),
                "terms_candidate_count": len(restoration.terms_candidates),
                "edit_count": len(edits),
                "doubt_flag_count": len(judgment.doubt_flags),
                "artifact_dir": str(artifacts.path),
                "stopped_after": STOP_AFTER_RESTORATION,
            }
            if restoration.error and stage_tracker is not None:
                stage_tracker.warn(
                    STAGE_RESTORER, "restorer.output_warning", meeting_id=candidate.lookup_id
                )
            if execute:
                persist_judgment_doubt_flags(
                    transcript=edited_transcript,
                    plan=plan,
                    doubt_flags=judgment.doubt_flags,
                    result=result,
                    root_path=root_path,
                )
                persist_restoration_findings(
                    transcript=transcript,
                    plan=plan,
                    restoration=restoration,
                    result=result,
                    root_path=root_path,
                    stage_tracker=stage_tracker,
                )
        if not execute:
            return result
        artifacts.save_store_result(result)
        return result

    def _process_saved_restoration_candidate(
        self,
        candidate: MeetingCandidate,
        *,
        execute: bool,
        root_path: Path,
        state_path: Path,
        artifact_root: Path | None,
        stage_tracker: StageStatusTracker | None = None,
    ) -> dict[str, Any]:
        artifacts = meeting_artifacts(
            candidate.lookup_id, artifact_root=artifact_root, state_path=state_path
        )
        store = self.store
        if execute and store is None:
            raise RuntimeError("execute requires circleback_local.local_store")
        with tracked_stage(
            stage_tracker,
            STAGE_WRITER,
            meeting_id=candidate.lookup_id,
        ):
            transcript = artifacts.load_restored_transcript()
            plan = artifacts.load_plan()
            people_context = load_people_context(root_path)
            transcript = normalize_transcript_people(transcript, people_context.aliases)
            plan = complete_plan_filename_locally(plan, transcript)
            transcript = normalize_transcript_people(
                with_plan_speakers(transcript, plan, people_context),
                people_context.aliases,
            )
            result = {
                "operation": "processed" if execute else "dry_run",
                "mode": "restoration_resume",
                "meeting_id": candidate.lookup_id,
                "source_key": transcript.key,
                "title": transcript.title,
                "client": plan.client,
                "meeting_type": plan.meeting_type,
                "filename_stem": plan.filename.stem,
                "artifact_dir": str(artifacts.path),
            }
            if not execute:
                return result
            distilled = require_distiller(self.distiller).distill(
                transcript,
                format_transcript(transcript),
                planning_context=minutes_planning_context_from_plan(plan),
            )
            paths = save_meeting_pair_to_store(
                store,
                transcript,
                distilled,
                root_path=root_path,
                state_path=state_path,
                metadata=store_metadata_from_plan(plan),
                filename_stem=plan.filename.stem,
                share=plan.share,
            )
            result.update(
                {
                    "title": distilled.title,
                    "distiller": distilled.distiller,
                    "transcript_path": str(paths.transcript_path),
                    "minutes_path": str(paths.minutes_path),
                }
            )
            artifacts.write_text(
                "04_store/transcripts/03_saved_transcript.md",
                paths.transcript_path.read_text(encoding="utf-8"),
            )
            artifacts.save_minutes(
                minutes=distilled,
                stored_minutes_markdown=paths.minutes_path.read_text(encoding="utf-8"),
            )
            artifacts.save_store_result(result)
        return result

    def _process_saved_transcript_candidate(
        self,
        candidate: MeetingCandidate,
        *,
        execute: bool,
        state_path: Path,
        artifact_root: Path | None,
        force: bool,
        stage_tracker: StageStatusTracker | None = None,
    ) -> dict[str, Any]:
        record = saved_transcript_record(candidate, state_path=state_path)
        if record is None:
            return {
                "operation": "skip_no_saved_transcript",
                "meeting_id": candidate.lookup_id,
                "source_key": candidate.key,
            }
        transcript_path = saved_transcript_path(record)
        if transcript_path is None:
            return {
                "operation": "skip_no_saved_transcript",
                "meeting_id": candidate.lookup_id,
                "source_key": candidate.key,
            }
        if not transcript_path.exists():
            return {
                "operation": "skip_missing_saved_transcript",
                "meeting_id": candidate.lookup_id,
                "source_key": candidate.key,
                "transcript_path": str(transcript_path),
            }
        minutes_path = saved_minutes_path(record, transcript_path)
        if (
            not force
            and minutes_path.exists()
            and record.get("minutes_needs_regeneration") is not True
        ):
            return {
                "operation": "skip_existing",
                "meeting_id": candidate.lookup_id,
                "source_key": str(record.get("source_key") or candidate.key),
                "minutes_path": str(minutes_path),
            }

        result = {
            "operation": "processed" if execute else "dry_run",
            "mode": "minutes_only",
            "meeting_id": candidate.lookup_id,
            "source_key": str(record.get("source_key") or candidate.key),
            "transcript_path": str(transcript_path),
            "filename_stem": transcript_path.parent.name,
        }
        if not execute:
            return result

        store = self.store
        if store is None:
            raise RuntimeError("execute requires circleback_local.local_store")
        saved_plan_share: str | None = None
        with tracked_stage(
            stage_tracker,
            STAGE_WRITER,
            meeting_id=candidate.lookup_id,
        ):
            transcript = load_saved_transcript(transcript_path, meeting_id=candidate.lookup_id)
            metadata = store_metadata_from_saved_transcript(transcript_path)
            paths = MeetingPaths(
                meeting_id=candidate.lookup_id,
                slug=transcript_path.parent.name,
                meeting_dir=transcript_path.parent,
                transcript_path=transcript_path,
                minutes_path=minutes_path,
            )
            artifacts = meeting_artifacts(
                candidate.lookup_id, artifact_root=artifact_root, state_path=state_path
            )
            plan_path = artifacts.path / "02_judgment" / "meeting_plan_full.json"
            if plan_path.exists():
                plan_payload = artifacts.read_json("02_judgment/meeting_plan_full.json")
                if not isinstance(plan_payload, dict):
                    raise ValueError("artifact meeting plan must be an object")
                if "share" in plan_payload:
                    plan_share = plan_payload["share"]
                    if plan_share not in MEETING_SHARE_VALUES:
                        raise ValueError("artifact meeting plan share must be open or confidential")
                    saved_plan_share = plan_share
            artifacts.reset()
            artifacts.save_transcript_stages(
                original_transcript=transcript,
                restored_transcript=transcript,
                stored_transcript_markdown=transcript_path.read_text(encoding="utf-8"),
            )
            distilled = require_distiller(self.distiller).distill(
                transcript,
                format_transcript(transcript),
                planning_context=minutes_planning_context_from_saved_transcript(
                    metadata, paths.slug
                ),
            )
            minutes_path = save_minutes_to_store(
                store,
                transcript,
                distilled,
                state_path=state_path,
                paths=paths,
                metadata=metadata,
                share=minutes_share_for_plan(minutes_path, saved_plan_share),
            )
            artifacts.save_minutes(
                minutes=distilled, stored_minutes_markdown=minutes_path.read_text(encoding="utf-8")
            )
            result.update(
                {
                    "title": distilled.title,
                    "client": metadata.client,
                    "meeting_type": metadata.meeting_type,
                    "distiller": distilled.distiller,
                    "minutes_path": str(minutes_path),
                }
            )
            artifacts.save_store_result(result)
            store.StateStore(state_path).upsert_meeting(
                candidate.lookup_id, {"minutes_needs_regeneration": False}
            )
        return result


def persist_restoration_findings(
    *,
    transcript: TranscriptDoc,
    plan: MeetingPlan,
    restoration: TranscriptRestoration,
    result: dict[str, Any],
    root_path: Path,
    stage_tracker: StageStatusTracker | None,
) -> None:
    if restoration.needs_review_items:
        result["review_inbox_path"] = str(
            append_review_inbox(root_path, plan, restoration.needs_review_items, transcript)
        )
        if stage_tracker is not None:
            stage_tracker.info(
                STAGE_RESTORER,
                "restorer.review_required",
                count=len(restoration.needs_review_items),
            )
    if restoration.terms_candidates:
        result["terms_candidates_path"] = str(
            append_terms_candidates(root_path, plan, restoration.terms_candidates, transcript)
        )
        if stage_tracker is not None:
            stage_tracker.info(
                STAGE_RESTORER,
                "restorer.terms_candidates",
                count=len(restoration.terms_candidates),
            )


def persist_judgment_doubt_flags(
    *,
    transcript: TranscriptDoc,
    plan: MeetingPlan,
    doubt_flags: tuple[dict[str, Any], ...],
    result: dict[str, Any],
    root_path: Path,
) -> None:
    if doubt_flags:
        result["review_inbox_path"] = str(
            append_review_inbox(
                root_path,
                plan,
                tuple(
                    ReviewItem(
                        id=f"la-doubt-{index}",
                        before=(
                            f"[{str(flag.get('time') or '')}] "
                            f"{str(flag.get('speaker') or '')}: "
                            f"{str(flag.get('quote') or '')}"
                        ),
                        after="",
                        type="L-A疑義",
                        context_block=str(flag.get("note") or ""),
                    )
                    for index, flag in enumerate(doubt_flags, start=1)
                ),
                transcript,
            )
        )


def minutes_planning_context_from_plan(plan: MeetingPlan) -> dict[str, Any]:
    return {
        "client": plan.client,
        "client_url": plan.client_url,
        "meeting_type": plan.meeting_type,
        "meeting_context": {
            "overview": plan.meeting_context.overview,
            "main_topics": list(plan.meeting_context.main_topics),
            "context_keywords": list(plan.meeting_context.context_keywords),
        },
        "additional_references": [
            {"url": reference.url, "hint": reference.hint}
            for reference in plan.additional_references
        ],
        "filename": {
            "subject": plan.filename.subject,
            "summary": plan.filename.summary,
            "stem": plan.filename.stem,
        },
    }


def minutes_planning_context_from_saved_transcript(
    metadata: StoreMetadata, filename_stem: str
) -> dict[str, Any]:
    return {
        "client": metadata.client,
        "client_url": "",
        "meeting_type": metadata.meeting_type,
        "filename": {
            "subject": saved_filename_subject(filename_stem, metadata.client),
            "summary": saved_filename_summary(filename_stem),
            "stem": filename_stem,
        },
    }


def saved_filename_subject(filename_stem: str, client: str) -> str:
    if client:
        return client
    parts = filename_stem.split("_")
    return parts[1] if len(parts) >= 2 else filename_stem


def saved_filename_summary(filename_stem: str) -> str:
    parts = filename_stem.rsplit("_", 1)
    return parts[1] if len(parts) == 2 else filename_stem


def complete_plan_filename_locally(plan: MeetingPlan, transcript: TranscriptDoc) -> MeetingPlan:
    filename_date = transcript.date.replace("-", "")
    filename = build_meeting_plan_filename(
        filename_date,
        plan.filename.subject,
        plan.meeting_type,
        plan.filename.summary,
    )
    if plan.filename == filename:
        return plan
    return dataclasses.replace(plan, filename=filename)


def existing_minutes_record(
    candidate: MeetingCandidate, *, state_path: Path = DEFAULT_STATE_PATH
) -> dict[str, Any] | None:
    """Use state as the ledger; brain file existence is never an existing check."""
    from .local_store import StateStore

    return StateStore(state_path).meeting(candidate.lookup_id)


def collect_saved_transcript_candidates(config: PipelineConfig) -> list[MeetingCandidate]:
    """Only explicit regeneration flags restore minutes; transcript-only files do not."""
    from .local_store import StateStore

    if config.meeting_ids:
        return [
            MeetingCandidate(lookup_id=circleback_lookup_id(meeting_id))
            for meeting_id in config.meeting_ids
        ]
    meetings = StateStore(config.state_path).data.get("meetings")
    if not isinstance(meetings, dict):
        return []
    candidates: list[MeetingCandidate] = []
    for meeting_id, record in sorted(meetings.items()):
        if isinstance(record, dict) and record.get("minutes_needs_regeneration") is True:
            candidates.append(MeetingCandidate(lookup_id=circleback_lookup_id(str(meeting_id))))
    return candidates


def saved_transcript_record(
    candidate: MeetingCandidate, *, state_path: Path = DEFAULT_STATE_PATH
) -> dict[str, Any] | None:
    from .local_store import StateStore

    state = StateStore(state_path)
    record = state.meeting(candidate.lookup_id)
    if record is not None:
        return record
    meetings = state.data.get("meetings")
    if not isinstance(meetings, dict):
        return None
    for value in meetings.values():
        if isinstance(value, dict) and value.get("source_key") == candidate.key:
            return value
    return None


def saved_transcript_path(record: dict[str, Any]) -> Path | None:
    value = record.get("transcript_path")
    return Path(str(value)) if value else None


def saved_minutes_path(record: dict[str, Any], transcript_path: Path) -> Path:
    value = record.get("minutes_path")
    if value:
        return Path(str(value))
    return transcript_path.parent / "minutes.md"


def excluded_meeting_ids_for_state(state_path: Path) -> frozenset[str]:
    from .local_store import StateStore

    excluded_meetings = StateStore(state_path).data.get("excluded_meetings", {})
    return frozenset(excluded_meetings)


def load_saved_transcript(path: Path, *, meeting_id: str) -> TranscriptDoc:
    markdown = path.read_text(encoding="utf-8")
    frontmatter, body = split_frontmatter(markdown)
    date = str(frontmatter.get("date") or "").strip()
    if not date:
        raise ValueError(f"saved transcript missing date: {path}")
    utterances = parse_saved_transcript_utterances(body, path=path)
    if not utterances:
        raise ValueError(f"saved transcript has no utterances: {path}")
    attendees = tuple(Attendee(name) for name in frontmatter_list(frontmatter, "attendees"))
    return TranscriptDoc(
        meeting_id=meeting_id,
        title=str(frontmatter.get("title") or path.parent.name),
        started_at=f"{date}T00:00:00+09:00",
        duration_seconds=None,
        attendees=attendees,
        utterances=utterances,
    )


def parse_saved_transcript_utterances(body: str, *, path: Path) -> tuple[Utterance, ...]:
    lines = body.splitlines()
    try:
        start = lines.index(TRANSCRIPT_SECTION_HEADING) + 1
    except ValueError as exc:
        raise ValueError(f"saved transcript missing {TRANSCRIPT_SECTION_HEADING}: {path}") from exc
    utterances: list[Utterance] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        if not line.strip():
            continue
        parts = line.split("|", 3)
        if len(parts) != 4 or not parts[0].strip().isdigit():
            raise ValueError(f"saved transcript has malformed utterance line: {path}")
        timestamp = parts[1].strip()
        utterances.append(
            Utterance(
                parse_timestamp_seconds(timestamp), timestamp, parts[2].strip(), parts[3].strip()
            )
        )
    return tuple(utterances)


def parse_timestamp_seconds(timestamp: str) -> int | None:
    parts = timestamp.split(":")
    if len(parts) not in {2, 3} or any(not part.isdigit() for part in parts):
        return None
    values = [int(part) for part in parts]
    if len(values) == 2:
        minutes, seconds = values
        return minutes * 60 + seconds
    hours, minutes, seconds = values
    return hours * 3600 + minutes * 60 + seconds


def store_metadata_from_saved_transcript(path: Path) -> StoreMetadata:
    frontmatter, _body = split_frontmatter(path.read_text(encoding="utf-8"))
    return StoreMetadata(
        client=str(frontmatter.get("client") or ""),
        meeting_type=str(frontmatter.get("type") or ""),
        tags=tuple(frontmatter_list(frontmatter, "tags")),
    )


def frontmatter_list(frontmatter: dict[str, Any], key: str) -> tuple[str, ...]:
    value = frontmatter.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def save_minutes_to_store(
    store: Any,
    transcript: TranscriptDoc,
    minutes: DistilledMinutes,
    *,
    metadata: StoreMetadata,
    paths: Any,
    state_path: Path = DEFAULT_STATE_PATH,
    share: str,
) -> Path:
    state = store.StateStore(state_path)
    return store.save_minutes(
        to_store_minutes(minutes, transcript, metadata),
        paths,
        state=state,
        share=share,
    )


def save_meeting_pair_to_store(
    store: Any,
    transcript: TranscriptDoc,
    minutes: DistilledMinutes,
    *,
    metadata: StoreMetadata,
    root_path: Path = BRAIN_ROOT,
    state_path: Path = DEFAULT_STATE_PATH,
    filename_stem: str,
    share: str,
) -> MeetingPaths:
    state = store.StateStore(state_path)
    return store.save_meeting_pair(
        to_store_transcript(transcript, metadata),
        to_store_minutes(minutes, transcript, metadata),
        root=root_path,
        state=state,
        filename_stem=filename_stem,
        share=share,
    )


def minutes_share_for_plan(minutes_path: Path, planned_share: str | None) -> str:
    if not minutes_path.is_file():
        if planned_share is None:
            raise ValueError("artifact meeting plan share must be open or confidential")
        return planned_share
    frontmatter, _body = split_frontmatter(minutes_path.read_text(encoding="utf-8"))
    return meeting_share(frontmatter)


def to_store_transcript(transcript: TranscriptDoc, metadata: StoreMetadata) -> Any:
    from .models import Attendee as StoreAttendee
    from .models import Transcript, Utterance

    return Transcript(
        meeting_id=transcript.meeting_id,
        title=transcript.title,
        date=transcript_date(transcript),
        attendees=tuple(
            StoreAttendee(attendee.name, attendee.email) for attendee in transcript.attendees
        ),
        utterances=tuple(
            Utterance(utterance.timestamp, utterance.speaker, utterance.text)
            for utterance in transcript.utterances
        ),
        client=metadata.client,
        meeting_type=metadata.meeting_type,
        tags=metadata.tags,
        duration_seconds=transcript.duration_seconds,
    )


def to_store_minutes(
    minutes: DistilledMinutes, transcript: TranscriptDoc, metadata: StoreMetadata
) -> Any:
    from .models import Minutes

    return Minutes(
        meeting_id=transcript.meeting_id,
        title=minutes.title,
        date=transcript_date(transcript),
        attendees=tuple(attendee.name for attendee in transcript.attendees),
        client=metadata.client,
        meeting_type=metadata.meeting_type,
        tags=minutes.tags,
        markdown_body=minutes.markdown,
        notable_quotes=extract_quote_lines(minutes.markdown),
    )


def transcript_date(transcript: TranscriptDoc) -> str:
    return transcript.date


def parse_datetime(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def extract_quote_lines(markdown: str) -> tuple[str, ...]:
    in_quotes = False
    quotes: list[str] = []
    for line in markdown.splitlines():
        if line.strip() == "## 注目発言":
            in_quotes = True
            continue
        if in_quotes and line.startswith("## "):
            break
        if in_quotes:
            stripped = line.lstrip("- >\t").strip()
            if stripped:
                quotes.append(stripped)
    return tuple(quotes)


def load_default_store() -> Any:
    try:
        from circleback_local import local_store
    except ImportError as exc:
        raise RuntimeError("circleback_local.local_store is not available yet") from exc

    return local_store


def run_pipeline(
    args: Any, stage_tracker: StageStatusTracker | None = None
) -> list[dict[str, Any]]:
    execute = bool(getattr(args, "execute", False))
    if not execute:
        return [{"operation": "dry_run", "limit": getattr(args, "limit", 20)}]

    root_path = Path(getattr(args, "root_path", BRAIN_ROOT))
    state_path = Path(getattr(args, "state_path", DEFAULT_STATE_PATH))
    stage = validate_backfill_stage(getattr(args, "stage", None))
    stop_after = validate_stop_after(getattr(args, "stop_after", STOP_AFTER_MINUTES))
    minutes_only = bool(getattr(args, "minutes_only", False))
    if stage == BACKFILL_STAGE_PREPROCESS:
        stop_after = STOP_AFTER_CIRCLEBACK
    elif stage == BACKFILL_STAGE_LLM1:
        stop_after = STOP_AFTER_PLANNING
    elif stage == BACKFILL_STAGE_LLM2:
        stop_after = STOP_AFTER_RESTORATION
    elif stage == BACKFILL_STAGE_LLM3:
        stop_after = STOP_AFTER_MINUTES
        minutes_only = True
    all_meetings = bool(getattr(args, "all_meetings", False))
    if all_meetings and getattr(args, "meeting_id", None):
        raise ValueError("--all does not support explicit meeting IDs")
    if all_meetings and stop_after != STOP_AFTER_CIRCLEBACK:
        raise ValueError("--all requires stage=preprocess or stop_after=circleback")
    if minutes_only:
        validate_minutes_only_config(stop_after=stop_after)
    cutover_started_at = None
    if stage is None and not all_meetings and not minutes_only:
        from .local_store import StateStore

        cutover_started_at = StateStore(state_path).ensure_cutover_started_at(
            getattr(args, "cutover_started_at", None)
        )

    store = load_default_store() if execute else None
    distiller = None
    if stop_after == STOP_AFTER_MINUTES:
        distiller = get_distiller(
            "codex",
            codex_bin=getattr(args, "codex_bin", DEFAULT_CODEX_BIN),
            timeout_seconds=getattr(args, "distiller_timeout", 3600),
            model=DEFAULT_CODEX_MODEL,
            effort=DEFAULT_CODEX_EFFORT,
        )
    judge = None
    restorer = None
    if not minutes_only and stop_after != STOP_AFTER_CIRCLEBACK and stage != BACKFILL_STAGE_LLM2:
        judge = ClaudeMeetingJudge(
            claude_bin=getattr(args, "claude_bin", DEFAULT_CLAUDE_BIN),
            timeout_seconds=getattr(args, "judge_timeout", DEFAULT_JUDGE_TIMEOUT_SECONDS),
        )
    if (
        not minutes_only
        and stop_after not in {STOP_AFTER_CIRCLEBACK, STOP_AFTER_PLANNING}
        and stage != BACKFILL_STAGE_LLM1
    ):
        restorer = CodexTranscriptRestorer(
            codex_bin=getattr(args, "codex_bin", DEFAULT_CODEX_BIN),
            timeout_seconds=getattr(args, "restorer_timeout", 3600),
            model=DEFAULT_CODEX_MODEL,
            effort=DEFAULT_CODEX_EFFORT,
            inline_effort=DEFAULT_CODEX_EFFORT,
        )

    pipeline = CirclebackLocalPipeline(
        client=CirclebackClient(getattr(args, "circleback_bin", "circleback")),
        distiller=distiller,
        store=store,
        judge=judge,
        restorer=restorer,
    )
    summary = pipeline.run_once(
        PipelineConfig(
            execute=execute,
            meeting_ids=tuple(getattr(args, "meeting_id", ()) or ()),
            search=getattr(args, "search", None),
            last_days=ALL_MEETINGS_LAST_DAYS if all_meetings else getattr(args, "last_days", 7),
            from_date=getattr(args, "from_date", None),
            to_date=getattr(args, "to_date", None),
            limit=None if all_meetings else getattr(args, "limit", 20),
            page=0 if all_meetings else getattr(args, "page", 1),
            pages=getattr(args, "pages", None)
            or (ALL_MEETINGS_MAX_PAGES if all_meetings else None),
            cutover_started_at=cutover_started_at,
            root_path=root_path,
            state_path=state_path,
            artifact_root=Path(args.artifact_root)
            if getattr(args, "artifact_root", None)
            else None,
            force=bool(getattr(args, "force", False)),
            stop_after=stop_after,
            minutes_only=minutes_only,
            backfill_stage=stage,
            stage_tracker=stage_tracker,
        )
    )
    return list(summary["results"])


def failed_candidate_result(
    candidate: MeetingCandidate,
    exc: BaseException,
    *,
    artifact_dir: Path | None,
    failed_stage: str | None,
) -> dict[str, Any]:
    return {
        "operation": "failed",
        "meeting_id": candidate.lookup_id,
        "source_key": candidate.key,
        "error_type": type(exc).__name__,
        "failed_stage": failed_stage,
        "error_message": short_log_detail(str(exc), limit=500),
        "artifact_dir": str(artifact_dir) if artifact_dir is not None else None,
    }


def skip_short_transcript_result(
    candidate: MeetingCandidate,
    transcript: TranscriptDoc,
    *,
    artifact_dir: Path,
) -> dict[str, Any] | None:
    utterance_count = len(transcript.utterances)
    if utterance_count >= MIN_TRANSCRIPT_UTTERANCES_FOR_LLM:
        return None
    return {
        "operation": "skip_short_transcript",
        "meeting_id": candidate.lookup_id,
        "source_key": candidate.key,
        "title": transcript.title,
        "utterance_count": utterance_count,
        "min_utterances": MIN_TRANSCRIPT_UTTERANCES_FOR_LLM,
        "artifact_dir": str(artifact_dir),
    }


def validate_stop_after(value: str) -> str:
    if value not in STOP_AFTER_VALUES:
        raise ValueError(f"stop_after must be one of: {', '.join(sorted(STOP_AFTER_VALUES))}")
    return value


def validate_backfill_stage(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in BACKFILL_STAGE_VALUES:
        raise ValueError(
            f"backfill_stage must be one of: {', '.join(sorted(BACKFILL_STAGE_VALUES))}"
        )
    return value


def stop_after_for_backfill_stage(stage: str | None) -> str | None:
    if stage == BACKFILL_STAGE_PREPROCESS:
        return STOP_AFTER_CIRCLEBACK
    if stage == BACKFILL_STAGE_LLM1:
        return STOP_AFTER_PLANNING
    if stage == BACKFILL_STAGE_LLM2:
        return STOP_AFTER_RESTORATION
    if stage == BACKFILL_STAGE_LLM3:
        return STOP_AFTER_MINUTES
    return None


def validate_minutes_only_config(*, stop_after: str) -> None:
    if stop_after != STOP_AFTER_MINUTES:
        raise ValueError("minutes_only requires stop_after=minutes")


def should_track_candidate_collection(minutes_only: bool, backfill_stage: str | None) -> bool:
    return not minutes_only and backfill_stage not in {
        BACKFILL_STAGE_LLM1,
        BACKFILL_STAGE_LLM2,
        BACKFILL_STAGE_LLM3,
    }


def dedupe_candidates(candidates: Iterable[MeetingCandidate]) -> list[MeetingCandidate]:
    result: list[MeetingCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate.key
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


@contextmanager
def meeting_lock(meeting_id: str, state_path: Path, *, enabled: bool) -> Any:
    if not enabled:
        yield True
        return
    lock_root = state_path.parent / "locks" / "meetings"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_root.chmod(0o700)
    lock_path = lock_root / f"{sanitize_lock_name(meeting_id)}.lock"
    fd: int | None = None
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            fd = None
            yield False
            return
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        yield True
    finally:
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def sanitize_lock_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "meeting"


def read_required_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_people_context(root_path: Path) -> PeopleContext:
    members_path = root_path / "40_人物組織" / "members.json"
    if root_path == BRAIN_ROOT and not members_path.is_file():
        members_path = DEFAULT_MEMBERS_JSON_PATH
    return PeopleContext(
        aliases=load_speaker_aliases(DEFAULT_SPEAKER_ALIASES_PATH),
        roster=load_roster(members_path, DEFAULT_INTERNAL_PEOPLE_PATH),
        member_names=load_member_names(members_path),
        person_name_stems=load_person_name_stems(root_path),
    )


def normalize_plan_speakers(
    plan: MeetingPlan, aliases: dict[str, str]
) -> tuple[MeetingPlanSpeaker, ...]:
    speakers: list[MeetingPlanSpeaker] = []
    seen: set[str] = set()
    for speaker in plan.speakers:
        normalized_name = normalize_speaker_name(speaker, aliases)
        if not normalized_name or normalized_name in seen:
            continue
        seen.add(normalized_name)
        speakers.append(dataclasses.replace(speaker, name=normalized_name))
    return tuple(speakers)


def normalize_speaker_name(speaker: MeetingPlanSpeaker, aliases: dict[str, str]) -> str:
    candidates = (speaker.name, *speaker.raw)
    for candidate in candidates:
        cleaned = str(candidate or "").strip()
        normalized = normalize_person_name(cleaned, aliases)
        if normalized and normalized != cleaned:
            return normalized
    return normalize_person_name(speaker.name, aliases)


def normalize_transcript_people(
    transcript: TranscriptDoc, aliases: dict[str, str]
) -> TranscriptDoc:
    attendees = tuple(
        dataclasses.replace(attendee, name=normalize_person_name(attendee.name, aliases))
        for attendee in transcript.attendees
    )
    utterances = tuple(
        dataclasses.replace(utterance, speaker=normalize_person_name(utterance.speaker, aliases))
        for utterance in transcript.utterances
    )
    if attendees == transcript.attendees and utterances == transcript.utterances:
        return transcript
    return dataclasses.replace(transcript, attendees=attendees, utterances=utterances)


def intake_people_after_store(
    plan: MeetingPlan,
    transcript: TranscriptDoc,
    minutes_path: Path,
    *,
    root_path: Path,
    execute: bool,
) -> dict[str, Any] | None:
    persons_dir = root_path / "40_人物組織" / "persons"
    if not persons_dir.is_dir():
        return None
    people_context = load_people_context(root_path)
    meeting = IntakeMeeting(
        meeting_id=transcript.key,
        meeting_title=transcript.title,
        meeting_path=minutes_path,
        attendees=people_intake_attendees(plan, transcript, people_context),
        context=people_intake_context(plan),
    )
    report = intake_people_for_meeting(
        meeting,
        persons_dir=persons_dir,
        aliases=people_context.aliases,
        execute=execute,
    )
    if (
        not report.created
        and not report.would_create
        and not report.skipped
        and not report.warnings
    ):
        return None
    return intake_report_payload(report)


def people_intake_attendees(
    plan: MeetingPlan,
    transcript: TranscriptDoc,
    people_context: PeopleContext,
) -> tuple[str, ...]:
    attendees: list[str] = []
    seen: set[str] = set()
    for speaker in plan.speakers:
        add_unique_person_name(
            attendees,
            seen,
            normalize_speaker_name(speaker, people_context.aliases),
        )
    for attendee in transcript.attendees:
        add_unique_person_name(
            attendees,
            seen,
            normalize_person_name(attendee.name, people_context.aliases),
        )
    return tuple(attendees)


def add_unique_person_name(names: list[str], seen: set[str], name: str) -> None:
    if not name or name in seen:
        return
    seen.add(name)
    names.append(name)


def people_intake_context(plan: MeetingPlan) -> str:
    parts = [
        plan.meeting_context.overview,
        *plan.meeting_context.main_topics,
        *plan.meeting_context.context_keywords,
    ]
    return "\n".join(part for part in parts if part)


def build_people_index(config_root: Path) -> str:
    people_root = config_root / "40_人物組織" / "persons"
    if not people_root.exists():
        people_root = BRAIN_ROOT / "40_人物組織" / "persons"
    organizations_root = config_root / "40_人物組織" / "organizations"
    if not organizations_root.exists():
        organizations_root = BRAIN_ROOT / "40_人物組織" / "organizations"

    lines = ["## Persons"]
    if people_root.exists():
        for path in sorted(people_root.glob("*.md")):
            lines.append(
                format_people_index_entry(
                    "person", path, PEOPLE_INDEX_PERSON_KEYS, ("関連組織", "概要")
                )
            )
    else:
        lines.append("- なし")

    lines.extend(("", "## Organizations"))
    if organizations_root.exists():
        for path in sorted(organizations_root.glob("*.md")):
            lines.append(
                format_people_index_entry(
                    "organization", path, PEOPLE_INDEX_ORGANIZATION_KEYS, ("関連人物",)
                )
            )
    else:
        lines.append("- なし")
    return "\n".join(lines)


def format_people_index_entry(
    kind: str, path: Path, keys: tuple[str, ...], sections: tuple[str, ...]
) -> str:
    signals = extract_markdown_signals(path, keys, sections)
    suffix = f" | {signals}" if signals else ""
    return f"- [{kind}] {path.stem}: file://{path}{suffix}"


def extract_markdown_signals(path: Path, keys: tuple[str, ...], sections: tuple[str, ...]) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    signals: list[str] = []
    for key in keys:
        match = re.search(rf"^- {re.escape(key)}:\s*(.+)$", text, flags=re.MULTILINE)
        if match:
            signals.append(f"{key}: {clean_index_signal(match.group(1))}")
    for section in sections:
        values = markdown_section_bullets(text, section, max_items=4)
        if values:
            signals.append(f"{section}: {', '.join(values)}")
    return trim_index_signal("; ".join(signals))


def markdown_section_bullets(text: str, section: str, *, max_items: int) -> list[str]:
    values: list[str] = []
    in_section = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_section = line[3:].strip() == section
            continue
        if in_section and line.startswith("#"):
            break
        if not in_section:
            continue
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        value = clean_index_signal(stripped[2:])
        if value:
            values.append(value)
        if len(values) >= max_items:
            break
    return values


def clean_index_signal(value: str) -> str:
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"`([^`]+)`", r"\1", value)
    return " ".join(value.split())


def trim_index_signal(value: str, *, max_chars: int = 360) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3].rstrip() + "..."


def require_judge(judge: Any | None) -> Any:
    if judge is None:
        raise RuntimeError("CirclebackLocalPipeline requires meeting judge")
    return judge


def require_restorer(restorer: TranscriptRestorer | None) -> TranscriptRestorer:
    if restorer is None:
        raise RuntimeError("CirclebackLocalPipeline requires transcript restorer")
    return restorer


def require_distiller(distiller: Distiller | None) -> Distiller:
    if distiller is None:
        raise RuntimeError("CirclebackLocalPipeline requires minutes distiller")
    return distiller


def store_metadata_from_plan(plan: MeetingPlan) -> StoreMetadata:
    return StoreMetadata(client=plan.client, meeting_type=plan.meeting_type, tags=())


def saved_attendee_names(
    transcript: TranscriptDoc,
    plan: MeetingPlan,
    people_context: PeopleContext,
) -> tuple[str, ...]:
    attendees: list[str] = []
    seen: set[str] = set()
    for speaker in plan.speakers:
        add_unique_person_name(
            attendees,
            seen,
            normalize_speaker_name(speaker, people_context.aliases),
        )
    for attendee in transcript.attendees:
        normalized_name = normalize_person_name(attendee.name, people_context.aliases)
        if not is_saved_metadata_attendee(normalized_name, people_context):
            continue
        add_unique_person_name(attendees, seen, normalized_name)
    return tuple(attendees)


def is_saved_metadata_attendee(name: str, people_context: PeopleContext) -> bool:
    return name in people_context.roster or name in people_context.person_name_stems


def with_plan_speakers(
    transcript: TranscriptDoc,
    plan: MeetingPlan,
    people_context: PeopleContext | None = None,
) -> TranscriptDoc:
    if people_context is not None:
        attendee_names = saved_attendee_names(transcript, plan, people_context)
        if not attendee_names:
            raise ValueError("meeting plan must include speakers")
        return dataclasses.replace(
            transcript, attendees=tuple(Attendee(name) for name in attendee_names)
        )

    speaker_names = tuple(
        getattr(speaker, "name", "") for speaker in plan.speakers if getattr(speaker, "name", "")
    )
    if not speaker_names:
        raise ValueError("meeting plan must include speakers")
    if transcript.attendees:
        return transcript

    return dataclasses.replace(
        transcript, attendees=tuple(Attendee(name) for name in speaker_names)
    )
