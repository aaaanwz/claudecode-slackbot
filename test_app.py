import json
import os
import subprocess
import tempfile
import threading
from unittest.mock import ANY, MagicMock, patch

import pytest

import app


# --- strip_mention ---


class TestStripMention:
    def test_single_mention(self):
        assert app.strip_mention("<@U12345> hello") == "hello"

    def test_multiple_mentions(self):
        assert app.strip_mention("<@U12345> <@U67890> hello") == "hello"

    def test_no_mention(self):
        assert app.strip_mention("hello world") == "hello world"

    def test_only_mention(self):
        assert app.strip_mention("<@U12345>") == ""

    def test_mention_in_middle(self):
        assert app.strip_mention("hi <@U12345> there") == "hi there"


# --- has_non_bot_mention ---


class TestHasNonBotMention:
    def setup_method(self):
        self._original = app.BOT_USER_ID
        app.BOT_USER_ID = "B999"

    def teardown_method(self):
        app.BOT_USER_ID = self._original

    def test_no_mention(self):
        assert app.has_non_bot_mention("hello world") is False

    def test_bot_mention_only(self):
        assert app.has_non_bot_mention("<@B999> hello") is False

    def test_other_user_mention(self):
        assert app.has_non_bot_mention("<@U123> hello") is True

    def test_bot_and_other_mention(self):
        assert app.has_non_bot_mention("<@B999> <@U123> hello") is True

    def test_multiple_other_mentions(self):
        assert app.has_non_bot_mention("<@U123> <@U456> hello") is True


# --- split_markdown ---


class TestSplitMarkdown:
    def test_short_text(self):
        assert app.split_markdown("short") == ["short"]

    def test_split_at_paragraph_boundary(self):
        chunk1 = "a" * 100
        chunk2 = "b" * 100
        text = chunk1 + "\n\n" + chunk2
        result = app.split_markdown(text, max_length=150)
        assert result == [chunk1, chunk2]

    def test_split_at_line_boundary(self):
        chunk1 = "a" * 100
        chunk2 = "b" * 100
        text = chunk1 + "\n" + chunk2
        result = app.split_markdown(text, max_length=150)
        assert result == [chunk1, chunk2]

    def test_forced_split(self):
        text = "a" * 300
        result = app.split_markdown(text, max_length=150)
        assert len(result) == 2
        assert result[0] == "a" * 150
        assert result[1] == "a" * 150

    def test_empty_text(self):
        assert app.split_markdown("") == [""]

    def test_exact_length(self):
        text = "a" * 150
        assert app.split_markdown(text, max_length=150) == [text]


# --- bot_has_replied ---


class TestBotHasReplied:
    def setup_method(self):
        self._original = app.BOT_USER_ID
        app.BOT_USER_ID = "B123"

    def teardown_method(self):
        app.BOT_USER_ID = self._original

    def test_bot_replied(self):
        messages = [
            {"user": "U111", "text": "hello"},
            {"user": "B123", "text": "hi"},
        ]
        assert app.bot_has_replied(messages) is True

    def test_bot_not_replied(self):
        messages = [{"user": "U111", "text": "hello"}]
        assert app.bot_has_replied(messages) is False

    def test_empty(self):
        assert app.bot_has_replied([]) is False


# --- format_cost_line ---


class TestFormatCostLine:
    def test_none_returns_empty(self):
        assert app.format_cost_line(None) == ""

    def test_formats_cost(self):
        line = app.format_cost_line(0.1234)
        assert "$0.1234" in line
        assert "💰" in line

    def test_rounds_to_four_decimals(self):
        assert "$0.0106" in app.format_cost_line(0.01059725)


# --- run_claude ---


def _make_popen_mock(returncode=0, stdout="", stderr=""):
    """subprocess.Popenのモックを生成する。"""
    mock_proc = MagicMock()
    mock_proc.returncode = returncode
    mock_proc.stdout.read.return_value = stdout
    mock_proc.stderr.read.return_value = stderr
    mock_proc.wait.return_value = returncode
    return mock_proc


def _success_json(text="response text", cost=0.0123):
    """claude -p --output-format json の成功時stdoutを模したJSON文字列を返す。"""
    return json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": text, "total_cost_usd": cost}
    )


