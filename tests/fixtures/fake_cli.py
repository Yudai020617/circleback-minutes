#!/usr/bin/env python3
"""Synthetic CLI replies for offline tests. Never connects to an account."""
import json
import os
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
utterances = [
    {"timestamp": f"00:{i * 5:02d}", "speaker": "参加者A", "text": "今日は プロジェクトの 進捗を確認します。" if i == 0 else f"確認事項{i}を整理します。"}
    for i in range(12)
]
metadata = {"id": "demo001", "title": "サンプル定例", "startedAt": "2026-01-10T10:00:00+09:00", "durationSeconds": 60, "attendees": [{"name": "参加者A"}]}
if name == "circleback":
    if sys.argv[1:3] == ["meetings", "search"]:
        result = [metadata]
    elif sys.argv[1:3] == ["meetings", "read"]:
        result = metadata
    elif sys.argv[1:3] == ["transcripts", "read"]:
        result = {**metadata, "utterances": utterances}
    else:
        raise SystemExit(2)
elif name == "claude":
    prompt = sys.stdin.read()
    assert "judge_input" in prompt
    result = {"subtype": "success", "is_error": False, "result": json.dumps({
        "client": "", "client_url": "", "meeting_type": "定例-社内", "share": "open",
        "speakers": [{"name": "参加者A", "tokens": ["S1"], "url": ""}],
        "meeting_context": {"overview": "進捗を確認する会議", "main_topics": ["進捗"], "context_keywords": []},
        "additional_references": [], "filename": {"subject": "サンプル", "summary": "進捗確認"},
        "edits": [], "doubt_flags": []
    }, ensure_ascii=False)}
elif name == "codex":
    prompt = sys.stdin.read()
    schema = json.loads(Path(sys.argv[sys.argv.index("--output-schema") + 1]).read_text())
    assert "exec" in sys.argv and "read-only" in sys.argv
    if os.environ.get("DEMO_CODEX_FAIL"):
        print("synthetic failure", file=sys.stderr)
        raise SystemExit(1)
    if "restored_transcript_markdown" in schema["properties"]:
        assert "今日はプロジェクトの進捗を確認します。" in prompt
        result = {
            "restored_transcript_markdown": "\n".join(f"[{u['timestamp']}] 参加者A: {u['text'].replace(' ', '')}" for u in utterances),
            "needs_review_items": [], "terms_candidates": []
        }
    else:
        assert "今日はプロジェクトの進捗を確認します。" in prompt
        result = {"minutes_markdown": "## 要約\nプロジェクトの進捗を確認した。\n\n## アジェンダ\n- 進捗\n\n## ToDo\n- [ ] 参加者A: 確認事項を整理する\n\n## 合意事項\n- 確認事項を整理する\n\n## 決定事項\n- なし\n\n## 主な議事・ポイント\n- 進捗を確認した\n\n## 要確認事項\n- なし\n", "tags": []}
    Path(sys.argv[sys.argv.index("--output-last-message") + 1]).write_text(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0)
else:
    raise SystemExit(2)
print(json.dumps(result, ensure_ascii=False))
