import os
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()

app = App(token=os.environ["SLACK_BOT_TOKEN"])

JST = timezone(timedelta(hours=9))


@app.event("app_mention")
def handle_mention(event, say):
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S %Z")
    say(text=f"現在のサーバー時刻: {now}", thread_ts=event["ts"])


if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
