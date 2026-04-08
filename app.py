import logging
import os
import re
import subprocess
import threading
import uuid

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

load_dotenv()

CLAUDE_WORKING_DIR = os.environ.get("CLAUDE_WORKING_DIR")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "1800"))
CLAUDE_CHECK_INTERVAL = int(os.environ.get("CLAUDE_CHECK_INTERVAL", "300"))
IGNORE_NON_BOT_MENTIONS = os.environ.get("IGNORE_NON_BOT_MENTIONS", "1").strip().lower() in ("1", "true", "yes")

app = App(token=os.environ["SLACK_BOT_TOKEN"])


@app.middleware
def log_request(body, next):
    event = body.get("event", {})
    etype = event.get("type", "")
    subtype = event.get("subtype")
    if subtype:
        etype = f"{etype}({subtype})"
    user = event.get("user", "?")
    ch = event.get("channel", "?")
    ts = event.get("thread_ts", event.get("ts", ""))
    text = (event.get("text", "") or "")[:80]
    logger.info("[slack] %s user=%s ch=%s ts=%s | %s", etype, user, ch, ts, text)
    next()

BOT_USER_ID = None

active_sessions = set()
sessions_lock = threading.Lock()

thread_locks = {}
thread_locks_lock = threading.Lock()

thread_session_ids = {}

SESSION_METADATA_TYPE = "claude_session"


def make_session_metadata(session_id):
    return {"event_type": SESSION_METADATA_TYPE, "event_payload": {"session_id": session_id}}


def find_session_id_from_metadata(client, channel, thread_ts):
    """スレッド内の最新メタデータからセッションIDを復元する。"""
    resp = client.conversations_replies(
        channel=channel, ts=thread_ts, limit=100, include_all_metadata=True
    )
    for msg in reversed(resp.get("messages", [])):
        metadata = msg.get("metadata", {})
        if metadata.get("event_type") == SESSION_METADATA_TYPE:
            session_id = metadata.get("event_payload", {}).get("session_id")
            if session_id:
                logger.info("[metadata] recovered session=%s from thread ts=%s", session_id, thread_ts)
                return session_id
    return None


class SessionResumeError(Exception):
    pass


def get_thread_lock(thread_ts):
    with thread_locks_lock:
        if thread_ts not in thread_locks:
            thread_locks[thread_ts] = threading.Lock()
        return thread_locks[thread_ts]


def has_non_bot_mention(text):
    """テキストにBot以外のユーザーへのメンションが含まれているか判定する。"""
    mentions = re.findall(r"<@([A-Z0-9]+)>", text)
    return any(uid != BOT_USER_ID for uid in mentions)


def strip_mention(text):
    return re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()


MARKDOWN_BLOCK_MAX_LENGTH = 12000


def split_markdown(text, max_length=MARKDOWN_BLOCK_MAX_LENGTH):
    """テキストをmax_length以下のチャンクに分割する。段落境界で分割を試みる。"""
    if len(text) <= max_length:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break

        # 段落境界（空行）で分割を試みる
        split_pos = text.rfind("\n\n", 0, max_length)
        if split_pos == -1:
            # 行境界で分割を試みる
            split_pos = text.rfind("\n", 0, max_length)
        if split_pos == -1:
            # どちらもなければmax_lengthで強制分割
            split_pos = max_length

        chunks.append(text[:split_pos])
        text = text[split_pos:].lstrip("\n")

    return chunks


