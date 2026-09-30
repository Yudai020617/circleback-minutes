from __future__ import annotations

import fcntl
from pathlib import Path

from .paths import APP_ROOT

DELETION_CANDIDATES_FILENAME = "circleback-deletion-candidates.md"


def append_deletion_candidate(
    *,
    meeting_id: str,
    title: str,
    date: str,
    utterance_count: int,
    duration_seconds: float | None,
    reason: str,
    path: Path | None = None,
) -> Path:
    candidate_path = path or APP_ROOT / "knowledge" / DELETION_CANDIDATES_FILENAME
    duration = "-" if duration_seconds is None else f"{duration_seconds:g}秒"
    row = f"| {meeting_id} | {date} | {title} | {utterance_count} | {duration} | {reason} |\n"

    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    with candidate_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        content = handle.read()
        if f"| {meeting_id} |" in content:
            return candidate_path
        handle.seek(0, 2)
        if not content.strip():
            handle.write(
                "# Circleback削除候補\n\n"
                "CirclebackのUIで削除候補を確認する台帳です。削除操作自体は行いません。\n\n"
                "| id | date | title | 発話数 | 長さ | 理由 |\n"
                "| --- | --- | --- | ---: | ---: | --- |\n"
            )
        handle.write(row)
    candidate_path.chmod(0o600)
    return candidate_path
