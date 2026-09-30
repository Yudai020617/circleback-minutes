from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable

from .runtime import local_runtime

STAGE_GET = "get"
STAGE_PLANNER = "planner"
STAGE_RESTORER = "restorer"
STAGE_WRITER = "writer"
STAGE_ORDER = (STAGE_GET, STAGE_PLANNER, STAGE_RESTORER, STAGE_WRITER)
STAGE_JOB_IDS = {
    STAGE_GET: "circleback-get",
    STAGE_PLANNER: "circleback-planner",
    STAGE_RESTORER: "circleback-restorer",
    STAGE_WRITER: "circleback-writer",
}
STAGE_LABELS = {
    STAGE_GET: "Get",
    STAGE_PLANNER: "Planner",
    STAGE_RESTORER: "Restorer",
    STAGE_WRITER: "Writer",
}
NORMAL_SKIP_REASONS = frozenset(
    {"backfill_not_requested", "no_candidates", "no_work", "stop_after"}
)


@dataclasses.dataclass
class StageStats:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    warning_count: int = 0
    info_count: int = 0
    candidate_count: int = 0
    skipped_upstream_count: int = 0
    latest_failed_meeting: str | None = None
    latest_error_type: str | None = None
    failure_reason_code: str | None = None
    warning_reason_code: str | None = None
    info_reason_code: str | None = None


StatusWriter = Callable[[str, str, str, str, str], bool]


