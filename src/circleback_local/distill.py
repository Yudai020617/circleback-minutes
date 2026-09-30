from __future__ import annotations

import dataclasses
import json
import logging
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from .circleback_client import TranscriptDoc
from .codex_exec import (
    DEFAULT_CODEX_BIN,
    DEFAULT_CODEX_EFFORT,
    DEFAULT_CODEX_MODEL,
    private_temp_parent,
    run_codex_text,
)
from .paths import TOPIC_TAGS_PATH

DEFAULT_DISTILL_TIMEOUT_SECONDS = 600
REQUIRED_SECTIONS = (
    "要約",
    "アジェンダ",
    "ToDo",
    "合意事項",
    "決定事項",
    "主な議事・ポイント",
    "要確認事項",
)
LOCAL_STORE_SECTIONS = ("関連ファイル", "基本情報")
MAX_TOPIC_TAGS = 4
LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class DistilledMinutes:
    source_key: str
    title: str
    markdown: str
    distiller: str
    tags: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class MinutesWriterOutput:
    markdown: str
    tags: tuple[str, ...]


class Distiller(Protocol):
    def distill(
        self,
        transcript: TranscriptDoc,
        transcript_markdown: str,
        planning_context: dict[str, Any] | None = None,
    ) -> DistilledMinutes:
        raise NotImplementedError


class CodexDistiller:
    name = "codex"

    def __init__(
        self,
        codex_bin: str = DEFAULT_CODEX_BIN,
        timeout_seconds: int = DEFAULT_DISTILL_TIMEOUT_SECONDS,
        model: str = DEFAULT_CODEX_MODEL,
        effort: str = DEFAULT_CODEX_EFFORT,
    ) -> None:
        self.codex_bin = codex_bin
        self.timeout_seconds = timeout_seconds
        self.model = model
        self.effort = effort

    def distill(
        self,
        transcript: TranscriptDoc,
        transcript_markdown: str,
        planning_context: Mapping[str, Any] | None = None,
    ) -> DistilledMinutes:
        with tempfile.TemporaryDirectory(
            prefix="circleback-distiller-input-", dir=private_temp_parent()
        ) as tmp_dir:
            tmp_path = Path(tmp_dir)
            output = run_codex_text(
                build_minutes_inline_prompt(
                    metadata=minutes_metadata_payload(
                        transcript, planning_context=planning_context
                    ),
                    transcript_markdown=transcript_markdown,
                    topic_tags=load_topic_tag_vocabulary(),
                ),
                codex_bin=self.codex_bin,
                model=self.model,
                effort=self.effort,
                timeout_seconds=self.timeout_seconds,
                failure_label="Codex distillation",
                output_schema=inline_minutes_output_schema(),
                sandbox="read-only",
                cwd=tmp_path,
            )
            parsed = parse_inline_minutes_output_json(output)
        return DistilledMinutes(
            source_key=transcript.key,
            title=transcript.title,
            markdown=parsed.markdown,
            distiller=self.name,
            tags=parsed.tags,
        )


def get_distiller(
    name: str = "codex",
    *,
    codex_bin: str = DEFAULT_CODEX_BIN,
    timeout_seconds: int = DEFAULT_DISTILL_TIMEOUT_SECONDS,
    model: str | None = None,
    effort: str | None = None,
) -> Distiller:
    if name == "codex":
        return CodexDistiller(
            codex_bin=codex_bin,
            timeout_seconds=timeout_seconds,
            model=model or DEFAULT_CODEX_MODEL,
            effort=effort or DEFAULT_CODEX_EFFORT,
        )
    raise ValueError(f"unknown distiller: {name}")


def minutes_metadata_payload(
    transcript: TranscriptDoc,
    planning_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "meeting_id": transcript.meeting_id,
        "source_key": transcript.key,
        "title": transcript.title,
        "date": transcript.date,
    }
    if planning_context is not None:
        payload["planning_context"] = dict(planning_context)
    return payload


def inline_minutes_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "minutes_markdown": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["minutes_markdown", "tags"],
    }


