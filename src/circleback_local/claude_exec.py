from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn

from .codex_exec import (
    DEFAULT_FAILURE_ARTIFACT_DIR,
    private_temp_parent,
    run_text_subprocess,
    write_failure_artifact,
    write_private_text,
)

DEFAULT_CLAUDE_BIN = os.environ.get("MINUTES_CLAUDE_BIN", "claude")
DEFAULT_CLAUDE_MODEL = os.environ.get("MINUTES_CLAUDE_MODEL", "claude-fable-5")
DEFAULT_CLAUDE_EFFORT = os.environ.get("MINUTES_CLAUDE_EFFORT", "xhigh")
CLAUDE_RATE_LIMIT_RETRY_SECONDS = 60


def run_claude_text(
    prompt: str,
    *,
    claude_bin: str = DEFAULT_CLAUDE_BIN,
    model: str = DEFAULT_CLAUDE_MODEL,
    effort: str = DEFAULT_CLAUDE_EFFORT,
    timeout_seconds: int = 600,
    allowed_tools: Sequence[str] = ("Read", "Glob", "Grep"),
    failure_label: str = "Claude execution",
    cwd: Path | str | None = None,
    failure_artifact_dir: Path | str | None = DEFAULT_FAILURE_ARTIFACT_DIR,
) -> str:
    command = [
        claude_bin,
        "-p",
        "--model",
        model,
        "--effort",
        effort,
        "--output-format",
        "json",
        "--allowedTools",
        *allowed_tools,
    ]
    for attempt in range(2):
        try:
            completed = run_text_subprocess(
                command,
                input_text=prompt,
                timeout=timeout_seconds,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired as error:
            raise_claude_failure(
                failure_artifact_dir,
                failure_label,
                command=command,
                prompt=prompt,
                stdout=error.stdout,
                stderr=error.stderr,
                error_kind="timeout",
                timeout_seconds=timeout_seconds,
                cause=error,
            )
        except OSError as error:
            raise_claude_failure(
                failure_artifact_dir,
                failure_label,
                command=command,
                prompt=prompt,
                stdout="",
                stderr=str(error),
                error_kind="execution",
                cause=error,
            )

        if completed.returncode == 0:
            break
        rate_limited = is_rate_limit_output(completed.stdout)
        if rate_limited and attempt == 0:
            # 2026-08-26: consecutive meeting-judge calls both hit request-rate 429.
            time.sleep(CLAUDE_RATE_LIMIT_RETRY_SECONDS)
            continue
        raise_claude_failure(
            failure_artifact_dir,
            failure_label,
            command=command,
            prompt=prompt,
            stdout=completed.stdout,
            stderr=completed.stderr,
            error_kind="rate_limit" if rate_limited else "process",
            exit_code=completed.returncode,
        )

    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise_claude_failure(
            failure_artifact_dir,
            failure_label,
            command=command,
            prompt=prompt,
            stdout=completed.stdout,
            stderr=completed.stderr,
            error_kind="invalid_output",
            cause=error,
        )
    if not isinstance(envelope, dict):
        raise_claude_failure(
            failure_artifact_dir,
            failure_label,
            command=command,
            prompt=prompt,
            stdout=completed.stdout,
            stderr=completed.stderr,
            error_kind="invalid_output",
        )

    result = envelope.get("result")
    if (
        envelope.get("is_error") is not False
        or envelope.get("subtype") != "success"
        or not isinstance(result, str)
    ):
        raise_claude_failure(
            failure_artifact_dir,
            failure_label,
            command=command,
            prompt=prompt,
            stdout=completed.stdout,
            stderr=completed.stderr,
            error_kind="invalid_output",
        )
    return result


def is_rate_limit_output(stdout: Any) -> bool:
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError, TypeError:
        return False
    if not isinstance(envelope, dict) or envelope.get("is_error") is not True:
        return False
    error_text = json.dumps(envelope, ensure_ascii=False).casefold()
    return "rate_limit_error" in error_text


def raise_claude_failure(
    failure_artifact_dir: Path | str | None,
    failure_label: str,
    *,
    command: list[str],
    prompt: str,
    stdout: Any,
    stderr: Any,
    error_kind: str = "process",
    exit_code: int | None = None,
    timeout_seconds: int | None = None,
    cause: BaseException | None = None,
) -> NoReturn:
    write_failure_artifact(
        failure_artifact_dir,
        failure_label,
        command=command,
        prompt=prompt,
        stdout=stdout,
        stderr=stderr,
        error_kind=error_kind,
        exit_code=exit_code,
        timeout_seconds=timeout_seconds,
    )
    error = RuntimeError(f"{failure_label} failed")
    if cause is None:
        raise error
    raise error from cause


__all__ = [
    "DEFAULT_CLAUDE_BIN",
    "DEFAULT_CLAUDE_EFFORT",
    "DEFAULT_CLAUDE_MODEL",
    "private_temp_parent",
    "run_claude_text",
    "write_private_text",
]
