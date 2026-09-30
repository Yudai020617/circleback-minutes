from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .log_safety import redact_sensitive_text
from .paths import APP_ROOT

DEFAULT_CODEX_BIN = os.environ.get("MINUTES_CODEX_BIN", "codex")
DEFAULT_CODEX_MODEL = os.environ.get("MINUTES_CODEX_MODEL", "gpt-6-astra")
DEFAULT_CODEX_EFFORT = os.environ.get("MINUTES_CODEX_EFFORT", "high")
DEFAULT_FAILURE_ARTIFACT_DIR = APP_ROOT / "var" / "run" / "codex-failures"
DEFAULT_TEMP_DIR = APP_ROOT / "tmp"
OUTPUT_TAIL_CHARS = 2000


def run_codex_text(
    prompt: str,
    *,
    codex_bin: str = DEFAULT_CODEX_BIN,
    model: str = DEFAULT_CODEX_MODEL,
    effort: str = DEFAULT_CODEX_EFFORT,
    timeout_seconds: int = 600,
    output_schema: dict[str, Any] | None = None,
    failure_label: str = "Codex execution",
    sandbox: str = "read-only",
    cwd: Path | str = APP_ROOT,
    failure_artifact_dir: Path | str | None = DEFAULT_FAILURE_ARTIFACT_DIR,
) -> str:
    with tempfile.TemporaryDirectory(
        prefix="circleback-codex-", dir=private_temp_parent()
    ) as tmp_dir:
        tmp_path = Path(tmp_dir)
        output_path = tmp_path / "output.txt"
        cwd_path = Path(cwd)
        command = [
            codex_bin,
            "exec",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            sandbox,
            "--cd",
            str(cwd_path),
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{effort}"',
        ]
        if output_schema is not None:
            schema_path = tmp_path / "schema.json"
            schema_path.write_text(json.dumps(output_schema, ensure_ascii=False), encoding="utf-8")
            command.extend(["--output-schema", str(schema_path)])
        command.extend(["--output-last-message", str(output_path), "-"])

        try:
            result = run_text_subprocess(
                command,
                input_text=prompt,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            artifact_path = write_failure_artifact(
                failure_artifact_dir,
                failure_label,
                command=command,
                prompt=prompt,
                stdout=exc.stdout,
                stderr=exc.stderr,
                error_kind="timeout",
                timeout_seconds=timeout_seconds,
            )
            artifact_text = f" artifacts={artifact_path}" if artifact_path else ""
            raise RuntimeError(
                f"{failure_label} timed out after {timeout_seconds}s{artifact_text}"
            ) from None
        if result.returncode != 0:
            artifact_path = write_failure_artifact(
                failure_artifact_dir,
                failure_label,
                command=command,
                prompt=prompt,
                stdout=result.stdout,
                stderr=result.stderr,
                error_kind="exit",
                exit_code=result.returncode,
            )
            artifact_text = f" artifacts={artifact_path}" if artifact_path else ""
            raise RuntimeError(
                f"{failure_label} failed with exit code {result.returncode}{artifact_text}"
            )
        if output_path.exists():
            output = output_path.read_text(encoding="utf-8").strip()
            if output:
                return output
        return result.stdout


def private_temp_parent() -> Path:
    DEFAULT_TEMP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        DEFAULT_TEMP_DIR.chmod(0o700)
    return DEFAULT_TEMP_DIR


def run_text_subprocess(
    command: list[str],
    *,
    input_text: str,
    timeout: float,
    cwd: Path | str | None = None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    group_tracker = ProcessGroupTracker(process.pid)
    group_tracker.start()
    try:
        stdout, stderr = process.communicate(input_text, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout, stderr = terminate_text_process_group(
            process, extra_snapshot=group_tracker.snapshot()
        )
        raise subprocess.TimeoutExpired(
            cmd=command,
            timeout=timeout,
            output=stdout,
            stderr=stderr,
        ) from exc
    finally:
        group_tracker.stop()
    return subprocess.CompletedProcess(
        command,
        process.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
    )


def terminate_text_process_group(
    process: subprocess.Popen[str],
    *,
    grace_seconds: float = 5.0,
    extra_snapshot: ProcessTreeSnapshot | None = None,
) -> tuple[str, str]:
    snapshot = merge_process_tree_snapshots(
        extra_snapshot, process_tree_snapshot_for_root(process.pid)
    )
    if not snapshot.pgids and not snapshot.pids:
        return communicate_text_with_timeout(process, grace_seconds)

    signal_process_tree(snapshot, signal.SIGTERM)

    try:
        stdout, stderr = process.communicate(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        signal_process_tree(
            merge_process_tree_snapshots(snapshot, process_tree_snapshot_for_root(process.pid)),
            signal.SIGKILL,
        )
        stdout, stderr = communicate_text_with_timeout(process, grace_seconds)
    return stdout or "", stderr or ""


@dataclass(frozen=True)
class ProcessTreeSnapshot:
    pids: frozenset[int]
    pgids: frozenset[int]


class ProcessGroupTracker:
    def __init__(self, root_pid: int, *, interval_seconds: float = 0.05) -> None:
        self.root_pid = root_pid
        self.interval_seconds = interval_seconds
        self._pids: set[int] = set()
        self._pgids: set[int] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._record()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def snapshot(self) -> ProcessTreeSnapshot:
        self._record()
        with self._lock:
            return ProcessTreeSnapshot(frozenset(self._pids), frozenset(self._pgids))

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._record()

    def _record(self) -> None:
        snapshot = process_tree_snapshot_for_root(self.root_pid)
        if not snapshot.pids and not snapshot.pgids:
            return
        with self._lock:
            self._pids.update(snapshot.pids)
            self._pgids.update(snapshot.pgids)


def merge_process_tree_snapshots(
    *snapshots: ProcessTreeSnapshot | None,
) -> ProcessTreeSnapshot:
    pids: set[int] = set()
    pgids: set[int] = set()
    for snapshot in snapshots:
        if snapshot is None:
            continue
        pids.update(snapshot.pids)
        pgids.update(snapshot.pgids)
    return ProcessTreeSnapshot(frozenset(pids), frozenset(pgids))


def process_tree_snapshot_for_root(root_pid: int) -> ProcessTreeSnapshot:
    pids = {root_pid, *descendant_pids(root_pid)}
    pgids: set[int] = set()
    existing_pids: set[int] = set()
    for pid in pids:
        try:
            pgids.add(os.getpgid(pid))
            existing_pids.add(pid)
        except ProcessLookupError:
            continue
    return ProcessTreeSnapshot(frozenset(existing_pids), frozenset(pgids))


def descendant_pids(root_pid: int) -> set[int]:
    descendants: set[int] = set()
    stack = [root_pid]
    while stack:
        parent_pid = stack.pop()
        result = subprocess.run(
            ["/usr/bin/pgrep", "-P", str(parent_pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode not in {0, 1}:
            continue
        for line in result.stdout.splitlines():
            try:
                child_pid = int(line.strip())
            except ValueError:
                continue
            if child_pid in descendants:
                continue
            descendants.add(child_pid)
            stack.append(child_pid)
    return descendants


def signal_process_tree(snapshot: ProcessTreeSnapshot, sig: signal.Signals) -> None:
    signal_process_groups(set(snapshot.pgids), sig)
    signal_processes(set(snapshot.pids), sig)


def signal_process_groups(pgids: set[int], sig: signal.Signals) -> None:
    for pgid in sorted(pgids, reverse=True):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, sig)


def signal_processes(pids: set[int], sig: signal.Signals) -> None:
    for pid in sorted(pids, reverse=True):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, sig)


def communicate_text_with_timeout(
    process: subprocess.Popen[str], timeout_seconds: float
) -> tuple[str, str]:
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return stdout or "", stderr or ""
    except subprocess.TimeoutExpired as exc:
        close_process_pipes(process)
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=timeout_seconds)
        return subprocess_output_text(exc.output), subprocess_output_text(exc.stderr)


def close_process_pipes(process: subprocess.Popen[str]) -> None:
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None:
            with contextlib.suppress(OSError):
                pipe.close()


def subprocess_output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def output_byte_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bytes):
        return len(value)
    return len(str(value).encode("utf-8", errors="replace"))


def output_tail_text(value: Any) -> str:
    if value is None:
        return ""
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
    return redact_sensitive_text(text[-OUTPUT_TAIL_CHARS:])


def write_failure_artifact(
    artifact_dir: Path | str | None,
    failure_label: str,
    *,
    command: list[str],
    prompt: str,
    stdout: Any,
    stderr: Any,
    error_kind: str = "process",
    exit_code: int | None = None,
    timeout_seconds: int | None = None,
) -> Path | None:
    if artifact_dir is None:
        return None
    try:
        root = Path(artifact_dir)
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        path = make_unique_artifact_path(root / f"{timestamp}-{artifact_slug(failure_label)}")
        write_private_text(
            path / "command.json",
            json.dumps({"command": command}, ensure_ascii=False, indent=2) + "\n",
        )
        # Prompts and full stdout can contain meeting transcripts, so store only
        # byte counts for the prompt and the full streams. Redacted tails are kept
        # for root-cause analysis: stderr for CLI errors, stdout because Claude/Codex
        # CLIs report failures such as quota exhaustion on stdout.
        write_private_text(
            path / "failure.json",
            json.dumps(
                {
                    "label": failure_label,
                    "error_kind": error_kind,
                    "exit_code": exit_code,
                    "timeout_seconds": timeout_seconds,
                    "prompt_bytes": output_byte_count(prompt),
                    "stdout_bytes": output_byte_count(stdout),
                    "stderr_bytes": output_byte_count(stderr),
                    "stdout_tail": output_tail_text(stdout),
                    "stderr_tail": output_tail_text(stderr),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        return path
    except OSError:
        return None


def make_unique_artifact_path(base_path: Path) -> Path:
    for index in range(1, 100):
        path = base_path if index == 1 else base_path.with_name(f"{base_path.name}-{index}")
        try:
            path.mkdir(mode=0o700)
            return path
        except FileExistsError:
            continue
    raise FileExistsError(f"too many failure artifacts with base path: {base_path}")


def write_private_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def artifact_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower()).strip("-._")
    return slug or "codex"