class TestRunClaude:
    @patch("app.subprocess.Popen")
    def test_new_session(self, mock_popen):
        mock_popen.return_value = _make_popen_mock(returncode=0, stdout=_success_json("response text", 0.0123))
        result = app.run_claude("hello", "session-123")
        assert result.text == "response text"
        assert result.cost_usd == 0.0123
        assert result.budget_exceeded is False

        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "claude"
        assert "--session-id" in cmd
        assert "session-123" in cmd
        # 費用制御まわりのフラグが付与されている
        assert "--output-format" in cmd
        assert "json" in cmd
        assert "--max-budget-usd" in cmd
        assert "--exclude-dynamic-system-prompt-sections" in cmd

    @patch("app.subprocess.Popen")
    def test_resume_session(self, mock_popen):
        mock_popen.return_value = _make_popen_mock(returncode=0, stdout=_success_json("resumed response", 0.5))
        result = app.run_claude("follow up", "existing-session-id", resume=True)
        assert result.text == "resumed response"
        assert result.cost_usd == 0.5

        cmd = mock_popen.call_args[0][0]
        assert "--resume" in cmd
        assert "existing-session-id" in cmd

    @patch("app.subprocess.Popen")
    def test_budget_exceeded_returns_flag(self, mock_popen):
        # 費用上限超過時は終了コード1だが、エラーにせず budget_exceeded=True を返す
        stdout = json.dumps(
            {
                "type": "result",
                "subtype": "error_max_budget_usd",
                "is_error": True,
                "total_cost_usd": 1.5,
                "errors": ["Reached maximum budget ($2)"],
            }
        )
        mock_popen.return_value = _make_popen_mock(returncode=1, stdout=stdout)
        result = app.run_claude("hello", "session-123")
        assert result.budget_exceeded is True
        assert result.cost_usd == 1.5
        assert result.text == ""

    @patch("app.subprocess.Popen")
    def test_resume_failure_raises_session_resume_error(self, mock_popen):
        mock_popen.return_value = _make_popen_mock(returncode=1, stderr="session not found")
        with pytest.raises(app.SessionResumeError):
            app.run_claude("hello", "bad-session", resume=True)

    @patch("app.subprocess.Popen")
    def test_new_session_failure_raises_runtime_error(self, mock_popen):
        mock_popen.return_value = _make_popen_mock(returncode=1, stderr="some error")
        with pytest.raises(RuntimeError, match="claude CLI error"):
            app.run_claude("hello", "session-123")


# --- find_session_id_from_metadata ---


class TestFindSessionIdFromMetadata:
    def test_finds_session_id(self):
        client = MagicMock()
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "U1", "text": "hello"},
                {
                    "user": "B1",
                    "bot_id": "B1",
                    "text": "response",
                    "metadata": {
                        "event_type": "claude_session",
                        "event_payload": {"session_id": "found-session"},
                    },
                },
            ]
        }
        result = app.find_session_id_from_metadata(client, "C1", "1234.5678")
        assert result == "found-session"
        client.conversations_replies.assert_called_once_with(
            channel="C1", ts="1234.5678", limit=100, include_all_metadata=True
        )

    def test_returns_latest_session_id(self):
        client = MagicMock()
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "B1",
                    "bot_id": "B1",
                    "text": "old",
                    "metadata": {
                        "event_type": "claude_session",
                        "event_payload": {"session_id": "old-session"},
                    },
                },
                {
                    "user": "B1",
                    "bot_id": "B1",
                    "text": "new",
                    "metadata": {
                        "event_type": "claude_session",
                        "event_payload": {"session_id": "new-session"},
                    },
                },
            ]
        }
        result = app.find_session_id_from_metadata(client, "C1", "1234.5678")
        assert result == "new-session"

    def test_returns_none_when_no_metadata(self):
        client = MagicMock()
        client.conversations_replies.return_value = {
            "messages": [{"user": "U1", "text": "hello"}]
        }
        result = app.find_session_id_from_metadata(client, "C1", "1234.5678")
        assert result is None


# --- post_response ---


