from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from .frontmatter import (
    meeting_circleback_id,
    meeting_share,
    render_frontmatter,
    split_frontmatter,
)
from .models import Minutes, Transcript
from .paths import DEFAULT_STATE_PATH, ROOT, MeetingPaths, meeting_paths


class StateStore:
    def __init__(self, path: Path = DEFAULT_STATE_PATH) -> None:
        self.path = path
        self.data = self._read()
        self._loaded_data = deepcopy(self.data)
        self._dirty_meeting_fields: dict[str, set[str]] = {}

    def ensure_cutover_started_at(self, value: str | None = None) -> str:
        current = self.data.get("cutover_started_at")
        if isinstance(current, str) and current:
            return current
        marker = value or dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        self.data["cutover_started_at"] = marker
        self.write()
        return marker

    def meeting(self, meeting_id: str) -> dict[str, Any] | None:
        value = self.data.setdefault("meetings", {}).get(meeting_id)
        return value if isinstance(value, dict) else None

    def upsert_meeting(self, meeting_id: str, values: dict[str, Any]) -> dict[str, Any]:
        meetings = self.data.setdefault("meetings", {})
        existing = meetings.get(meeting_id) if isinstance(meetings.get(meeting_id), dict) else {}
        updated = {**existing, **values}
        meetings[meeting_id] = updated
        self._dirty_meeting_fields.setdefault(meeting_id, set()).update(values)
        self.write()
        return updated

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        with lock_path.open("a", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            self.data = self._merge_with_disk()
            payload = json.dumps(self.data, ensure_ascii=False, indent=2) + "\n"
            tmp_path = self.path.with_name(f".{self.path.name}.tmp")
            with tmp_path.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
            self.path.chmod(0o600)
            lock_path.chmod(0o600)
            self._loaded_data = deepcopy(self.data)
            self._dirty_meeting_fields.clear()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"meetings": {}}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {"meetings": {}}

    def _merge_with_disk(self) -> dict[str, Any]:
        latest = self._read()
        merged = {**latest, **self.data}
        latest_meetings_value = latest.get("meetings")
        current_meetings_value = self.data.get("meetings")
        loaded_meetings_value = self._loaded_data.get("meetings")
        latest_meetings = (
            dict(latest_meetings_value) if isinstance(latest_meetings_value, dict) else {}
        )
        current_meetings = (
            dict(current_meetings_value) if isinstance(current_meetings_value, dict) else {}
        )
        loaded_meetings = (
            dict(loaded_meetings_value) if isinstance(loaded_meetings_value, dict) else {}
        )
        meeting_ids = set(latest_meetings) | set(current_meetings)
        merged_meetings: dict[str, Any] = {}
        for meeting_id in meeting_ids:
            latest_record = latest_meetings.get(meeting_id)
            current_record = current_meetings.get(meeting_id)
            if isinstance(latest_record, dict) and isinstance(current_record, dict):
                merged_record = dict(latest_record)
                loaded_record = loaded_meetings.get(meeting_id)
                dirty_fields = self._dirty_meeting_fields.get(meeting_id) or infer_dirty_fields(
                    loaded_record,
                    current_record,
                )
                for key in dirty_fields:
                    if key in current_record:
                        merged_record[key] = current_record[key]
                    else:
                        merged_record.pop(key, None)
                merged_meetings[meeting_id] = merged_record
            else:
                merged_meetings[meeting_id] = (
                    current_record if current_record is not None else latest_record
                )
        merged["meetings"] = merged_meetings
        return merged


def infer_dirty_fields(loaded_record: Any, current_record: dict[str, Any]) -> set[str]:
    if not isinstance(loaded_record, dict):
        return set(current_record)
    dirty = {key for key, value in current_record.items() if loaded_record.get(key) != value}
    dirty.update(key for key in loaded_record if key not in current_record)
    return dirty


