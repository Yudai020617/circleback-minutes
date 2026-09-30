"""Shared execution gate definitions and parsers.

The execution gate vocabulary is intentionally narrow:

- no gate flag means inspect-only mode
- ``--apply`` means write/apply mode
- ``--confirm <exact>`` means explicit confirmation mode

This module does not inspect whether a confirmation value contains secrets.
Callers should avoid logging raw confirmation values.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class GateClass(StrEnum):
    """Canonical execution gate classes."""

    INSPECT = "inspect"
    APPLY = "apply"
    CONFIRM = "confirm"


JAPANESE_GATE_LABELS: Final[dict[GateClass, str]] = {
    GateClass.INSPECT: "そのまま",
    GateClass.APPLY: "反映",
    GateClass.CONFIRM: "危険",
}

ENGLISH_GATE_LABELS: Final[dict[GateClass, str]] = {
    GateClass.INSPECT: "inspect",
    GateClass.APPLY: "apply",
    GateClass.CONFIRM: "confirm",
}

ALLOWED_GATE_FLAGS: Final[tuple[str, ...]] = ("--apply", "--confirm")

BANNED_GATE_FLAG_CLASSES: Final[dict[str, GateClass]] = {
    "--execute": GateClass.APPLY,
    "--yes": GateClass.APPLY,
    "--live": GateClass.APPLY,
    "--real": GateClass.APPLY,
    "--no-dry-run": GateClass.APPLY,
    "--confirm-write": GateClass.CONFIRM,
    "--force": GateClass.CONFIRM,
    "--dry-run": GateClass.INSPECT,
    "--dry_run": GateClass.INSPECT,
}

BANNED_GATE_FLAGS: Final[tuple[str, ...]] = tuple(BANNED_GATE_FLAG_CLASSES)

BANNED_GATE_FLAG_PREFIX_CLASSES: Final[tuple[tuple[str, GateClass], ...]] = (
    ("--apply-", GateClass.APPLY),
    ("--apply=", GateClass.APPLY),
    ("--confirm-", GateClass.CONFIRM),
    ("--confirm=", GateClass.CONFIRM),
)

BANNED_GATE_FLAG_PREFIXES: Final[tuple[str, ...]] = tuple(
    prefix for prefix, _gate_class in BANNED_GATE_FLAG_PREFIX_CLASSES
)

OUT_OF_SCOPE_RISK_FLAGS: Final[tuple[str, ...]] = (
    "--dangerously-bypass-approvals-and-sandbox",
    "--full-auto",
    "--dangerously-skip-permissions",
    "--forceExit",
)

EXCEPTION_CATEGORIES: Final[tuple[str, ...]] = (
    "doc_forbidden_mention",
    "negative_test_fixture",
    "third_party_passthrough",
    "local runner_job_contract",
)

CONFIRM_EXACT_PARTS: Final[tuple[int, int]] = (3, 4)

GENERIC_CONFIRM_VALUES: Final[frozenset[str]] = frozenset(
    {
        "",
        "ok",
        "okay",
        "yes",
        "y",
        "execute",
        "exec",
        "confirm",
        "apply",
        "run",
        "true",
        "1",
    }
)

_ACTION_SCOPE_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_COUNT_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9]+$")


class ExecutionGateError(ValueError):
    """Base error for invalid execution gate tokens."""


class BannedExecutionGateFlagError(ExecutionGateError):
    """Raised when a banned execution gate flag is present."""


class InvalidConfirmExactError(ExecutionGateError):
    """Raised when ``--confirm`` is missing a valid exact value."""


class MultipleExecutionGateFlagsError(ExecutionGateError):
    """Raised when more than one allowed execution gate is present."""


@dataclass(frozen=True)
class NormalizedGateTokens:
    """Normalized result for a command token sequence."""

    gate_class: GateClass
    gate_flag: str | None = None
    confirm_exact: str | None = None

    @property
    def japanese_label(self) -> str:
        return JAPANESE_GATE_LABELS[self.gate_class]

    @property
    def english_label(self) -> str:
        return ENGLISH_GATE_LABELS[self.gate_class]


def classify_flag(token: str) -> GateClass | None:
    """Return the gate class signaled by a flag token, if any.

    Banned gate aliases still return the class they attempt to express.
    Use ``is_banned_gate_flag`` before accepting a token.
    """
    if token == "--apply":
        return GateClass.APPLY
    if token == "--confirm":
        return GateClass.CONFIRM
    if token in BANNED_GATE_FLAG_CLASSES:
        return BANNED_GATE_FLAG_CLASSES[token]

    for prefix, gate_class in BANNED_GATE_FLAG_PREFIX_CLASSES:
        if token.startswith(prefix):
            return gate_class

    return None


def is_banned_gate_flag(token: str) -> bool:
    """Return True when a token is a forbidden execution gate flag."""
    if token in BANNED_GATE_FLAG_CLASSES:
        return True
    return any(token.startswith(prefix) for prefix in BANNED_GATE_FLAG_PREFIXES)


def is_allowed_gate_flag(token: str) -> bool:
    """Return True only for the two allowed gate flag tokens."""
    return token in ALLOWED_GATE_FLAGS


def is_out_of_scope_risk_flag(token: str) -> bool:
    """Return True for known risky flags that are not execution gate flags."""
    return token in OUT_OF_SCOPE_RISK_FLAGS


def validate_confirm_exact(value: str) -> bool:
    """Validate ``action:scope:target`` or ``action:scope:target:count``.

    This only validates shape and rejects low-specificity generic values.
    It intentionally does not inspect whether the value contains secrets.
    """
    if value != value.strip():
        return False

    lowered = value.lower()
    if lowered in GENERIC_CONFIRM_VALUES:
        return False

    parts = value.split(":")
    if len(parts) not in CONFIRM_EXACT_PARTS:
        return False
    if any(part == "" for part in parts):
        return False

    action, scope, target = parts[:3]
    if (
        action.lower() in GENERIC_CONFIRM_VALUES
        or scope.lower() in GENERIC_CONFIRM_VALUES
        or target.lower() in GENERIC_CONFIRM_VALUES
    ):
        return False

    if not _ACTION_SCOPE_RE.fullmatch(action):
        return False
    if not _ACTION_SCOPE_RE.fullmatch(scope):
        return False

    return len(parts) != 4 or _COUNT_RE.fullmatch(parts[3]) is not None


def normalize_gate_tokens(tokens: Iterable[str]) -> NormalizedGateTokens:
    """Normalize execution gate tokens in a command token sequence.

    Non-gate tokens and out-of-scope risk flags are ignored here. Banned gate
    flags fail closed. ``--confirm`` must be followed by a valid exact value.
    """
    token_list = list(tokens)
    normalized = NormalizedGateTokens(GateClass.INSPECT)
    index = 0

    while index < len(token_list):
        token = token_list[index]

        if is_banned_gate_flag(token):
            raise BannedExecutionGateFlagError("banned execution gate flag")

        if token == "--apply":
            normalized = _set_gate_once(normalized, GateClass.APPLY, "--apply", None)
            index += 1
            continue

        if token == "--confirm":
            if index + 1 >= len(token_list):
                raise InvalidConfirmExactError("missing confirm exact value")

            confirm_exact = token_list[index + 1]
            if not validate_confirm_exact(confirm_exact):
                raise InvalidConfirmExactError("invalid confirm exact value")

            normalized = _set_gate_once(
                normalized,
                GateClass.CONFIRM,
                "--confirm",
                confirm_exact,
            )
            index += 2
            continue

        index += 1

    return normalized


def _set_gate_once(
    current: NormalizedGateTokens,
    gate_class: GateClass,
    gate_flag: str,
    confirm_exact: str | None,
) -> NormalizedGateTokens:
    if current.gate_class is not GateClass.INSPECT:
        raise MultipleExecutionGateFlagsError("multiple execution gate flags")
    return NormalizedGateTokens(gate_class, gate_flag, confirm_exact)
