#!/usr/bin/env python3
"""Create empty workspace files without overwriting existing data."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "src"))
from circleback_local.paths import BRAIN_ROOT


def initialize(root: Path) -> None:
    os.umask(0o077)
    for directory in ("30_案件", "40_人物組織/persons", "40_人物組織/organizations", "50_議事録"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    templates = {
        "30_案件/clients-overview.md": "# 案件一覧\n\n利用する案件の名前と参照ファイルへのリンクを追加してください。\n",
        "40_人物組織/members.json": '{"members": []}\n',
    }
    for relative, content in templates.items():
        path = root / relative
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(content)
        except FileExistsError:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=BRAIN_ROOT)
    args = parser.parse_args()
    root = args.workspace.expanduser().resolve()
    initialize(root)
    print(f"Workspace ready: {root}")