def _make_client():
    """テスト用のSlackクライアントモックを作成する。"""
    client = MagicMock()
    client.conversations_replies.return_value = {"messages": []}
    return client


class TestPostResponse:
    def setup_method(self):
        self._sessions = app.thread_session_ids.copy()
        app.thread_session_ids.clear()
        self._locks = app.thread_locks.copy()
        app.thread_locks.clear()

    def teardown_method(self):
        app.thread_session_ids.clear()
        app.thread_session_ids.update(self._sessions)
        app.thread_locks.clear()
        app.thread_locks.update(self._locks)

    @patch("app.run_claude", return_value=app.ClaudeResult(text="bot reply", cost_usd=0.05, budget_exceeded=False))
    def test_resume_response(self, mock_claude):
        say = MagicMock()
        client = _make_client()
        app.thread_session_ids["ts1"] = "session1"

        app.post_response("hello", [], "C1", "ts1", "ev1", say, client)

        mock_claude.assert_called_once_with("hello", "session1", resume=True, on_still_running=ANY)
        client.reactions_add.assert_called_once()
        client.reactions_remove.assert_called_once()
        say.assert_called_once()
        # 応答本文と費用行の両方が含まれる
        assert "bot reply" in say.call_args[1]["text"]
        assert "💰" in say.call_args[1]["text"]
        # 応答にメタデータが付与される
        assert say.call_args[1]["metadata"]["event_type"] == "claude_session"
        assert say.call_args[1]["metadata"]["event_payload"]["session_id"] == "session1"

    @patch("app.run_claude", return_value=app.ClaudeResult(text="new reply", cost_usd=0.05, budget_exceeded=False))
    def test_new_session_response(self, mock_claude):
        say = MagicMock()
        client = _make_client()

        app.post_response("hello", [], "C1", "ts_new", "ev1", say, client)

        say.assert_called_once()
        assert "new reply" in say.call_args[1]["text"]
        # 応答にメタデータが付与される
        assert say.call_args[1]["metadata"]["event_type"] == "claude_session"

    @patch("app.run_claude", return_value=app.ClaudeResult(text="resumed reply", cost_usd=0.05, budget_exceeded=False))
    def test_recovers_session_from_metadata(self, mock_claude):
        say = MagicMock()
        client = _make_client()
        client.conversations_replies.return_value = {
            "messages": [
                {
                    "user": "B1",
                    "bot_id": "B1",
                    "text": "prev response",
                    "metadata": {
                        "event_type": "claude_session",
                        "event_payload": {"session_id": "recovered-id"},
                    },
                },
            ]
        }

        app.post_response("hello", [], "C1", "ts_recover", "ev1", say, client)

        assert app.thread_session_ids["ts_recover"] == "recovered-id"
        mock_claude.assert_called_once_with("hello", "recovered-id", resume=True, on_still_running=ANY)

    @patch("app.run_claude")
    def test_resume_failure_starts_new_session(self, mock_claude):
        mock_claude.side_effect = [
            app.SessionResumeError("fail"),
            app.ClaudeResult(text="new reply", cost_usd=0.05, budget_exceeded=False),
        ]
        say = MagicMock()
        client = _make_client()
        app.thread_session_ids["ts1"] = "session1"

        app.post_response("hello", [], "C1", "ts1", "ev1", say, client)

        assert mock_claude.call_count == 2
        first_call = mock_claude.call_args_list[0]
        assert first_call[1].get("resume") is True
        second_call = mock_claude.call_args_list[1]
        assert second_call[0][1] != "session1"
        assert second_call[1].get("resume", False) is False

    @patch("app.run_claude", side_effect=subprocess.TimeoutExpired("claude", 600))
    def test_timeout(self, mock_claude):
        say = MagicMock()
        client = _make_client()
        app.thread_session_ids["ts1"] = "session1"

        app.post_response("hello", [], "C1", "ts1", "ev1", say, client)

        say.assert_called_once()
        assert "タイムアウト" in say.call_args[1]["text"]
        client.reactions_remove.assert_called_once()

    @patch("app.run_claude", side_effect=RuntimeError("unexpected"))
    def test_generic_error(self, mock_claude):
        say = MagicMock()
        client = _make_client()
        app.thread_session_ids["ts1"] = "session1"

        app.post_response("hello", [], "C1", "ts1", "ev1", say, client)

        say.assert_called_once()
        assert "エラー" in say.call_args[1]["text"]
        client.reactions_remove.assert_called_once()

    @patch("app.run_claude", return_value=app.ClaudeResult(text="", cost_usd=None, budget_exceeded=False))
    def test_empty_response_fallback(self, mock_claude):
        say = MagicMock()
        client = _make_client()
        app.thread_session_ids["ts1"] = "session1"

        app.post_response("hello", [], "C1", "ts1", "ev1", say, client)

        assert "応答を生成できませんでした" in say.call_args[1]["text"]

    @patch("app.run_claude", return_value=app.ClaudeResult(text="hi", cost_usd=0.1234, budget_exceeded=False))
    def test_cost_line_appended(self, mock_claude):
        say = MagicMock()
        client = _make_client()
        app.thread_session_ids["ts1"] = "session1"

        app.post_response("hello", [], "C1", "ts1", "ev1", say, client)

        text = say.call_args[1]["text"]
        assert "hi" in text
        assert "$0.1234" in text

    @patch("app.run_claude", return_value=app.ClaudeResult(text="", cost_usd=2.0, budget_exceeded=True))
    def test_budget_exceeded_notifies_user(self, mock_claude):
        say = MagicMock()
        client = _make_client()
        app.thread_session_ids["ts1"] = "session1"

        app.post_response("hello", [], "C1", "ts1", "ev1", say, client)

        say.assert_called_once()
        text = say.call_args[1]["text"]
        assert "費用上限" in text
        assert "$2.0000" in text
        # 打ち切り通知にもセッションメタデータが付与される
        assert say.call_args[1]["metadata"]["event_type"] == "claude_session"
        client.reactions_remove.assert_called_once()

    @patch("app.run_claude", return_value=app.ClaudeResult(text="a" * 20000, cost_usd=0.1, budget_exceeded=False))
    def test_long_response_split(self, mock_claude):
        say = MagicMock()
        client = _make_client()
        app.thread_session_ids["ts1"] = "session1"

        app.post_response("hello", [], "C1", "ts1", "ev1", say, client)

        assert say.call_count == 2
        # 最初のチャンクにはメタデータなし
        assert "metadata" not in say.call_args_list[0][1]
        # 最後のチャンクにメタデータが付与される
        assert say.call_args_list[1][1]["metadata"]["event_type"] == "claude_session"


