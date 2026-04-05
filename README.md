# Claude Code Slackbot

GCEインスタンス上のClaude CodeをSlackから操作するためのBot。

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
3. 「Save Changes」をクリック

### 4. Bot Token Scopesを設定する

1. 左メニュー「OAuth & Permissions」を開く
2. 「Scopes」→「Bot Token Scopes」に以下を追加:
   - `app_mentions:read`
   - `chat:write`
3. ページ上部の「Install to Workspace」をクリックし、権限を許可
4. 表示される `xoxb-` で始まるBot User OAuth Tokenを控える

### 5. Botをチャンネルに招待する

使用したいSlackチャンネルで `/invite @Claude Code`（作成時のApp名）を実行してBotを招待する。

## サーバーのセットアップ

```bash
cd /home/anazawa/private/claudecode-slackbot

# 仮想環境の作成と有効化
python3 -m venv .venv
source .venv/bin/activate

# 依存パッケージのインストール
pip install -r requirements.txt

# 環境変数の設定
cp .env.example .env
# .env を編集し、控えておいたトークンを設定
```

## 起動

```bash
source .venv/bin/activate
python app.py
```

Botにメンションすると、サーバーの現在時刻を返す。
