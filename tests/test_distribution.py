from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated(tmp_path):
    app = tmp_path / "別のPC 空白" / "circleback-minutes"
    shutil.copytree(ROOT, app, ignore=shutil.ignore_patterns(".git", "workspace", "var", "logs", ".venv", ".pytest_cache", "__pycache__", ".ruff_cache"))
    fake_home = tmp_path / "new-home"
    fake_home.mkdir()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ("circleback", "claude", "codex"):
        target = binaries / name
        fixture = (ROOT / "tests/fixtures/fake_cli.py").read_text()
        target.write_text(f"#!{sys.executable}\n" + fixture.split("\n", 1)[1])
        target.chmod(0o700)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("MINUTES_", "DEMO_", "PYTHON", "CLAUDE", "CODEX", "OPENAI", "ANTHROPIC"))}
    env.update(HOME=str(fake_home), PATH=str(binaries) + os.pathsep + os.environ["PATH"], PYTHONDONTWRITEBYTECODE="1")

    def run(script, *args, **extra):
        return subprocess.run([sys.executable, str(app / "scripts" / script), *args], env={**env, **extra}, cwd=tmp_path, text=True, capture_output=True, timeout=60)
    return app, run


def test_preview_needs_no_accounts_and_writes_nothing(isolated):
    app, run = isolated
    result = run("run_circleback_local_pipeline.py", "--print-results")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["failed"] == 0
    assert not (app / "var").exists()
    assert not (app / "workspace").exists()


@pytest.mark.parametrize("custom_root", [False, True])
def test_fresh_directory_runs_all_stages_and_is_idempotent(isolated, tmp_path, custom_root):
    app, run = isolated
    workspace = tmp_path / "別の保存先 空白" if custom_root else app / "workspace"
    init = run("init_workspace.py", "--workspace", str(workspace))
    assert init.returncode == 0, init.stderr
    args = ["--apply", "--meeting-id", "demo001", "--print-results"]
    if custom_root:
        args += ["--root-path", str(workspace)]
    result = run("run_circleback_local_pipeline.py", *args)
    assert result.returncode == 0, result.stdout + result.stderr
    records = json.loads(result.stdout)
    assert records["total"] == 1 and records["failed"] == 0, records
    minutes = list((workspace / "50_議事録").glob("*/minutes.md"))
    assert len(minutes) == 1
    assert "プロジェクトの進捗を確認した。" in minutes[0].read_text()
    transcript = minutes[0].with_name("transcript.md")
    assert "今日はプロジェクトの進捗を確認します。" in transcript.read_text()
    assert (app / "var/run/circleback-writer.json").is_file()
    before = minutes[0].read_bytes()
    second = run("run_circleback_local_pipeline.py", *args)
    assert second.returncode == 0, second.stdout + second.stderr
    assert minutes[0].read_bytes() == before
    assert len(list((workspace / "50_議事録").glob("*/minutes.md"))) == 1
    if custom_root:
        assert not (app / "workspace").exists()


def test_failed_model_does_not_report_success_or_create_minutes(isolated):
    app, run = isolated
    assert run("init_workspace.py").returncode == 0
    result = run("run_circleback_local_pipeline.py", "--apply", "--meeting-id", "demo001", "--print-results", DEMO_CODEX_FAIL="1")
    assert result.returncode == 1
    assert json.loads(result.stdout)["failed"] == 1
    assert not list((app / "workspace/50_議事録").glob("*/minutes.md"))


def test_init_does_not_replace_user_data(isolated):
    app, run = isolated
    assert run("init_workspace.py").returncode == 0
    path = app / "workspace/40_人物組織/members.json"
    path.write_text('{"members": [{"name": "参加者A"}]}')
    assert run("init_workspace.py").returncode == 0
    assert "参加者A" in path.read_text()


def test_empty_topic_vocabulary_is_supported():
    from circleback_local.distill import load_topic_tag_vocabulary, validate_topic_tags
    assert load_topic_tag_vocabulary() == ()
    assert validate_topic_tags([], vocabulary=()) == ()


def test_dictionary_has_no_entries():
    text = (ROOT / "knowledge/transcription-terms/common.md").read_text()
    rows = [line for line in text.splitlines() if line.startswith("|")]
    assert len(rows) == 2
    assert list((ROOT / "knowledge/transcription-terms/clients").glob("*.md")) == []


def test_reference_cannot_escape_workspace(tmp_path, monkeypatch):
    from circleback_local import planning
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("test")
    (allowed / "escape.txt").symlink_to(outside)
    monkeypatch.setattr(planning, "REFERENCE_FILE_ROOT", allowed)
    with pytest.raises(ValueError):
        planning.file_url_path((allowed / "escape.txt").as_uri(), "test")


def test_local_runtime_lock_and_status(tmp_path, monkeypatch):
    from circleback_local import runtime
    monkeypatch.setattr(runtime, "APP_ROOT", tmp_path)
    first = runtime.local_runtime("test-job", "test")
    second = runtime.local_runtime("test-job", "test")
    with first.lock() as acquired:
        assert acquired
        with second.lock() as acquired_again:
            assert not acquired_again
    assert first.write_status("normal", "attempted", "completed", "finished")
    assert json.loads((tmp_path / "var/run/test-job.json").read_text())["reason_code"] == "completed"
