# Circleback Minutes

Circlebackの文字起こしを取得し、機械的な前処理 → 会議の構造・話者の判断 → 言い間違い・文字起こしの修正 → 議事録作成まで実行する、日本語向けのローカルアプリです。

既存の `circleback-local-minutes` アプリの処理本体とプロンプトをコピーし、個人PCの固定パスや社内ツールへの依存を取り除いた配布版です。修正辞書・名簿・案件情報は空の状態です。実際の会議データ、認証情報、過去のGit履歴は含みません。

## 処理の流れ

```text
Circleback CLI
  ↓ 会議情報・文字起こしを取得
機械的な前処理（空白整理、話者トークン、発話行ID）
  ↓
Claude Code（話者・会議分類・編集指示の判断）
  ↓ 編集指示を機械適用
Codex CLI（文脈・辞書を使った文字起こしの復元）
  ↓
Codex CLI（要約・ToDo・合意事項などの議事録）
  ↓
workspace/50_議事録/<会議名>/transcript.md と minutes.md
```

復元は既存の文字起こしと文脈を使います。音声の再文字起こしは行いません。判断が曖昧な箇所は確認事項として残ります。モデルが出力した内容は、必要に応じて原文と照合してください。

## 必要なもの

- macOS / Linux。WindowsはWSL2上で実行してください。
- Python **3.14以上**（3.13以前では動きません）。
- Node.js 18以上とnpm。
- 利用者自身のCircleback、Codex CLI、Claude Codeのアカウントと利用権限。

Python本体の実行依存ライブラリはありません。テスト時のみpytestとruffを使用します。
このアプリは会議本文と参照情報をClaude / Codexへ送信します。認証や利用料金は各利用者の契約に従います。

## 最初の設定

```bash
git clone https://github.com/Yudai020617/circleback-minutes.git
cd circleback-minutes

# 元アプリが使用しているCircleback CLIのバージョン
npm install -g @circleback/cli@0.4.0 @openai/codex @anthropic-ai/claude-code

circleback auth login
codex login
claude
# Claude Codeの初回ログインを完了してから終了

python3.14 scripts/init_workspace.py
python3.14 scripts/run_circleback_local_pipeline.py --print-results
```

最後のコマンドは実行設定の確認だけです。会議取得、AI呼び出し、ファイル書き込みはしません。初期設定を繰り返しても既存の名簿や案件情報は上書きしません。

利用するモデルは環境変数で指定できます。元アプリの既定値は下表のとおりです。契約で利用できるモデル・推論設定に変更してください。

| 環境変数 | 既定値 | 用途 |
|---|---|---|
| `MINUTES_CODEX_MODEL` | `gpt-6-astra` | 復元・議事録作成 |
| `MINUTES_CODEX_EFFORT` | `high` | Codexの推論設定 |
| `MINUTES_CLAUDE_MODEL` | `claude-fable-5` | 会議の構造・話者判断 |
| `MINUTES_CLAUDE_EFFORT` | `xhigh` | Claudeの推論設定 |
| `MINUTES_WORKSPACE` | このディレクトリの `workspace/` | 出力・参照情報の保存先 |
| `MINUTES_CODEX_BIN` | `codex` | Codex実行ファイル |
| `MINUTES_CLAUDE_BIN` | `claude` | Claude実行ファイル |

例: `export MINUTES_CODEX_MODEL="利用可能なモデル名"`。`.env` の自動読込はありません。CLIがPATHにない場合は `--circleback-bin` / `--codex-bin` / `--claude-bin` で実行ファイルのパスを指定できます。

## 会議を処理する

Circlebackで会議IDを確認して1件を実行します。

```bash
circleback meetings search --json --last 7
python3.14 scripts/run_circleback_local_pipeline.py \
  --apply --meeting-id "会議ID" --print-results
```

`--meeting-id` は複数指定できます。処理済み会議は通常の再実行で重複作成しません。10発話未満の文字起こしは対象外として記録し、AIを呼び出しません。Circleback上の会議は削除しません。

