#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True

APP_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = APP_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

# Resolve the CLI workspace before any module-level path constants are imported.
_bootstrap = argparse.ArgumentParser(add_help=False)
_bootstrap.add_argument("--root-path")
_bootstrap_args, _ = _bootstrap.parse_known_args()
if _bootstrap_args.root_path:
    os.environ["MINUTES_WORKSPACE"] = str(Path(_bootstrap_args.root_path).expanduser().resolve())

from circleback_local.circleback_client import DEFAULT_CIRCLEBACK_BIN  # noqa: E402
from circleback_local.claude_exec import DEFAULT_CLAUDE_BIN  # noqa: E402
from circleback_local.codex_exec import DEFAULT_CODEX_BIN  # noqa: E402
from circleback_local.execution_gate import (  # noqa: E402
    GateClass,
    add_execution_gate_arguments,
    apply_execution_gate,
    parse_execution_gate,
)
from circleback_local.judgment import DEFAULT_JUDGE_TIMEOUT_SECONDS  # noqa: E402
from circleback_local.runtime import (  # noqa: E402
    ensure_private_dir,
    ensure_runtime_python_314,
    local_runtime,
    set_owner_only_umask,
)
from circleback_local.models import circleback_source_key  # noqa: E402
from circleback_local.paths import BRAIN_ROOT, DEFAULT_STATE_PATH  # noqa: E402
from circleback_local.pipeline import (  # noqa: E402
    BACKFILL_STAGE_LLM1,
    BACKFILL_STAGE_LLM2,
    BACKFILL_STAGE_LLM3,
    BACKFILL_STAGE_PREPROCESS,
    BACKFILL_STAGE_VALUES,
    STOP_AFTER_CIRCLEBACK,
    STOP_AFTER_MINUTES,
    STOP_AFTER_PLANNING,
    STOP_AFTER_RESTORATION,
    STOP_AFTER_VALUES,
    run_pipeline,
)
from circleback_local.stage_status import (  # noqa: E402
    STAGE_GET,
    STAGE_ORDER,
    STAGE_PLANNER,
    STAGE_RESTORER,
    STAGE_WRITER,
    StageStatusTracker,
    local_stage_status_tracker,
)

JOB_ID = "circleback-local-meetings-sync"
FORCE_CONFIRM_EXACT = "force:circleback-local-minutes:pipeline"
SKIPPED_EXIT_CODE = 75
PARTIAL_EXIT_CODE = 76


