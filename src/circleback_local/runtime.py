"""Local file locking and status output; no scheduler or external service required."""
from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .paths import APP_ROOT


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def set_owner_only_umask() -> None:
    os.umask(0o077)


def ensure_runtime_python_314() -> None:
    if sys.version_info < (3, 14):
        raise RuntimeError("Python 3.14 or later is required")


class LocalRuntime:
    def __init__(self, job_id: str, process_marker: str) -> None:
        self.job_id = job_id
        self.process_marker = process_marker
        self.directory = APP_ROOT / "var" / "run"

    @contextmanager
    def lock(self):
        ensure_private_dir(self.directory)
        with (self.directory / f"{self.job_id}.lock").open("a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def write_status(self, status: str, run_state: str, reason_code: str, message: str) -> bool:
        ensure_private_dir(self.directory)
        payload = {
            "job_id": self.job_id,
            "process_marker": self.process_marker,
            "status": status,
            "run_state": run_state,
            "reason_code": reason_code,
            "message": message,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory, delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            temporary.replace(self.directory / f"{self.job_id}.json")
            return True
        except OSError:
            return False
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def local_runtime(job_id: str, process_marker: str) -> LocalRuntime:
    return LocalRuntime(job_id, process_marker)