def build_minutes_inline_prompt(
    *,
    metadata: Mapping[str, Any],
    transcript_markdown: str,
    topic_tags: tuple[str, ...],
) -> str:
    return (
        "\n".join(
            [
                "# Minutes Writer Inline Prompt",
                "",
                "あなたは会議内容を実務で使える議事録に編集する担当です。",
                "復元済み文字起こしを一次情報として、論点、決定、未決、次アクションが後から追えるMarkdown議事録を作成してください。",
                "ファイル作成、ファイル編集、Web、MCPは禁止。planning_context.additional_referencesに列挙されたローカルファイルは、必要なものを読み取り専用で直接読む。",
                "まずrestored_transcript_markdownだけで現在の会議を理解し、その後で過去会議を含むadditional_referencesを読み、省略された主語、継続案件、固有名詞、前回からの変化を補う。",
                "過去会議の文脈は判断材料として使ってよい。現在の会議と食い違う場合は現在の会議を優先する。",
                "過去の合意、決定、ToDoを今回のものとして転載しない。",
                "議事録を書く前に、現在会議の各作業項目を、案件・依頼元、施策、成果物、前回状態、今回の変更へ内部で対応づける。過去会議から一意に補える関係は補い、この内部整理自体は出力しない。",
                "過去会議の文脈が現在会議の作業対象、優先順位、案件と施策・成果物の関係を理解するために必要なら、その関係を議事録で省略しない。現在の発話で固有名詞が省略されていても、過去からの継続が一意に読める場合は補ってよい。",
                "",
                "## 出力",
                "",
                "最終応答はJSONだけ。`minutes_markdown` に議事録Markdown本文を入れる。",
                "frontmatter、# タイトル、関連ファイル、基本情報、toggle、details、HTML、表は禁止。",
                "必須セクションは `## 要約`、`## アジェンダ`、`## ToDo`、`## 合意事項`、`## 決定事項`、`## 主な議事・ポイント`、`## 要確認事項` の順で出す。",
                "ToDoは `- [ ] 担当者: タスク（期限: YYYY-MM-DD）`。担当者か期限が不明なら要確認事項へ入れる。ToDoがない場合、`## ToDo` の本文は空にする。",
                "tagsはallowed_topic_tagsから会議の主題に該当する1〜4個だけ選び、迷ったら空配列にする。語彙外生成は禁止。",
                "現在の会議とadditional_referencesのどちらにも根拠がない人物、数字、期限、合意、決定を作らない。",
                "Pythonは議事録の内容を補正しない。出力前に自分で全セクションを見直し、発話の根拠、案件と施策の関係、担当者と期限、決定と未決の区別を整合させる。",
                "長尺文字起こしのchunk境界に重複や表現のずれがある場合は、全文文脈で同じ事実を一度だけ議事録化する。原文の発話を書き換えたり、欠落を推測で埋めない。",
                "",
                "## meeting_metadata_json",
                "",
                "```json",
                json.dumps(metadata, ensure_ascii=False, indent=2),
                "```",
                "",
                "## allowed_topic_tags",
                "",
                "```json",
                json.dumps(list(topic_tags), ensure_ascii=False, indent=2),
                "```",
                "",
                "## restored_transcript_markdown",
                "",
                "```text",
                transcript_markdown,
                "```",
            ]
        ).strip()
        + "\n"
    )


def parse_inline_minutes_output_json(
    output: str,
    *,
    topic_tags_path: Path = TOPIC_TAGS_PATH,
) -> MinutesWriterOutput:
    payload = json.loads(output.strip())
    if not isinstance(payload, dict):
        raise ValueError("minutes JSON must be an object")
    markdown = normalize_minutes_markdown(clean_block(payload.get("minutes_markdown")))
    validate_v2_minutes_markdown(markdown)
    vocabulary = load_topic_tag_vocabulary(topic_tags_path)
    tags = validate_topic_tags(payload.get("tags", []), vocabulary=vocabulary)
    return MinutesWriterOutput(markdown=markdown, tags=tags)


def load_topic_tag_vocabulary(path: Path = TOPIC_TAGS_PATH) -> tuple[str, ...]:
    tags: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not cells:
            continue
        tag = clean_inline(cells[0])
        if not tag or tag == "タグ" or set(tag) <= {"-"}:
            continue
        if tag not in tags:
            tags.append(tag)
    return tuple(tags)