def save_transcript(
    transcript: Transcript,
    root: Path = ROOT,
    state: StateStore | None = None,
    *,
    filename_stem: str,
) -> MeetingPaths:
    paths = meeting_paths(
        root,
        filename_stem,
        transcript.meeting_id,
    )
    existing_dir = existing_meeting_directory(paths)
    ensure_existing_directory_source_key(existing_dir, transcript.source_key)
    ensure_paths_available(paths, transcript.source_key, state=state)
    relocate_meeting_directory(existing_dir, paths.meeting_dir)
    body = render_transcript_markdown(transcript, paths=paths, root=root)
    write_private_text(paths.transcript_path, body)
    if state:
        values: dict[str, Any] = {
            "source_key": transcript.source_key,
            "slug": paths.slug,
            "transcript_path": str(paths.transcript_path),
            "transcript_hash": content_hash(body),
        }
        if paths.minutes_path.exists():
            minutes_body = paths.minutes_path.read_text(encoding="utf-8")
            values.update(
                {
                    "minutes_path": str(paths.minutes_path),
                    "minutes_hash": content_hash(minutes_body),
                }
            )
        state.upsert_meeting(
            transcript.meeting_id,
            values,
        )
    return paths


def save_minutes(
    minutes: Minutes,
    paths: MeetingPaths,
    state: StateStore | None = None,
    *,
    share: str,
) -> Path:
    root = paths.meeting_dir.parents[1]
    body = render_minutes_markdown(minutes, paths=paths, root=root, share=share)
    write_private_text(paths.minutes_path, body)
    if state:
        state.upsert_meeting(
            minutes.meeting_id,
            {
                "source_key": minutes.source_key,
                "slug": paths.slug,
                "minutes_path": str(paths.minutes_path),
                "minutes_hash": content_hash(body),
                "minutes_needs_regeneration": False,
            },
        )
    return paths.minutes_path


def save_meeting_pair(
    transcript: Transcript,
    minutes: Minutes,
    root: Path = ROOT,
    state: StateStore | None = None,
    *,
    filename_stem: str,
    share: str,
) -> MeetingPaths:
    paths = meeting_paths(root, filename_stem, transcript.meeting_id)
    existing_dir = existing_meeting_directory(paths)
    ensure_existing_directory_source_key(existing_dir, transcript.source_key)
    ensure_paths_available(paths, transcript.source_key, state=state)
    existing_minutes_path = existing_dir / "minutes.md" if existing_dir is not None else None
    if existing_minutes_path is not None and existing_minutes_path.is_file():
        existing_frontmatter, _body = split_frontmatter(
            existing_minutes_path.read_text(encoding="utf-8")
        )
        share = meeting_share(existing_frontmatter)
    transcript_body = render_transcript_markdown(transcript, paths=paths, root=root)
    minutes_body = render_minutes_markdown(minutes, paths=paths, root=root, share=share)
    meetings_dir = paths.meeting_dir.parent
    meetings_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=".circleback-pair-", dir=meetings_dir))
    staging_dir.chmod(0o700)
    previous_dir = staging_dir.with_name(f"{staging_dir.name}.previous")
    committed = False

    try:
        write_private_text(staging_dir / "transcript.md", transcript_body)
        write_private_text(staging_dir / "minutes.md", minutes_body)
        if existing_dir is not None:
            existing_dir.rename(previous_dir)
        try:
            staging_dir.rename(paths.meeting_dir)
        except Exception:
            if existing_dir is not None:
                previous_dir.rename(existing_dir)
            raise

        if state is not None:
            try:
                state.upsert_meeting(
                    transcript.meeting_id,
                    {
                        "source_key": transcript.source_key,
                        "slug": paths.slug,
                        "transcript_path": str(paths.transcript_path),
                        "transcript_hash": content_hash(transcript_body),
                        "minutes_path": str(paths.minutes_path),
                        "minutes_hash": content_hash(minutes_body),
                        "minutes_needs_regeneration": False,
                    },
                )
            except Exception:
                paths.meeting_dir.rename(staging_dir)
                if existing_dir is not None:
                    previous_dir.rename(existing_dir)
                raise
        committed = True
        if previous_dir.exists():
            shutil.rmtree(previous_dir)
        return paths
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        if previous_dir.exists() and (
            committed or (existing_dir is not None and existing_dir.exists())
        ):
            shutil.rmtree(previous_dir)