# --- handle_mention ---


class TestHandleMention:
    def setup_method(self):
        self._sessions = app.active_sessions.copy()
        app.active_sessions.clear()

    def teardown_method(self):
        app.active_sessions.clear()
        app.active_sessions.update(self._sessions)

    def test_empty_text_responds_with_prompt(self):
        say = MagicMock()
        client = MagicMock()
        event = {"text": "<@BOT>", "ts": "1234", "channel": "C1"}

        app.handle_mention(event, say, client)

        say.assert_called_once()
        assert "メッセージを入力してください" in say.call_args[1]["text"]

    @patch("app.threading.Thread")
    def test_starts_thread(self, mock_thread_cls):
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread
        say = MagicMock()
        client = MagicMock()
        event = {"text": "<@BOT> hello", "ts": "1234", "channel": "C1"}

        app.handle_mention(event, say, client)

        assert "1234" in app.active_sessions
        mock_thread.start.assert_called_once()

    @patch("app.threading.Thread")
    def test_uses_thread_ts_if_present(self, mock_thread_cls):
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread
        say = MagicMock()
        client = MagicMock()
        event = {
            "text": "<@BOT> hello",
            "ts": "1234",
            "thread_ts": "1000",
            "channel": "C1",
        }

        app.handle_mention(event, say, client)

        assert "1000" in app.active_sessions

    @patch("app.threading.Thread")
    def test_attachment_only_triggers_thread(self, mock_thread_cls):
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread
        say = MagicMock()
        client = MagicMock()
        event = {
            "text": "<@BOT>",
            "ts": "1234",
            "channel": "C1",
            "files": [{"id": "F1", "name": "a.txt", "url_private_download": "https://x/y"}],
        }

        app.handle_mention(event, say, client)

        say.assert_not_called()
        mock_thread.start.assert_called_once()
        args = mock_thread_cls.call_args[1]["args"]
        # post_response(text, files, channel, thread_ts, event_ts, say, client)
        assert args[0] == ""
        assert args[1] == event["files"]


