import os
import re
import subprocess
import threading
import time
import uuid

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

SESSION_TTL = 3600  # 1時間でセッションを期限切れにする
session_last_active = {}
thread_session_ids = {}


def get_thread_lock(thread_ts):
    with thread_locks_lock:
        if thread_ts not in thread_locks:
            thread_locks[thread_ts] = threading.Lock()
        return thread_locks[thread_ts]


def cleanup_expired_sessions():
    """期限切れのセッションとロックを削除する。sessions_lockの外から呼ぶこと。"""
    now = time.time()
    with sessions_lock:
        expired = [
            ts
            for ts, last in session_last_active.items()
            if now - last > SESSION_TTL
        ]
        for ts in expired:
            active_sessions.discard(ts)
            session_last_active.pop(ts, None)
            thread_session_ids.pop(ts, None)
        # thread_locks_lockもsessions_lock内で取得する（ロック順序: sessions_lock → thread_locks_lock）
        with thread_locks_lock:
            for ts in expired:
                thread_locks.pop(ts, None)


def _touch_session(thread_ts):
    """セッションの最終アクティブ時刻を更新する。sessions_lockを保持した状態で呼ぶこと。"""
    session_last_active[thread_ts] = time.time()


def strip_mention(text):
    return re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()


def run_claude(prompt, thread_ts):
    session_id = thread_session_ids.get(thread_ts)
    if session_id is None:
        # 初回: 新規セッションを作成
        session_id = str(uuid.uuid4())
        cmd = ["claude", "-p", "--session-id", session_id, prompt]
    else:
        # 2回目以降: 既存セッションを再開
        cmd = ["claude", "-p", "--resume", session_id, prompt]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() if result.stderr else f"claude exited with code {result.returncode}")

    # 成功したらセッションIDを記録
    thread_session_ids[thread_ts] = session_id
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
            response = run_claude(text, thread_ts)
            if not response:
                response = "応答を生成できませんでした。"
            if len(response) > 3900:
                response = response[:3900] + "\n\n... (truncated)"
            say(text=response, thread_ts=thread_ts)
        except subprocess.TimeoutExpired:
            say(text="タイムアウトしました（5分）。", thread_ts=thread_ts)
        except Exception:
            say(text="エラーが発生しました。", thread_ts=thread_ts)
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

    cleanup_expired_sessions()
    with sessions_lock:
        active_sessions.add(thread_ts)
        _touch_session(thread_ts)

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
        _touch_session(thread_ts)

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
