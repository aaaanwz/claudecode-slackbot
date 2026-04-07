import subprocess
import threading
from unittest.mock import MagicMock, patch

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


# --- run_claude ---


class TestRunClaude:
    @patch("app.subprocess.run")
    def test_new_session(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="response text", stderr=""
        )
        result = app.run_claude("hello", "session-123")
        assert result == "response text"

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "claude"
        assert "--session-id" in cmd
        assert "session-123" in cmd

    @patch("app.subprocess.run")
    def test_resume_session(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="resumed response", stderr=""
        )
        result = app.run_claude("follow up", "existing-session-id", resume=True)
        assert result == "resumed response"

        cmd = mock_run.call_args[0][0]
        assert "--resume" in cmd
        assert "existing-session-id" in cmd

    @patch("app.subprocess.run")
    def test_resume_failure_raises_session_resume_error(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="session not found"
        )
        with pytest.raises(app.SessionResumeError):
            app.run_claude("hello", "bad-session", resume=True)

    @patch("app.subprocess.run")
    def test_new_session_failure_raises_runtime_error(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="some error"
        )
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

    @patch("app.run_claude", return_value="bot reply")
    def test_resume_response(self, mock_claude):
        say = MagicMock()
        client = _make_client()
        app.thread_session_ids["ts1"] = "session1"

        app.post_response("hello", "C1", "ts1", "ev1", say, client)

        mock_claude.assert_called_once_with("hello", "session1", resume=True)
        client.reactions_add.assert_called_once()
        client.reactions_remove.assert_called_once()
        say.assert_called_once()
        assert say.call_args[1]["text"] == "bot reply"
        # 応答にメタデータが付与される
        assert say.call_args[1]["metadata"]["event_type"] == "claude_session"
        assert say.call_args[1]["metadata"]["event_payload"]["session_id"] == "session1"

    @patch("app.run_claude", return_value="new reply")
    def test_new_session_response(self, mock_claude):
        say = MagicMock()
        client = _make_client()

        app.post_response("hello", "C1", "ts_new", "ev1", say, client)

        say.assert_called_once()
        assert say.call_args[1]["text"] == "new reply"
        # 応答にメタデータが付与される
        assert say.call_args[1]["metadata"]["event_type"] == "claude_session"

    @patch("app.run_claude", return_value="resumed reply")
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

        app.post_response("hello", "C1", "ts_recover", "ev1", say, client)

        assert app.thread_session_ids["ts_recover"] == "recovered-id"
        mock_claude.assert_called_once_with("hello", "recovered-id", resume=True)

    @patch("app.run_claude")
    def test_resume_failure_starts_new_session(self, mock_claude):
        mock_claude.side_effect = [
            app.SessionResumeError("fail"),
            "new reply",
        ]
        say = MagicMock()
        client = _make_client()
        app.thread_session_ids["ts1"] = "session1"

        app.post_response("hello", "C1", "ts1", "ev1", say, client)

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

        app.post_response("hello", "C1", "ts1", "ev1", say, client)

        say.assert_called_once()
        assert "タイムアウト" in say.call_args[1]["text"]
        client.reactions_remove.assert_called_once()

    @patch("app.run_claude", side_effect=RuntimeError("unexpected"))
    def test_generic_error(self, mock_claude):
        say = MagicMock()
        client = _make_client()
        app.thread_session_ids["ts1"] = "session1"

        app.post_response("hello", "C1", "ts1", "ev1", say, client)

        say.assert_called_once()
        assert "エラー" in say.call_args[1]["text"]
        client.reactions_remove.assert_called_once()

    @patch("app.run_claude", return_value="")
    def test_empty_response_fallback(self, mock_claude):
        say = MagicMock()
        client = _make_client()
        app.thread_session_ids["ts1"] = "session1"

        app.post_response("hello", "C1", "ts1", "ev1", say, client)

        assert "応答を生成できませんでした" in say.call_args[1]["text"]

    @patch("app.run_claude", return_value="a" * 20000)
    def test_long_response_split(self, mock_claude):
        say = MagicMock()
        client = _make_client()
        app.thread_session_ids["ts1"] = "session1"

        app.post_response("hello", "C1", "ts1", "ev1", say, client)

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

    def test_ignores_subtype_message(self):
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
