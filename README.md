# Claude Code Slackbot

サーバー上のClaude CodeをSlackから操作するためのBot。
メンションするとClaude Codeのセッションが開始され、スレッド内で会話を続けることができる。

## 前提条件

- [uv](https://docs.astral.sh/uv/)
- `claude` CLI がインストール済みで、PATHが通っていること

## Slack Appのセットアップ

### 1. Slack Appを作成する

1. [Slack API](https://api.slack.com/apps) にアクセスし「Create New App」→「From scratch」を選択
2. App名（例: `Claude Code`）とワークスペースを指定して作成

### 2. Socket Modeを有効化する

1. 左メニュー「Socket Mode」を開き、Socket Modeを有効化
2. App-Level Tokenの作成を求められるので、Token名（例: `socket-token`）を入力し、Scopeに `connections:write` を追加して「Generate」
3. 生成された `xapp-` で始まるトークンを控える

### 3. Event Subscriptionsを設定する

1. 左メニュー「Event Subscriptions」を開き、Enable Eventsをオンにする
2. 「Subscribe to bot events」で以下を追加:
   - `app_mention`
   - `message.channels`（スレッドでの会話継続に必要）
3. 「Save Changes」をクリック

### 4. Bot Token Scopesを設定する

1. 左メニュー「OAuth & Permissions」を開く
2. 「Scopes」→「Bot Token Scopes」に以下を追加:
   - `app_mentions:read`
   - `chat:write`
   - `channels:history`（スレッド内メッセージの受信に必要）
   - `reactions:write`（処理中インジケーターに必要）
3. ページ上部の「Install to Workspace」をクリックし、権限を許可
4. 表示される `xoxb-` で始まるBot User OAuth Tokenを控える

> **Note:** プライベートチャンネルでも使用する場合は、`message.groups` イベントと `groups:history` スコープも追加する。

### 5. Botをチャンネルに招待する

使用したいSlackチャンネルで `/invite @Claude Code`（作成時のApp名）を実行してBotを招待する。

## サーバーのセットアップ

```bash
git clone https://github.com/aaaanwz/claudecode-slackbot.git
cd claudecode-slackbot

# 環境変数の設定
cp .env.example .env
# .env を編集し、控えておいたトークンを設定
```

## 起動

```bash
uv run app.py
```

## テスト

```bash
uv run --group dev pytest
```

## サービスとして常駐させる (systemd)

### 1. ユニットファイルを配置する

リポジトリに含まれる `claudecode-slackbot.service` を編集し、パスとユーザーを環境に合わせて書き換える。

```bash
sudo cp claudecode-slackbot.service /etc/systemd/system/
sudo vi /etc/systemd/system/claudecode-slackbot.service
```

書き換える箇所:
- `User=` — 実行ユーザー
- `WorkingDirectory=` — リポジトリの絶対パス
- `EnvironmentFile=` — `.env` の絶対パス
- `ExecStart=` の `uv` パス — `which uv` で確認
- `Environment=PATH=` — `claude` CLI を含むディレクトリを追加（`which claude` で確認）

### 2. サービスを有効化・起動する

```bash
sudo systemctl daemon-reload
sudo systemctl enable claudecode-slackbot
sudo systemctl start claudecode-slackbot
```

### 3. 動作確認

```bash
sudo systemctl status claudecode-slackbot
sudo journalctl -u claudecode-slackbot -f
```

## 使い方

1. Botをメンションしてメッセージを送信すると、Claude Codeのセッションが開始される
2. スレッド内で返信すると、同じセッションで会話が継続される（メンション不要）
3. 処理中は :hourglass_flowing_sand: リアクションが表示される

## ライセンス

[MIT License](LICENSE)
