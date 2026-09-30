from __future__ import annotations

from pathlib import Path
from string import Template
from typing import Any

from .paths import PROMPTS_DIR

PROMPT_FILES = {
    "meeting_judge": "meeting-judge.md",
    "transcript_restorer_chunk": "transcript-restorer-chunk.md",
}


def render_prompt_template(name: str, **values: Any) -> str:
    path = prompt_template_path(name)
    template = Template(path.read_text(encoding="utf-8"))
    payload = {key: str(value) for key, value in values.items()}
    return template.substitute(payload).strip() + "\n"


def prompt_template_path(name: str) -> Path:
    filename = PROMPT_FILES.get(name, name)
    path = PROMPTS_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(f"prompt template not found: {path}")
    return path