def run_claude(prompt, session_id, resume=False, on_still_running=None):
    if resume:
        cmd = ["claude", "-p", "--resume", session_id, prompt]
    else:
        cmd = ["claude", "-p", "--session-id", session_id, prompt]

    mode = "resume" if resume else "new"
    logger.info("[claude] >>> %s session=%s prompt=%s", mode, session_id, prompt[:120])

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=CLAUDE_WORKING_DIR,
    )

    stdout_chunks = []
    stderr_chunks = []
    t_out = threading.Thread(target=lambda: stdout_chunks.append(proc.stdout.read()), daemon=True)
    t_err = threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True)
    t_out.start()
    t_err.start()

    elapsed = 0
    while True:
        try:
            proc.wait(timeout=CLAUDE_CHECK_INTERVAL)
            break
        except subprocess.TimeoutExpired:
            elapsed += CLAUDE_CHECK_INTERVAL
            if elapsed >= CLAUDE_TIMEOUT:
                proc.kill()
                t_out.join()
                t_err.join()
                raise subprocess.TimeoutExpired(cmd, CLAUDE_TIMEOUT)
            if on_still_running:
                on_still_running(elapsed)

    t_out.join()
    t_err.join()

    stdout = stdout_chunks[0] if stdout_chunks else ""
    stderr = stderr_chunks[0] if stderr_chunks else ""

    if proc.returncode != 0:
        error_msg = (
            stderr.strip()
            if stderr
            else f"claude exited with code {proc.returncode}"
        )
        logger.error("[claude] <<< error (code=%d): %s", proc.returncode, error_msg[:200])
        if resume:
            raise SessionResumeError(error_msg)
        raise RuntimeError(f"claude CLI error: {error_msg}")

    output = stdout.strip()
    logger.info("[claude] <<< %d chars | %s", len(output), output[:120])
    return output


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
            def on_still_running(elapsed_seconds):
                minutes = elapsed_seconds // 60
                say(text=f"まだ処理中です… （経過: {minutes}分）", thread_ts=thread_ts)

            # セッションIDの復元/生成
            should_resume = thread_ts in thread_session_ids
            if not should_resume:
                try:
                    recovered_id = find_session_id_from_metadata(client, channel, thread_ts)
                except Exception:
                    logger.warning(
                        "[slack] failed to recover session metadata, starting new session ts=%s",
                        thread_ts,
                        exc_info=True,
                    )
                    recovered_id = None

                if recovered_id:
                    thread_session_ids[thread_ts] = recovered_id
                    should_resume = True
                else:
                    thread_session_ids[thread_ts] = str(uuid.uuid4())

            session_id = thread_session_ids[thread_ts]

            if should_resume:
                try:
                    response = run_claude(text, session_id, resume=True, on_still_running=on_still_running)
                except SessionResumeError:
                    logger.warning("[claude] resume failed, starting new session ts=%s", thread_ts)
                    session_id = str(uuid.uuid4())
                    thread_session_ids[thread_ts] = session_id
                    response = run_claude(text, session_id, on_still_running=on_still_running)
            else:
                response = run_claude(text, session_id, on_still_running=on_still_running)

            if not response:
                response = "応答を生成できませんでした。"

            chunks = split_markdown(response)
            for i, chunk in enumerate(chunks):
                kwargs = {
                    "blocks": [{"type": "markdown", "text": chunk}],
                    "text": chunk,
                    "thread_ts": thread_ts,
                }
                if i == len(chunks) - 1:
                    kwargs["metadata"] = make_session_metadata(session_id)
                say(**kwargs)
        except subprocess.TimeoutExpired:
            logger.error("[claude] timeout after %ds ts=%s", CLAUDE_TIMEOUT, thread_ts)
            say(text=f"タイムアウトしました（{CLAUDE_TIMEOUT // 60}分）。", thread_ts=thread_ts)
        except Exception:
            logger.exception("[error] ts=%s", thread_ts)
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

    with sessions_lock:
        active_sessions.add(thread_ts)

    threading.Thread(
        target=post_response,
        args=(text, channel, thread_ts, event["ts"], say, client),
        daemon=True,
    ).start()


def bot_has_replied(messages):
    """メッセージ一覧にBotの投稿が含まれているか判定する。"""
    return any(msg.get("user") == BOT_USER_ID for msg in messages)


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

    if IGNORE_NON_BOT_MENTIONS and has_non_bot_mention(text):
        logger.info("[skip] non-bot mention in thread ts=%s", thread_ts)
        return

    with sessions_lock:
        is_active = thread_ts in active_sessions

    if not is_active:
        try:
            resp = client.conversations_replies(
                channel=event["channel"], ts=thread_ts, limit=100
            )
        except Exception:
            return
        if not bot_has_replied(resp.get("messages", [])):
            return
        with sessions_lock:
            active_sessions.add(thread_ts)

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
    logger.info("Bot started (user_id=%s)", BOT_USER_ID)
    socket_handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    socket_handler.start()