class StageStatusTracker:
    def __init__(
        self,
        *,
        write_status: StatusWriter,
        requested_stages: Iterable[str] = STAGE_ORDER,
        skip_reason_by_stage: dict[str, str] | None = None,
    ) -> None:
        self.write_status = write_status
        self.requested_stages = set(requested_stages)
        self.skip_reason_by_stage = skip_reason_by_stage or {}
        self.stats = {stage: StageStats() for stage in STAGE_ORDER}
        self.failed_stage_by_meeting: dict[str, str] = {}
        self.writer_failed = False
        self.candidate_count = 0

    def initialize_run(self) -> bool:
        ok = True
        for stage in STAGE_ORDER:
            ok = (
                self._write(
                    stage,
                    "normal",
                    "pending",
                    "run_started",
                    f"{STAGE_LABELS[stage]} pending: run started",
                )
                and ok
            )
        return ok

    def record_candidates(self, candidate_count: int) -> None:
        self.candidate_count = candidate_count
        self.stats[STAGE_GET].candidate_count = candidate_count

    def start(self, stage: str, *, meeting_id: str | None = None) -> None:
        stats = self._stats(stage)
        stats.attempted += 1
        self._write(
            stage,
            "normal",
            "attempted",
            "started",
            self._stage_message(stage, "started", meeting_id=meeting_id),
        )
        self._write(
            stage,
            "normal",
            "running",
            "in_progress",
            self._stage_message(stage, "running", meeting_id=meeting_id),
        )

    def succeed(self, stage: str) -> None:
        stats = self._stats(stage)
        stats.succeeded += 1
        self._write_attempted_result(stage, stats)

    def warn(
        self,
        stage: str,
        reason_code: str,
        *,
        meeting_id: str | None = None,
    ) -> None:
        stats = self._stats(stage)
        stats.warning_count += 1
        stats.warning_reason_code = reason_code
        if meeting_id:
            stats.latest_failed_meeting = meeting_id

    def info(
        self,
        stage: str,
        reason_code: str,
        *,
        count: int = 1,
    ) -> None:
        stats = self._stats(stage)
        stats.info_count += count
        stats.info_reason_code = reason_code

    def fail(
        self,
        stage: str,
        exc: BaseException,
        *,
        meeting_id: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        stats = self._stats(stage)
        stats.failed += 1
        stats.latest_error_type = type(exc).__name__
        if reason_code is not None:
            stats.failure_reason_code = reason_code
        if meeting_id:
            stats.latest_failed_meeting = meeting_id
            self.failed_stage_by_meeting[meeting_id] = stage
        self._write_attempted_result(stage, stats)

    def failure_stage_for(self, meeting_id: str) -> str | None:
        return self.failed_stage_by_meeting.get(meeting_id)

    def first_failure_reason_code(self) -> str | None:
        return next(
            (
                self.stats[stage].failure_reason_code
                for stage in STAGE_ORDER
                if self.stats[stage].failure_reason_code is not None
            ),
            None,
        )

    def finalize(self) -> bool:
        ok = True
        upstream_failed = False
        for stage in STAGE_ORDER:
            stats = self.stats[stage]
            if stats.failed:
                upstream_failed = True
            if stats.failed or stats.warning_count or stats.attempted:
                ok = self._write_attempted_result(stage, stats) and ok
                continue

            reason_code = self._skip_reason(stage, upstream_failed)
            if reason_code == "upstream_failed":
                stats.skipped_upstream_count += 1
            ok = (
                self._write(
                    stage,
                    "normal" if reason_code in NORMAL_SKIP_REASONS else "warning",
                    "skipped",
                    reason_code,
                    f"{STAGE_LABELS[stage]} skipped: {reason_code}",
                )
                and ok
            )
        return ok and not self.writer_failed

    def _write_attempted_result(self, stage: str, stats: StageStats) -> bool:
        if stats.failed:
            return self._write(
                stage,
                "error",
                "attempted",
                stats.failure_reason_code or f"{stage}.failed",
                self._failure_message(stage, stats),
            )
        if stats.warning_count:
            return self._write(
                stage,
                "warning",
                "attempted",
                stats.warning_reason_code or f"{stage}.warning",
                self._warning_message(stage, stats),
            )
        return self._write(
            stage,
            "normal",
            "attempted",
            stats.info_reason_code or "completed",
            self._success_message(stage, stats),
        )

    def _stats(self, stage: str) -> StageStats:
        if stage not in self.stats:
            raise ValueError(f"unknown Circleback stage: {stage}")
        return self.stats[stage]

    def _write(
        self, stage: str, status: str, run_state: str, reason_code: str, message: str
    ) -> bool:
        ok = self.write_status(STAGE_JOB_IDS[stage], status, run_state, reason_code, message)
        if not ok:
            self.writer_failed = True
        return ok

    def _skip_reason(self, stage: str, upstream_failed: bool) -> str:
        if upstream_failed:
            return "upstream_failed"
        if self.candidate_count == 0:
            return "no_candidates"
        if stage not in self.requested_stages:
            return self.skip_reason_by_stage.get(stage, "backfill_not_requested")
        return "no_work"

    @staticmethod
    def _stage_message(stage: str, action: str, *, meeting_id: str | None = None) -> str:
        suffix = f" meeting={meeting_id}" if meeting_id else ""
        return f"{STAGE_LABELS[stage]} {action}{suffix}"

    @staticmethod
    def _failure_message(stage: str, stats: StageStats) -> str:
        parts = [
            f"{STAGE_LABELS[stage]} failed",
            f"failed={stats.failed}",
            f"attempted={stats.attempted}",
        ]
        if stats.latest_failed_meeting:
            parts.append(f"meeting={stats.latest_failed_meeting}")
        if stats.latest_error_type:
            parts.append(f"error={stats.latest_error_type}")
        return " ".join(parts)

    @staticmethod
    def _warning_message(stage: str, stats: StageStats) -> str:
        parts = [
            f"{STAGE_LABELS[stage]} warning",
            f"warnings={stats.warning_count}",
            f"attempted={stats.attempted}",
        ]
        if stats.latest_failed_meeting:
            parts.append(f"meeting={stats.latest_failed_meeting}")
        return " ".join(parts)

    @staticmethod
    def _success_message(stage: str, stats: StageStats) -> str:
        parts = [
            f"{STAGE_LABELS[stage]} completed",
            f"succeeded={stats.succeeded}",
            f"attempted={stats.attempted}",
        ]
        if stats.candidate_count:
            parts.append(f"candidates={stats.candidate_count}")
        if stats.info_count:
            parts.append(f"review_count={stats.info_count}")
        return " ".join(parts)


def local_stage_status_tracker(
    *,
    process_marker: str,
    requested_stages: Iterable[str] = STAGE_ORDER,
    skip_reason_by_stage: dict[str, str] | None = None,
) -> StageStatusTracker:
    def write_status(
        job_id: str, status: str, run_state: str, reason_code: str, message: str
    ) -> bool:
        return local_runtime(job_id, process_marker).write_status(
            status, run_state, reason_code, message
        )

    return StageStatusTracker(
        write_status=write_status,
        requested_stages=requested_stages,
        skip_reason_by_stage=skip_reason_by_stage,
    )