検索でまとめて処理する場合は、初回の対象開始日時を明示します。

```bash
python3.14 scripts/run_circleback_local_pipeline.py \
  --apply --last-days 7 --limit 20 \
  --cutover-started-at "2026-01-01T00:00:00+09:00" --print-results
```

開始日時を指定しない初回検索実行では、その実行時刻以降が対象になります。既存会議を処理する最初の操作には、会議ID指定がおすすめです。保存した開始日時と処理状態は `var/state/` に残ります。会議の日付・保存名は日本時間です。

取得と前処理まで確認するには `--stop-after circleback`、判断までなら `--stop-after planning`、復元までなら `--stop-after restoration` を追加します。既存の段階別再処理オプションは `--help` で確認できます。

別の場所に保存したい場合は、環境変数を設定してから初期化・実行します。

```bash
export MINUTES_WORKSPACE="$HOME/Documents/meeting-workspace"
python3.14 scripts/init_workspace.py
python3.14 scripts/run_circleback_local_pipeline.py --apply --meeting-id "会議ID"
```

1回だけ指定する場合は、初期化に `--workspace`、処理に同じパスの `--root-path` を付けます。コード内のパスの書き換えは不要です。

## 辞書・名簿

空でも実行できます。必要な項目だけ利用者が追加してください。

| ファイル | 内容 |
|---|---|
| `knowledge/transcription-terms/common.md` | 共通の修正辞書 |
| `knowledge/transcription-terms/clients/` | 案件別の修正辞書 |
| `knowledge/speaker-aliases.md` | 話者名の揺れと正規名 |
| `knowledge/internal-people.md` | 社内人物名 |
| `knowledge/topic-tags.md` | 議事録タグ。空ならタグなし |
| `workspace/40_人物組織/members.json` | 社内名簿。形式は `{"members": [{"name": "参加者A"}]}` |
| `workspace/30_案件/clients-overview.md` | 案件名と参照ファイルへのリンク |

人物・組織の参照情報は `workspace/40_人物組織/` 配下へ置きます。参照ファイルはワークスペースまたはアプリのknowledge配下に限定されます。

## 保存物と再配布

- `workspace/50_議事録/`: 復元した文字起こしと議事録。
- `var/meetings/`: 取得・判断・復元・議事録の各段階の中間ファイル。
- `var/state/`: 処理済み会議の管理。
- `var/run/`: 実行状態、AIエラー時の診断ファイル。
- `knowledge/transcription-review-inbox.md`: 人が確認する項目。
- `knowledge/transcription-terms-candidates.md`: 辞書への追加候補。

`share: open / confidential` は保存ファイルの分類情報です。このアプリに公開機能・閲覧制限機能はありません。

`workspace/`・`var/`・`tmp/`・`logs/`・`.env` はGit管理対象外です。ただし辞書や確認事項を含む `knowledge/` は編集可能な配布ファイルです。利用後に再配布する場合は、新しいクローンを使ってください。実データ入りの作業ディレクトリをそのまま公開しないでください。

終了コードは成功 `0`、失敗 `1`、既に実行中 `75`、一部失敗 `76` です。`--print-results` は件数と失敗段階だけを出力します。詳細は `var/` 内で確認できます。診断ファイルには会議本文やAIへの入力が含まれることがあります。

## 動作確認

```bash
python3.14 -m venv .venv
. .venv/bin/activate
python -m pip install pytest ruff
python -m ruff check .
python -m pytest -q
```

テストは架空の会議と外部CLIの模擬応答を使い、別ホーム・日本語と空白を含む別ディレクトリから、全段階の接続、保存、重複防止、失敗処理を確認します。実アカウントへの接続やモデルの生成品質を保証するテストではありません。GitHub ActionsでもmacOSとLinuxで同じテストを実行します。

社内の監視設定、Slack連携、既存データ専用の移行スクリプトは配布対象に含めていません。通常実行・段階別処理・再処理を含む議事録作成の本体を収録しています。
