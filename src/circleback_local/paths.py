from __future__ import annotations

import re
import os
from dataclasses import dataclass
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
BRAIN_ROOT = Path(os.environ.get("MINUTES_WORKSPACE", str(APP_ROOT / "workspace"))).expanduser().resolve()
ROOT = BRAIN_ROOT

MEETINGS_DIR = BRAIN_ROOT / "50_議事録"
PEOPLE_ORGS_DIR = BRAIN_ROOT / "40_人物組織"
KNOWLEDGE_DIR = APP_ROOT / "knowledge"
PROMPTS_DIR = KNOWLEDGE_DIR / "prompts"
TOPIC_TAGS_PATH = KNOWLEDGE_DIR / "topic-tags.md"
TRANSCRIPTION_TERMS_DIR = KNOWLEDGE_DIR / "transcription-terms"
REVIEW_INBOX_PATH = KNOWLEDGE_DIR / "transcription-review-inbox.md"
TERMS_CANDIDATES_PATH = KNOWLEDGE_DIR / "transcription-terms-candidates.md"
STATE_DIR = APP_ROOT / "var" / "state"
DEFAULT_STATE_PATH = STATE_DIR / "circleback-local-pipeline.json"
DEFAULT_ARTIFACT_ROOT = APP_ROOT / "var" / "meetings"

DANGEROUS_FILENAME_CHARS_RE = re.compile(r'[\x00-\x1f\x7f/\\:<>"|?*]')
MEETING_ID_RE = re.compile(r"[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class MeetingPaths:
    meeting_id: str
    slug: str
    meeting_dir: Path
    transcript_path: Path
    minutes_path: Path


def sanitize_filename_stem(filename_stem: str) -> str:
    stem = filename_stem.strip()
    if stem.casefold().endswith(".md"):
        stem = stem[:-3]
    stem = DANGEROUS_FILENAME_CHARS_RE.sub("_", stem)
    stem = re.sub(r"_+", "_", stem).strip(" .")
    return stem or "_"


def meeting_paths(
    root: Path,
    filename_stem: str,
    meeting_id: str,
) -> MeetingPaths:
    if not MEETING_ID_RE.fullmatch(meeting_id):
        raise ValueError(f"meeting_id must be ASCII alphanumeric: {meeting_id}")
    slug = sanitize_filename_stem(filename_stem)
    meeting_dir = root / "50_議事録" / slug
    return MeetingPaths(
        meeting_id=meeting_id,
        slug=slug,
        meeting_dir=meeting_dir,
        transcript_path=meeting_dir / "transcript.md",
        minutes_path=meeting_dir / "minutes.md",
    )
