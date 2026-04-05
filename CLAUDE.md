# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

サーバー上のClaude CodeをSlackから操作するBot。Slack Bolt (Socket Mode) でメンションやスレッド返信を受け取り、`claude` CLIをサブプロセスとして呼び出して応答を返す。

## 開発環境のセットアップ

```bash
cp .env.example .env  # SLACK_BOT_TOKEN, SLACK_APP_TOKEN を設定
```

前提: [uv](https://docs.astral.sh/uv/)、`claude` CLIがインストール済みであること。

## 起動

```bash
uv run app.py
```

## アーキテクチャ

単一ファイル構成 (`app.py`)。主要な処理フロー:

1. `app_mention` イベント → 新規セッション開始。`claude -p --session-id <UUID>` で初回応答
2. スレッド内の後続メッセージ → `claude -p --resume <UUID>` でセッション継続
3. スレッドごとにロック (`thread_locks`) で直列化し、同一スレッドへの並行応答を防止
4. `active_sessions` でBot参加済みスレッドを管理。メンションなしのスレッドには反応しない
5. セッションは1時間 (`SESSION_TTL`) で期限切れ・自動クリーンアップ