def existing_meeting_directory(paths: MeetingPaths) -> Path | None:
    meetings_dir = paths.meeting_dir.parent
    if not meetings_dir.exists():
        return None
    existing_dirs: list[Path] = []
    for path in meetings_dir.iterdir():
        if not path.is_dir() or path.is_symlink():
            continue
        identities: list[str] = []
        for file_name in ("minutes.md", "transcript.md"):
            markdown_path = path / file_name
            if not markdown_path.is_file() or markdown_path.is_symlink():
                continue
            frontmatter, _body = split_frontmatter(markdown_path.read_text(encoding="utf-8"))
            identities.append(meeting_circleback_id(frontmatter))
        if identities and len(set(identities)) != 1:
            raise FileExistsError(f"meeting pair circleback_id mismatch: path={path}")
        if identities and identities[0] == paths.meeting_id:
            existing_dirs.append(path)
    if len(existing_dirs) > 1:
        raise FileExistsError(
            f"multiple meeting directories found for meeting_id={paths.meeting_id}"
        )
    return existing_dirs[0] if existing_dirs else None


def ensure_existing_directory_source_key(existing_dir: Path | None, source_key: str) -> None:
    if existing_dir is None:
        return
    transcript_path = existing_dir / "transcript.md"
    if transcript_path.exists() and source_key_from_markdown(transcript_path) != source_key:
        raise FileExistsError(
            f"refusing to overwrite existing meeting file: path={transcript_path}"
        )


def relocate_meeting_directory(existing_dir: Path | None, destination: Path) -> None:
    if existing_dir is not None and existing_dir != destination:
        if destination.exists():
            raise FileExistsError(f"meeting destination already exists: path={destination}")
        existing_dir.rename(destination)


SOURCE_KEY_LINE_RE = re.compile(r"^- Circlebackキー:\s*(?P<source_key>\S+)\s*$", flags=re.MULTILINE)


def ensure_paths_available(
    paths: MeetingPaths, source_key: str, state: StateStore | None = None
) -> None:
    for path in (paths.transcript_path, paths.minutes_path):
        if not path.exists():
            continue
        existing_source_key = (
            source_key_from_state(state, paths.meeting_id) if state is not None else None
        )
        if existing_source_key is None:
            existing_source_key = source_key_from_markdown(path)
        if existing_source_key != source_key:
            raise FileExistsError(
                f"refusing to overwrite existing meeting file: path={path} existing_source_key={existing_source_key or 'missing'} new_source_key={source_key}"
            )


def source_key_from_state(state: StateStore | None, meeting_id: str) -> str | None:
    if state is None:
        return None
    meetings = state.data.get("meetings")
    if not isinstance(meetings, dict):
        return None
    record = meetings.get(meeting_id)
    return str(record.get("source_key") or "") if isinstance(record, dict) else None


def source_key_from_markdown(path: Path) -> str:
    match = SOURCE_KEY_LINE_RE.search(path.read_text(encoding="utf-8"))
    return match.group("source_key") if match else ""


def render_transcript_markdown(
    transcript: Transcript,
    *,
    paths: MeetingPaths,
    root: Path = ROOT,
) -> str:
    frontmatter = render_frontmatter(
        {
            "title": transcript.title,
            "date": transcript.date,
            "circleback_id": transcript.meeting_id,
            "attendees": [attendee.name for attendee in transcript.attendees],
            "client": transcript.client,
            "type": transcript.meeting_type,
            "tags": frontmatter_tags("transcript", transcript.tags),
        }
    )
    transcript_path = paths.transcript_path
    minutes_path = paths.minutes_path
    lines = [
        "## 関連ファイル",
        "",
        f"- [議事録]({relative_markdown_link(transcript_path, minutes_path)})",
        "",
        "## 基本情報",
        "",
        f"- 日付: {transcript.date}",
    ]
    duration = format_duration(transcript.duration_seconds)
    if duration:
        lines.append(f"- 時間: {duration}")
    lines.extend(
        basic_info_lines(
            transcript.client,
            tuple(attendee.name for attendee in transcript.attendees),
            transcript.meeting_type,
            transcript.source_key,
            transcript_path,
            root,
        )
    )
    lines.extend(["", "## 文字起こし", ""])
    for index, utterance in enumerate(transcript.utterances, start=1):
        lines.append(
            "|".join(
                (
                    f"{index:04d}",
                    transcript_cell(utterance.timestamp),
                    transcript_cell(utterance.speaker),
                    transcript_cell(utterance.text),
                )
            )
        )
    return frontmatter + "\n".join(lines).rstrip() + "\n"


