import dataclasses
from collections.abc import Callable, Mapping
from typing import Any

from .circleback_client import Utterance

_OPS = frozenset(
    {
        "delete_line",
        "replace_text",
        "split_line",
        "merge_into_previous",
        "set_speaker",
    }
)


class EditTargetResolutionError(ValueError):
    def __init__(self, edit_id: int, line_id: int) -> None:
        self.edit_id = edit_id
        self.line_id = line_id
        super().__init__(
            f"edit target does not match an original line: edit_id={edit_id} line_id={line_id}"
        )


def _identity(value: str) -> str:
    return value


def validate_edit_instructions(payload: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(payload, list):
        raise ValueError("edits must be a list")

    previous_line_id = 0
    for expected_id, edit in enumerate(payload, start=1):
        if not isinstance(edit, dict):
            raise ValueError("each edit must be an object")
        if type(edit.get("id")) is not int or edit["id"] != expected_id:
            raise ValueError("edit ids must be consecutive from 1")

        op = edit.get("op")
        if not isinstance(op, str) or op not in _OPS:
            raise ValueError("edit op is invalid")

        target = edit.get("target")
        if not isinstance(target, dict):
            raise ValueError("edit target must be an object")
        line_id = target.get("line_id")
        if set(target) != {"line_id"} or type(line_id) is not int or line_id < 1:
            raise ValueError("target must contain only a positive integer line_id")
        if line_id < previous_line_id:
            raise ValueError("edit target line_ids must be nondecreasing")
        previous_line_id = line_id

        if not isinstance(edit.get("reason"), str) or not edit["reason"]:
            raise ValueError("edit reason must be a nonempty string")

        if op == "replace_text" and (
            not isinstance(edit.get("new_text"), str) or not edit["new_text"]
        ):
            raise ValueError("replace_text requires nonempty new_text")
        if op == "split_line":
            parts = edit.get("parts")
            if not isinstance(parts, list) or not parts:
                raise ValueError("split_line requires nonempty parts")
            for part in parts:
                if (
                    not isinstance(part, dict)
                    or not isinstance(part.get("speaker"), str)
                    or not part["speaker"]
                    or not isinstance(part.get("text"), str)
                    or not part["text"]
                ):
                    raise ValueError("split_line parts require nonempty speaker and text")
        if op == "set_speaker" and (
            not isinstance(edit.get("speaker"), str) or not edit["speaker"]
        ):
            raise ValueError("set_speaker requires nonempty speaker")

    return tuple(payload)


def apply_edit_instructions(
    utterances: tuple[Utterance, ...],
    edits: tuple[dict[str, Any], ...],
    *,
    legend: Mapping[str, str],
    speaker_names_by_token: Mapping[str, str],
    unmark: Callable[[str], str] = _identity,
) -> tuple[Utterance, ...]:
    validated_edits = validate_edit_instructions(list(edits))
    resolved_indices = tuple(_resolve_target(utterances, edit) for edit in validated_edits)
    current = [(index, utterance, True) for index, utterance in enumerate(utterances)]

    for edit, source_index in zip(validated_edits, resolved_indices, strict=True):
        position = next(
            (index for index, item in enumerate(current) if item[0] == source_index),
            None,
        )
        if position is None:
            raise ValueError("edit targets a deleted line")

        _, utterance, speaker_is_token_label = current[position]
        op = edit["op"]
        if op == "delete_line":
            del current[position]
        elif op == "replace_text":
            current[position] = (
                source_index,
                dataclasses.replace(utterance, text=unmark(edit["new_text"])),
                speaker_is_token_label,
            )
        elif op == "split_line":
            current[position : position + 1] = [
                (
                    source_index,
                    dataclasses.replace(
                        utterance,
                        speaker=legend.get(part["speaker"], part["speaker"]),
                        text=unmark(part["text"]),
                    ),
                    part["speaker"] in legend,
                )
                for part in edit["parts"]
            ]
        elif op == "merge_into_previous":
            if position == 0:
                raise ValueError("cannot merge the first current line")
            previous_source, previous, previous_is_token_label = current[position - 1]
            current[position - 1] = (
                previous_source,
                dataclasses.replace(previous, text=f"{previous.text} {utterance.text}"),
                previous_is_token_label,
            )
            del current[position]
        else:
            speaker = edit["speaker"]
            current[position] = (
                source_index,
                dataclasses.replace(utterance, speaker=legend.get(speaker, speaker)),
                speaker in legend,
            )

    identified_names_by_label = {
        legend[token]: name for token, name in speaker_names_by_token.items() if token in legend
    }
    return tuple(
        dataclasses.replace(utterance, speaker=identified_names_by_label[utterance.speaker])
        if speaker_is_token_label and utterance.speaker in identified_names_by_label
        else utterance
        for _, utterance, speaker_is_token_label in current
    )


def _resolve_target(
    utterances: tuple[Utterance, ...],
    edit: dict[str, Any],
) -> int:
    line_id = edit["target"]["line_id"]
    if line_id > len(utterances):
        raise EditTargetResolutionError(edit["id"], line_id)
    return line_id - 1