def validate_topic_tags(value: Any, *, vocabulary: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        return ()
    if not isinstance(value, list):
        LOGGER.warning("minutes topic tags dropped: reason=invalid_type count=1 label=topic_tags")
        return ()
    allowed = set(vocabulary)
    tags: list[str] = []
    unknown_count = 0
    over_limit_count = 0
    for item in value:
        tag = clean_inline(item)
        if not tag:
            continue
        if tag not in allowed:
            unknown_count += 1
            continue
        if tag in tags:
            continue
        if len(tags) >= MAX_TOPIC_TAGS:
            over_limit_count += 1
            continue
        tags.append(tag)
    if unknown_count:
        LOGGER.warning(
            "minutes topic tags dropped: reason=unknown count=%d label=topic_tags",
            unknown_count,
        )
    if over_limit_count:
        LOGGER.warning(
            "minutes topic tags dropped: reason=limit count=%d label=topic_tags",
            over_limit_count,
        )
    return tuple(tags)


def normalize_minutes_markdown(markdown: str) -> str:
    text = strip_leading_title_heading(markdown.strip()).strip()
    if not text:
        raise ValueError("distiller markdown is empty")
    return text + "\n"


def strip_leading_title_heading(markdown: str) -> str:
    return re.sub(r"\A#(?!#)\s+.+(?:\r?\n)+", "", markdown, count=1).lstrip()


def clean_block(value: Any) -> str:
    return str(value or "").strip()


def clean_inline(value: Any) -> str:
    return " ".join(str(value or "").split())


def validate_v2_minutes_markdown(markdown: str) -> None:
    if re.match(r"\A---(?:\r?\n|$)", markdown):
        raise ValueError("distiller markdown must not include frontmatter")
    if re.search(r"^#(?!#)\s+\S", markdown, flags=re.MULTILINE):
        raise ValueError("distiller markdown must not include title heading")
    for section in LOCAL_STORE_SECTIONS:
        if re.search(rf"^##\s+{re.escape(section)}\s*$", markdown, flags=re.MULTILINE):
            raise ValueError(f"distiller markdown must not include local_store section: {section}")
    last_index = -1
    for section in REQUIRED_SECTIONS:
        match = re.search(rf"^##\s+{re.escape(section)}\s*$", markdown, flags=re.MULTILINE)
        if not match:
            raise ValueError(f"missing required minutes section: {section}")
        if section == REQUIRED_SECTIONS[0] and markdown[: match.start()].strip():
            raise ValueError(f"distiller markdown must start with required section: {section}")
        if match.start() < last_index:
            raise ValueError(
                f"minutes sections must appear in required order: {', '.join(REQUIRED_SECTIONS)}"
            )
        last_index = match.start()
    if re.search(r"<\s*/?\s*details\b|\btoggle\b", markdown, flags=re.IGNORECASE):
        raise ValueError("toggle/details are not allowed in v2 minutes")
    for line in [
        line.strip() for line in section_body(markdown, "ToDo").splitlines() if line.strip()
    ]:
        if not re.match(r"^-\s+\[[ xX]\]\s+\S", line):
            raise ValueError(f"ToDo section must contain only Markdown checkbox items: {line}")


def section_body(markdown: str, section: str) -> str:
    match = re.search(rf"^##\s+{re.escape(section)}\s*$", markdown, flags=re.MULTILINE)
    if not match:
        return ""
    next_match = re.search(r"^##\s+\S.*$", markdown[match.end() :], flags=re.MULTILINE)
    return (
        markdown[match.end() : match.end() + next_match.start()]
        if next_match
        else markdown[match.end() :]
    )


def format_transcript(transcript: TranscriptDoc) -> str:
    return "\n".join(format_any_utterance(utterance) for utterance in transcript.utterances)


def format_any_utterance(utterance: Any) -> str:
    timestamp = getattr(utterance, "timestamp", "")
    speaker = getattr(utterance, "speaker", "")
    text = getattr(utterance, "text", "")
    prefix = " ".join(
        part
        for part in (f"[{timestamp}]" if timestamp else "", f"{speaker}:" if speaker else "")
        if part
    )
    return f"{prefix} {text}".strip() if prefix else str(text)


__all__ = [
    "CodexDistiller",
    "DistilledMinutes",
    "get_distiller",
    "validate_v2_minutes_markdown",
]