def main() -> int:
    set_owner_only_umask()
    args = parse_args()
    if args.execute:
        ensure_runtime_dirs()
    runtime = local_runtime(JOB_ID, Path(__file__).name)
    final_reason_code: str | None = None
    stage_tracker: StageStatusTracker | None = None
    stage_status_finalized = False
    failure_summary: dict[str, object] | None = None
    try:
        ensure_runtime_python_314()
        args.meeting_id = merge_meeting_ids(
            args.meeting_id,
            load_meeting_ids_from_file(Path(args.meeting_id_file)) if args.meeting_id_file else [],
            load_meeting_ids_from_file(Path(args.failed_from), failed_only=True)
            if args.failed_from
            else [],
        )
        apply_stage_alias(args)
        validate_parallel_meeting_args(args)
        if not args.execute:
            results = run_pipeline(args)
            handle_results(args, results)
            return 0
        elif args.allow_parallel_meeting:
            results = run_pipeline(args)
            handle_results(args, results)
            failed_count, total_count = failed_operation_counts(results)
            if failed_count == total_count and failed_count > 0:
                failure_summary = summarize_results_for_log(results)
                raise RuntimeError("pipeline returned failed operation")
            write_completion_marker_if_succeeded(args, failed_count, total_count)
            return PARTIAL_EXIT_CODE if failed_count else 0
        else:
            with runtime.lock() as acquired:
                if not acquired:
                    print("SKIPPED reason_code=lock_busy", file=sys.stderr)
                    return SKIPPED_EXIT_CODE
                requested_stages, skip_reason_by_stage = requested_stage_status_scope(args)
                stage_tracker = local_stage_status_tracker(
                    process_marker=Path(__file__).name,
                    requested_stages=requested_stages,
                    skip_reason_by_stage=skip_reason_by_stage,
                )
                if not stage_tracker.initialize_run():
                    raise RuntimeError("stage status writer failed")
                results = run_pipeline(args, stage_tracker=stage_tracker)
                handle_results(args, results)
                stage_status_finalized = True
                if not stage_tracker.finalize():
                    raise RuntimeError("stage status writer failed")
                failed_count, total_count = failed_operation_counts(results)
                if failed_count == total_count and failed_count > 0:
                    failure_summary = summarize_results_for_log(results)
                    raise RuntimeError("pipeline returned failed operation")
                if failed_count > 0:
                    failure_summary = summarize_results_for_log(results)
                    failed_by_stage = json.dumps(
                        failure_summary["failed_by_stage"],
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    print(
                        f"PARTIAL reason_code=child_partial failed={failure_summary['failed']}"
                        f" total={failure_summary['total']} failed_by_stage={failed_by_stage}",
                        file=sys.stderr,
                    )
                    return PARTIAL_EXIT_CODE
                write_completion_marker_if_succeeded(args, failed_count, total_count)
    except Exception as exc:  # noqa: BLE001 - local runner must surface one-line failures.
        stage_failure_reason_code = (
            stage_tracker.first_failure_reason_code() if stage_tracker is not None else None
        )
        if stage_tracker is not None and not stage_status_finalized:
            stage_status_finalized = True
            if not stage_tracker.finalize():
                final_reason_code = "status_writer_failed"
        if str(exc) == "stage status writer failed":
            final_reason_code = "status_writer_failed"
        elif stage_failure_reason_code is not None:
            final_reason_code = stage_failure_reason_code
        else:
            final_reason_code = "pipeline_failed"
        failure_context = ""
        if failure_summary is not None:
            failed_by_stage = json.dumps(
                failure_summary["failed_by_stage"], separators=(",", ":"), sort_keys=True
            )
            failure_context = (
                f" failed={failure_summary['failed']} total={failure_summary['total']}"
                f" failed_by_stage={failed_by_stage}"
            )
        print(f"FAILED reason_code={final_reason_code}{failure_context}", file=sys.stderr)
        return 1
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Circleback -> local meeting SSoT pipeline.")
    add_execution_gate_arguments(parser)
    parser.add_argument("--meeting-id", action="append", default=[])
    parser.add_argument("--meeting-id-file")
    parser.add_argument("--failed-from")
    parser.add_argument("--search")
    parser.add_argument("--all", dest="all_meetings", action="store_true")
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--last-days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--pages", type=int)
    parser.add_argument("--circleback-bin", default=DEFAULT_CIRCLEBACK_BIN)
    parser.add_argument("--codex-bin", default=DEFAULT_CODEX_BIN)
    parser.add_argument("--claude-bin", default=DEFAULT_CLAUDE_BIN)
    parser.add_argument("--distiller-timeout", type=int, default=3600)
    parser.add_argument("--judge-timeout", type=int, default=DEFAULT_JUDGE_TIMEOUT_SECONDS)
    parser.add_argument("--restorer-timeout", type=int, default=3600)
    parser.add_argument("--state-path", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--artifact-root")
    parser.add_argument("--root-path", default=str(BRAIN_ROOT))
    parser.add_argument("--cutover-started-at")
    parser.add_argument("--allow-parallel-meeting", action="store_true")
    parser.add_argument("--stage", choices=sorted(BACKFILL_STAGE_VALUES))
    parser.add_argument(
        "--stop-after", choices=sorted(STOP_AFTER_VALUES), default=STOP_AFTER_MINUTES
    )
    parser.add_argument("--minutes-only", action="store_true")
    parser.add_argument("--completion-marker")
    parser.add_argument("--print-results", action="store_true")
    gate = parse_execution_gate(parser, argv)
    args = parser.parse_args(argv)
    apply_execution_gate(args, gate)
    if args.completion_marker and gate.gate_class is not GateClass.APPLY:
        parser.error("--completion-marker requires --apply")
    args.force = False
    if gate.gate_class is GateClass.CONFIRM:
        if gate.confirm_exact != FORCE_CONFIRM_EXACT:
            parser.error(f"--confirm exact must be {FORCE_CONFIRM_EXACT}")
        args.force = True
    return args


def validate_parallel_meeting_args(args: argparse.Namespace) -> None:
    validate_stop_after_args(args)
    if not args.allow_parallel_meeting:
        return
    if len(args.meeting_id) != 1:
        raise ValueError("--allow-parallel-meeting requires exactly one --meeting-id")


def validate_stop_after_args(args: argparse.Namespace) -> None:
    stage = getattr(args, "stage", None)
    if getattr(args, "all_meetings", False) and getattr(args, "meeting_id", []):
        raise ValueError("--all does not support explicit meeting IDs")
    if getattr(args, "all_meetings", False) and stage and stage != BACKFILL_STAGE_PREPROCESS:
        raise ValueError("--all requires --stage preprocess")
    if (
        getattr(args, "all_meetings", False)
        and getattr(args, "stop_after", STOP_AFTER_MINUTES) != STOP_AFTER_CIRCLEBACK
    ):
        raise ValueError("--all requires --stop-after circleback")
    if (
        getattr(args, "minutes_only", False)
        and getattr(args, "stop_after", STOP_AFTER_MINUTES) != STOP_AFTER_MINUTES
    ):
        raise ValueError("--minutes-only requires --stop-after minutes")


def apply_stage_alias(args: argparse.Namespace) -> None:
    stage = getattr(args, "stage", None)
    if stage == BACKFILL_STAGE_PREPROCESS:
        args.stop_after = STOP_AFTER_CIRCLEBACK
        args.minutes_only = False
    elif stage == BACKFILL_STAGE_LLM1:
        args.stop_after = STOP_AFTER_PLANNING
        args.minutes_only = False
    elif stage == BACKFILL_STAGE_LLM2:
        args.stop_after = STOP_AFTER_RESTORATION
        args.minutes_only = False
    elif stage == BACKFILL_STAGE_LLM3:
        args.stop_after = STOP_AFTER_MINUTES
        args.minutes_only = True


def requested_stage_status_scope(
    args: argparse.Namespace,
) -> tuple[tuple[str, ...], dict[str, str]]:
    stage = getattr(args, "stage", None)
    stop_after = getattr(args, "stop_after", STOP_AFTER_MINUTES)
    if getattr(args, "minutes_only", False) or stage == BACKFILL_STAGE_LLM3:
        return (STAGE_WRITER,), {
            STAGE_GET: "backfill_not_requested",
            STAGE_PLANNER: "backfill_not_requested",
            STAGE_RESTORER: "backfill_not_requested",
        }
    if stage == BACKFILL_STAGE_LLM1:
        return (STAGE_PLANNER,), {
            STAGE_GET: "backfill_not_requested",
            STAGE_RESTORER: "backfill_not_requested",
            STAGE_WRITER: "backfill_not_requested",
        }
    if stage == BACKFILL_STAGE_LLM2:
        return (STAGE_RESTORER,), {
            STAGE_GET: "backfill_not_requested",
            STAGE_PLANNER: "backfill_not_requested",
            STAGE_WRITER: "backfill_not_requested",
        }

    if stop_after == STOP_AFTER_CIRCLEBACK:
        return (STAGE_GET,), {
            STAGE_PLANNER: "stop_after",
            STAGE_RESTORER: "stop_after",
            STAGE_WRITER: "stop_after",
        }
    if stop_after == STOP_AFTER_PLANNING:
        return (STAGE_GET, STAGE_PLANNER), {
            STAGE_RESTORER: "stop_after",
            STAGE_WRITER: "stop_after",
        }
    if stop_after == STOP_AFTER_RESTORATION:
        return (STAGE_GET, STAGE_PLANNER, STAGE_RESTORER), {
            STAGE_WRITER: "stop_after",
        }
    return STAGE_ORDER, {}


def load_meeting_ids_from_file(path: Path, *, failed_only: bool = False) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        meeting_id = ""
        if line.startswith("{"):
            payload = json.loads(line)
            if isinstance(payload, dict):
                if failed_only and payload.get("operation") != "failed":
                    continue
                meeting_id = str(payload.get("meeting_id") or "").strip()
        elif failed_only:
            continue
        else:
            meeting_id = line
        if meeting_id and meeting_id not in seen:
            seen.add(meeting_id)
            ids.append(meeting_id)
    return ids


def merge_meeting_ids(*groups: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for meeting_id in group:
            key = circleback_source_key(meeting_id) if meeting_id else ""
            if key and key not in seen:
                seen.add(key)
                result.append(meeting_id)
    return result


def handle_results(args: argparse.Namespace, results: list[dict[str, object]]) -> None:
    if getattr(args, "print_results", False):
        print(json.dumps(summarize_results_for_log(results), indent=2))


def failed_operation_counts(results: list[dict[str, object]]) -> tuple[int, int]:
    failed_count = sum(1 for result in results if is_failed_operation(result))
    return failed_count, len(results)


def is_failed_operation(result: object) -> bool:
    return isinstance(result, dict) and result.get("operation") == "failed"


def summarize_results_for_log(results: list[dict[str, object]]) -> dict[str, object]:
    failed_by_stage = dict.fromkeys((*STAGE_ORDER, "unknown"), 0)
    failed_count = 0
    for result in results:
        if not is_failed_operation(result):
            continue
        failed_count += 1
        raw_stage = result.get("failed_stage")
        stage = raw_stage if isinstance(raw_stage, str) and raw_stage in STAGE_ORDER else "unknown"
        failed_by_stage[stage] += 1
    return {
        "total": len(results),
        "failed": failed_count,
        "non_failed": len(results) - failed_count,
        "failed_by_stage": {stage: count for stage, count in failed_by_stage.items() if count > 0},
    }


def write_completion_marker_if_succeeded(
    args: argparse.Namespace, failed_count: int, total_count: int
) -> None:
    marker = getattr(args, "completion_marker", None)
    if marker and failed_count == 0:
        write_completion_marker(Path(marker), result_count=total_count)


def write_completion_marker(path: Path, *, result_count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(
        json.dumps({"status": "completed", "result_count": result_count}) + "\n",
        encoding="utf-8",
    )
    temporary_path.chmod(0o600)
    temporary_path.replace(path)


def ensure_runtime_dirs() -> None:
    ensure_private_dir(APP_ROOT / "logs")
    ensure_private_dir(DEFAULT_STATE_PATH.parent)


if __name__ == "__main__":
    raise SystemExit(main())
