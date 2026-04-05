import os
import re
import subprocess
import threading

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()

app = App(token=os.environ["SLACK_BOT_TOKEN"])

BOT_USER_ID = None

active_sessions = set()
sessions_lock = threading.Lock()

thread_locks = {}
thread_locks_lock = threading.Lock()


def get_thread_lock(thread_ts):
    with thread_locks_lock:
        if thread_ts not in thread_locks:
            thread_locks[thread_ts] = threading.Lock()
        return thread_locks[thread_ts]


def strip_mention(text):
    return re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()


def fetch_thread_history(client, channel, thread_ts):
    """スレッドの会話履歴を取得してプロンプト用のコンテキストを構築する"""
    result = client.conversations_replies(channel=channel, ts=thread_ts)
    messages = result.get("messages", [])

    history = []
    for msg in messages:
        if msg["ts"] == thread_ts and not msg.get("thread_ts"):
            # スレッドの親メッセージ（最初のメンション）
            role = "User"
        elif msg.get("bot_id") or (BOT_USER_ID and msg.get("user") == BOT_USER_ID):
            role = "Assistant"
        else:
            role = "User"
        text = strip_mention(msg.get("text", ""))
        if text:
            history.append(f"{role}: {text}")

    return history


def build_prompt(text, history):
    """会話履歴を含むプロンプトを構築する"""
    if len(history) <= 1:
        return text

    # 最後のメッセージ（今回の入力）は除外して履歴として渡す
    past = history[:-1]
    context = "\n".join(past)
    return f"以下はこれまでの会話履歴です:\n{context}\n\nUser: {text}"


def run_claude(prompt):
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0 and result.stderr:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def post_response(text, channel, thread_ts, event_ts, say, client):
    try:
        client.reactions_add(
            channel=channel, name="hourglass_flowing_sand", timestamp=event_ts
        )
    except Exception:
        pass

    lock = get_thread_lock(thread_ts)
    with lock:
        try:
            history = fetch_thread_history(client, channel, thread_ts)
            prompt = build_prompt(text, history)
            response = run_claude(prompt)
            if not response:
                response = "応答を生成できませんでした。"
            if len(response) > 3900:
                response = response[:3900] + "\n\n... (truncated)"
            say(text=response, thread_ts=thread_ts)
        except subprocess.TimeoutExpired:
            say(text="タイムアウトしました（5分）。", thread_ts=thread_ts)
        except Exception as e:
            say(text=f"エラーが発生しました: {e}", thread_ts=thread_ts)
        finally:
            try:
                client.reactions_remove(
                    channel=channel,
                    name="hourglass_flowing_sand",
                    timestamp=event_ts,
                )
            except Exception:
                pass


@app.event("app_mention")
def handle_mention(event, say, client):
    thread_ts = event.get("thread_ts", event["ts"])
    text = strip_mention(event.get("text", ""))
    channel = event["channel"]

    if not text:
        say(text="メッセージを入力してください。", thread_ts=thread_ts)
        return

    with sessions_lock:
        active_sessions.add(thread_ts)

    threading.Thread(
        target=post_response,
        args=(text, channel, thread_ts, event["ts"], say, client),
        daemon=True,
    ).start()


@app.event("message")
def handle_message(event, say, client):
    thread_ts = event.get("thread_ts")
    if not thread_ts:
        return

    if event.get("bot_id") or event.get("subtype"):
        return

    text = event.get("text", "")
    if BOT_USER_ID and f"<@{BOT_USER_ID}>" in text:
        return

    with sessions_lock:
        if thread_ts not in active_sessions:
            return

    text = strip_mention(text)
    if not text:
        return

    channel = event["channel"]

    threading.Thread(
        target=post_response,
        args=(text, channel, thread_ts, event["ts"], say, client),
        daemon=True,
    ).start()


if __name__ == "__main__":
    BOT_USER_ID = app.client.auth_test()["user_id"]
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