# --- handle_message ---


class TestHandleMessage:
    def setup_method(self):
        self._sessions = app.active_sessions.copy()
        self._bot_user_id = app.BOT_USER_ID
        app.active_sessions.clear()
        app.BOT_USER_ID = "B999"

    def teardown_method(self):
        app.active_sessions.clear()
        app.active_sessions.update(self._sessions)
        app.BOT_USER_ID = self._bot_user_id

    def test_ignores_non_thread_message(self):
        say = MagicMock()
        client = MagicMock()
        event = {"text": "hello", "ts": "1234", "channel": "C1"}

        app.handle_message(event, say, client)

        say.assert_not_called()

    def test_ignores_bot_message(self):
        say = MagicMock()
        client = MagicMock()
        event = {
            "text": "hello",
            "ts": "1234",
            "thread_ts": "1000",
            "channel": "C1",
            "bot_id": "B1",
        }

        app.handle_message(event, say, client)

        say.assert_not_called()

    def test_ignores_non_file_subtype(self):
        say = MagicMock()
        client = MagicMock()
        event = {
            "text": "hello",
            "ts": "1234",
            "thread_ts": "1000",
            "channel": "C1",
            "subtype": "message_changed",
        }

        app.handle_message(event, say, client)

        say.assert_not_called()

    @patch("app.threading.Thread")
    def test_accepts_file_share_subtype(self, mock_thread_cls):
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread
        say = MagicMock()
        client = MagicMock()
        app.active_sessions.add("1000")
        event = {
            "text": "",
            "ts": "1234",
            "thread_ts": "1000",
            "channel": "C1",
            "subtype": "file_share",
            "files": [{"id": "F1", "name": "a.txt", "url_private_download": "https://x/y"}],
        }

        app.handle_message(event, say, client)

        mock_thread.start.assert_called_once()
        args = mock_thread_cls.call_args[1]["args"]
        assert args[0] == ""
        assert args[1] == event["files"]

    def test_ignores_mention_to_bot(self):
        say = MagicMock()
        client = MagicMock()
        event = {
            "text": "<@B999> hello",
            "ts": "1234",
            "thread_ts": "1000",
            "channel": "C1",
        }

        app.handle_message(event, say, client)

        say.assert_not_called()

    @patch("app.threading.Thread")
    def test_responds_in_active_thread(self, mock_thread_cls):
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread
        say = MagicMock()
        client = MagicMock()
        app.active_sessions.add("1000")
        event = {
            "text": "follow up",
            "ts": "1234",
            "thread_ts": "1000",
            "channel": "C1",
        }

        app.handle_message(event, say, client)

        mock_thread.start.assert_called_once()

    @patch("app.threading.Thread")
    def test_detects_bot_in_thread_and_activates(self, mock_thread_cls):
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread
        say = MagicMock()
        client = MagicMock()
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "U1", "text": "hi"},
                {"user": "B999", "text": "hello"},
            ]
        }
        event = {
            "text": "follow up",
            "ts": "1234",
            "thread_ts": "1000",
            "channel": "C1",
        }

        app.handle_message(event, say, client)

        assert "1000" in app.active_sessions
        mock_thread.start.assert_called_once()

    def test_ignores_thread_without_bot(self):
        say = MagicMock()
        client = MagicMock()
        client.conversations_replies.return_value = {
            "messages": [{"user": "U1", "text": "hi"}]
        }
        event = {
            "text": "follow up",
            "ts": "1234",
            "thread_ts": "1000",
            "channel": "C1",
        }

        app.handle_message(event, say, client)

        assert "1000" not in app.active_sessions

    @patch("app.IGNORE_NON_BOT_MENTIONS", True)
    def test_ignores_non_bot_mention_in_thread(self):
        say = MagicMock()
        client = MagicMock()
        app.active_sessions.add("1000")
        event = {
            "text": "<@U111> can you check this?",
            "ts": "1234",
            "thread_ts": "1000",
            "channel": "C1",
        }

        with patch("app.threading.Thread") as mock_thread_cls:
            app.handle_message(event, say, client)
            mock_thread_cls.assert_not_called()

    @patch("app.IGNORE_NON_BOT_MENTIONS", False)
    @patch("app.threading.Thread")
    def test_responds_to_non_bot_mention_when_flag_disabled(self, mock_thread_cls):
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread
        say = MagicMock()
        client = MagicMock()
        app.active_sessions.add("1000")
        event = {
            "text": "<@U111> can you check this?",
            "ts": "1234",
            "thread_ts": "1000",
            "channel": "C1",
        }

        app.handle_message(event, say, client)

        mock_thread.start.assert_called_once()

    def test_ignores_empty_text_in_active_thread(self):
        say = MagicMock()
        client = MagicMock()
        app.active_sessions.add("1000")
        event = {
            "text": "<@U111>",
            "ts": "1234",
            "thread_ts": "1000",
            "channel": "C1",
        }

        with patch("app.threading.Thread") as mock_thread_cls:
            app.handle_message(event, say, client)
            mock_thread_cls.assert_not_called()