def render_minutes_markdown(
    minutes: Minutes,
    *,
    paths: MeetingPaths,
    root: Path = ROOT,
    share: str,
) -> str:
    frontmatter_values: dict[str, Any] = {
        "title": minutes.title,
        "date": minutes.date,
        "circleback_id": minutes.meeting_id,
        "attendees": minutes.attendees,
        "client": minutes.client,
        "type": minutes.meeting_type,
        "tags": frontmatter_tags("", minutes.tags),
    }
    frontmatter_values["share"] = share
    frontmatter = render_frontmatter(frontmatter_values)
    minutes_path = paths.minutes_path
    transcript_path = paths.transcript_path
    lines = [
        "## 基本情報",
        "",
        f"- 日付: {minutes.date}",
    ]
    lines.extend(
        basic_info_lines(
            minutes.client,
            minutes.attendees,
            minutes.meeting_type,
            minutes.source_key,
            minutes_path,
            root,
            include_source_key=False,
        )
    )
    body = strip_leading_title(minutes.markdown_body)
    if body:
        lines.extend(["", body])
    lines.extend(
        [
            "",
            "## 関連ファイル",
            "",
            f"- [文字起こし]({relative_markdown_link(minutes_path, transcript_path)})",
        ]
    )
    return frontmatter + "\n".join(lines).rstrip() + "\n"


def frontmatter_tags(primary: str, tags: tuple[str, ...]) -> tuple[str, ...]:
    excluded = {"meeting", "transcript", "circleback"}
    ordered = []
    if primary:
        ordered.append(primary)
        excluded.add(primary)
    for tag in tags:
        text = str(tag).strip()
        if text and text not in excluded and text not in ordered:
            ordered.append(text)
    return tuple(ordered)


def basic_info_lines(
    client: str,
    attendees: tuple[str, ...],
    meeting_type: str,
    source_key: str,
    current_path: Path,
    root: Path,
    *,
    include_source_key: bool = True,
) -> list[str]:
    links = people_link_index(root, current_path)
    lines = [f"- 種別: {meeting_type}"]
    if client:
        lines.append(f"- クライアント: {links.get(('organizations', client), client)}")
    if attendees:
        linked_attendees = [links.get(("persons", attendee), attendee) for attendee in attendees]
        lines.append(f"- 参加者: {', '.join(linked_attendees)}")
    if include_source_key:
        lines.append(f"- Circlebackキー: {source_key}")
    return lines


def people_link_index(root: Path, current_path: Path) -> dict[tuple[str, str], str]:
    index: dict[tuple[str, str], str] = {}
    people_dir = root / "40_人物組織"
    for group in ("organizations", "persons"):
        for path in sorted((people_dir / group).glob("*.md")):
            title = markdown_title(path)
            if title:
                index[(group, title)] = f"[{title}]({relative_markdown_link(current_path, path)})"
    return index


def markdown_title(path: Path) -> str:
    frontmatter, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    value = frontmatter.get("title")
    return str(value).strip() if isinstance(value, str) else ""


def relative_markdown_link(from_path: Path, to_path: Path) -> str:
    link = Path(os.path.relpath(to_path, start=from_path.parent)).as_posix()
    return f"<{link}>" if re.search(r"\s", link) else link


def format_duration(duration_seconds: float | None) -> str:
    if duration_seconds is None:
        return ""
    total_seconds = max(0, int(round(duration_seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def transcript_cell(value: str) -> str:
    return str(value).replace("\n", " ").replace("|", "｜").strip()


def strip_leading_title(markdown: str) -> str:
    lines = markdown.strip().splitlines()
    if lines and lines[0].startswith("# ") and not lines[0].startswith("## "):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    return "\n".join(lines).strip()


def write_private_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
