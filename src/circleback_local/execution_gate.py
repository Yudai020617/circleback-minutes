from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from .execution_gates import (
    ExecutionGateError,
    GateClass,
    NormalizedGateTokens,
    normalize_gate_tokens,
)


def add_execution_gate_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--apply", action="store_true", help="Apply normal writes.")
    parser.add_argument(
        "--confirm",
        metavar="EXACT",
        help="Run a dangerous write using an exact confirmation value.",
    )


def parse_execution_gate(
    parser: argparse.ArgumentParser, argv: Sequence[str] | None
) -> NormalizedGateTokens:
    tokens = list(sys.argv[1:] if argv is None else argv)
    try:
        return normalize_gate_tokens(tokens)
    except ExecutionGateError as error:
        parser.error(str(error))


def apply_execution_gate(args: argparse.Namespace, gate: NormalizedGateTokens) -> None:
    args.execution_gate = gate
    args.execute = gate.gate_class in {GateClass.APPLY, GateClass.CONFIRM}


def fail_gate(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(2)


__all__ = [
    "GateClass",
    "add_execution_gate_arguments",
    "apply_execution_gate",
    "fail_gate",
    "parse_execution_gate",
]