# --- get_thread_lock ---


class TestGetThreadLock:
    def setup_method(self):
        self._locks = app.thread_locks.copy()
        app.thread_locks.clear()

    def teardown_method(self):
        app.thread_locks.clear()
        app.thread_locks.update(self._locks)

    def test_returns_same_lock(self):
        lock1 = app.get_thread_lock("ts1")
        lock2 = app.get_thread_lock("ts1")
        assert lock1 is lock2

    def test_returns_different_locks(self):
        lock1 = app.get_thread_lock("ts1")
        lock2 = app.get_thread_lock("ts2")
        assert lock1 is not lock2


# --- build_prompt ---


class TestBuildPrompt:
    def test_text_only(self):
        assert app.build_prompt("hello", []) == "hello"

    def test_files_only(self):
        prompt = app.build_prompt("", ["/tmp/claude/sess/a.png"])
        assert prompt == "添付ファイル:\n- /tmp/claude/sess/a.png"

    def test_text_and_files(self):
        prompt = app.build_prompt("hi", ["/tmp/claude/sess/a.png", "/tmp/claude/sess/b.txt"])
        assert prompt.startswith("hi\n\n添付ファイル:\n")
        assert "- /tmp/claude/sess/a.png" in prompt
        assert "- /tmp/claude/sess/b.txt" in prompt


# --- download_slack_files ---


def _make_urlopen_mock(content):
    """urllib.request.urlopen のコンテキストマネージャ風モックを生成する。"""
    cm = MagicMock()
    chunks = []
    for i in range(0, len(content), 64 * 1024):
        chunks.append(content[i:i + 64 * 1024])
    chunks.append(b"")
    cm.read.side_effect = chunks
    cm.__enter__.return_value = cm
    cm.__exit__.return_value = False
    return cm


class TestDownloadSlackFiles:
    _UNSET = object()

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_base = app.SLACK_ATTACHMENTS_BASE
        self._orig_token = os.environ.get("SLACK_BOT_TOKEN", self._UNSET)
        app.SLACK_ATTACHMENTS_BASE = self._tmpdir
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-test"

    def teardown_method(self):
        app.SLACK_ATTACHMENTS_BASE = self._orig_base
        if self._orig_token is self._UNSET:
            os.environ.pop("SLACK_BOT_TOKEN", None)
        else:
            os.environ["SLACK_BOT_TOKEN"] = self._orig_token
        import shutil as _sh
        _sh.rmtree(self._tmpdir, ignore_errors=True)

    def test_empty_files_returns_empty(self):
        assert app.download_slack_files([], "sess-1") == []
        assert app.download_slack_files(None, "sess-1") == []

    @patch("app.urllib.request.urlopen")
    def test_downloads_to_session_dir(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_mock(b"hello world")
        files = [{"id": "F1", "name": "note.txt", "url_private_download": "https://x/y"}]

        paths = app.download_slack_files(files, "sess-A")

        assert len(paths) == 1
        assert paths[0] == os.path.join(self._tmpdir, "sess-A", "note.txt")
        with open(paths[0], "rb") as f:
            assert f.read() == b"hello world"
        # Authorization ヘッダにBot Tokenが付く
        req = mock_urlopen.call_args[0][0]
        assert req.headers.get("Authorization") == "Bearer xoxb-test"

    @patch("app.urllib.request.urlopen")
    def test_skips_files_without_url(self, mock_urlopen):
        files = [{"id": "F1", "name": "a.txt"}]

        paths = app.download_slack_files(files, "sess-B")

        assert paths == []
        mock_urlopen.assert_not_called()

    @patch("app.urllib.request.urlopen", side_effect=OSError("network"))
    def test_continues_on_download_failure(self, mock_urlopen):
        files = [{"id": "F1", "name": "a.txt", "url_private_download": "https://x/y"}]

        paths = app.download_slack_files(files, "sess-C")

        assert paths == []

    @patch("app.urllib.request.urlopen")
    def test_falls_back_to_url_private(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_mock(b"x")
        files = [{"id": "F1", "name": "a.txt", "url_private": "https://x/p"}]

        paths = app.download_slack_files(files, "sess-D")

        assert len(paths) == 1
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://x/p"

    @patch("app.urllib.request.urlopen")
    def test_strips_path_components_from_name(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_mock(b"x")
        files = [{"id": "F1", "name": "../../etc/passwd", "url_private_download": "https://x/y"}]

        paths = app.download_slack_files(files, "sess-E")

        assert len(paths) == 1
        assert os.path.dirname(paths[0]) == os.path.join(self._tmpdir, "sess-E")
        assert os.path.basename(paths[0]) == "passwd"


# --- post_response with files ---


class TestPostResponseWithFiles:
    def setup_method(self):
        self._sessions = app.thread_session_ids.copy()
        app.thread_session_ids.clear()
        self._locks = app.thread_locks.copy()
        app.thread_locks.clear()
        self._tmpdir = tempfile.mkdtemp()
        self._orig_base = app.SLACK_ATTACHMENTS_BASE
        app.SLACK_ATTACHMENTS_BASE = self._tmpdir
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-test"

    def teardown_method(self):
        app.thread_session_ids.clear()
        app.thread_session_ids.update(self._sessions)
        app.thread_locks.clear()
        app.thread_locks.update(self._locks)
        app.SLACK_ATTACHMENTS_BASE = self._orig_base
        import shutil as _sh
        _sh.rmtree(self._tmpdir, ignore_errors=True)

    @patch("app.urllib.request.urlopen")
    @patch("app.run_claude", return_value=app.ClaudeResult(text="reply", cost_usd=0.05, budget_exceeded=False))
    def test_attachments_embedded_in_prompt(self, mock_claude, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_mock(b"hello")
        say = MagicMock()
        client = _make_client()
        files = [{"id": "F1", "name": "a.txt", "url_private_download": "https://x/y"}]

        app.post_response("describe", files, "C1", "ts1", "ev1", say, client)

        prompt_arg = mock_claude.call_args[0][0]
        assert prompt_arg.startswith("describe")
        assert "添付ファイル:" in prompt_arg
        session_id = app.thread_session_ids["ts1"]
        expected = os.path.join(self._tmpdir, session_id, "a.txt")
        assert f"- {expected}" in prompt_arg

    @patch("app.urllib.request.urlopen")
    @patch("app.run_claude", return_value=app.ClaudeResult(text="reply", cost_usd=0.05, budget_exceeded=False))
    def test_attachments_only_no_text(self, mock_claude, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_mock(b"hello")
        say = MagicMock()
        client = _make_client()
        files = [{"id": "F1", "name": "a.txt", "url_private_download": "https://x/y"}]

        app.post_response("", files, "C1", "ts1", "ev1", say, client)

        prompt_arg = mock_claude.call_args[0][0]
        assert prompt_arg.startswith("添付ファイル:\n- ")
