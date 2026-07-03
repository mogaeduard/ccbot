"""Tests for message_sender's deleted-topic detection (change 2).

Telegram sends no event when a forum topic is deleted, so ccbot detects it
from send failures instead: a BadRequest("message thread not found") on any
send-into-a-thread call. Covers the predicate, the cleanup routine itself
(kills the bound window, unbinds, no Telegram calls — so it can't loop), and
that every send helper wires the detection in instead of blindly retrying.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest, RetryAfter

import ccbot.session as session_module
import ccbot.tmux_manager as tmux_module
from ccbot.handlers import message_sender
from ccbot.handlers.message_sender import (
    cleanup_deleted_thread,
    is_thread_deleted_error,
    safe_reply,
    safe_send,
    send_photo,
    send_with_fallback,
)
from ccbot.session import SessionManager


class TestIsThreadDeletedError:
    def test_matches_telegram_thread_not_found(self) -> None:
        # python-telegram-bot strips "Bad Request: " and capitalizes, so the
        # exception's str() is "Message thread not found" — see
        # telegram.error.TelegramError.__init__.
        assert is_thread_deleted_error(BadRequest("Message thread not found"))

    def test_matches_raw_api_description(self) -> None:
        assert is_thread_deleted_error(
            BadRequest("Bad Request: message thread not found")
        )

    def test_other_bad_request_does_not_match(self) -> None:
        assert not is_thread_deleted_error(BadRequest("Message is not modified"))

    def test_non_bad_request_does_not_match(self) -> None:
        assert not is_thread_deleted_error(RetryAfter(5))
        assert not is_thread_deleted_error(ValueError("message thread not found"))


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    """Fresh SessionManager patched onto ccbot.session — cleanup_deleted_thread
    imports session_manager locally (to avoid a circular import), re-resolving
    ccbot.session.session_manager on every call, so patching it here is what
    the function actually observes."""
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    m = SessionManager()
    monkeypatch.setattr(session_module, "session_manager", m)
    return m


class TestCleanupDeletedThread:
    @pytest.mark.asyncio
    async def test_kills_window_and_unbinds(
        self, monkeypatch, mgr: SessionManager
    ) -> None:
        mgr.set_group_chat_id(1, 42, -100999)
        mgr.bind_thread(1, 42, "@0", window_name="proj")
        mock_window = MagicMock()
        mock_window.window_id = "@0"

        with (
            patch.object(tmux_module, "tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.cleanup.clear_topic_state", new_callable=AsyncMock
            ) as mock_cleanup,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.kill_window = AsyncMock(return_value=True)

            await cleanup_deleted_thread(-100999, 42)

            mock_tmux.kill_window.assert_awaited_once_with("@0")
            mock_cleanup.assert_awaited_once_with(1, 42)
        assert mgr.get_window_for_thread(1, 42) is None

    @pytest.mark.asyncio
    async def test_no_telegram_calls_made(
        self, monkeypatch, mgr: SessionManager
    ) -> None:
        """The cleanup itself must never touch Telegram — that's what makes
        a delete-triggered kill unable to re-trigger a send into the dead
        thread (the loop-guard requirement)."""
        mgr.set_group_chat_id(1, 42, -100999)
        mgr.bind_thread(1, 42, "@0", window_name="proj")

        with (
            patch.object(tmux_module, "tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.cleanup.clear_topic_state", new_callable=AsyncMock
            ) as mock_cleanup,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)

            await cleanup_deleted_thread(-100999, 42)

            mock_tmux.kill_window.assert_not_called()
            # clear_topic_state called with bot=None: no delete_message calls
            mock_cleanup.assert_awaited_once_with(1, 42)

    @pytest.mark.asyncio
    async def test_ignores_binding_in_a_different_chat(
        self, monkeypatch, mgr: SessionManager
    ) -> None:
        mgr.set_group_chat_id(1, 42, -100999)
        mgr.bind_thread(1, 42, "@0", window_name="proj")

        with patch.object(tmux_module, "tmux_manager") as mock_tmux:
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)

            await cleanup_deleted_thread(-100111, 42)  # different chat_id

        assert mgr.get_window_for_thread(1, 42) == "@0"  # untouched

    @pytest.mark.asyncio
    async def test_idempotent_when_already_cleaned_up(
        self, monkeypatch, mgr: SessionManager
    ) -> None:
        """A second call for a thread that's already unbound (e.g. two queued
        tasks both failing) is a harmless no-op, not a crash."""
        with patch.object(tmux_module, "tmux_manager") as mock_tmux:
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)
            await cleanup_deleted_thread(-100999, 42)  # nothing bound at all
        mock_tmux.kill_window.assert_not_called()


class TestSendHelpersDetectDeletedThread:
    """Every send-path helper must detect the deleted-thread error and hand
    off to cleanup_deleted_thread instead of retrying with plain text."""

    @pytest.mark.asyncio
    async def test_send_with_fallback_skips_retry_and_cleans_up(self) -> None:
        bot = AsyncMock()
        bot.send_message = AsyncMock(
            side_effect=BadRequest("Bad Request: message thread not found")
        )
        with patch.object(
            message_sender, "cleanup_deleted_thread", new_callable=AsyncMock
        ) as mock_cleanup:
            result = await send_with_fallback(
                bot, -100999, "hello", message_thread_id=42
            )

        assert result is None
        bot.send_message.assert_awaited_once()  # no plain-text retry attempt
        mock_cleanup.assert_awaited_once_with(-100999, 42)

    @pytest.mark.asyncio
    async def test_send_with_fallback_other_error_still_retries(self) -> None:
        bot = AsyncMock()
        bot.send_message = AsyncMock(side_effect=ValueError("boom"))
        with patch.object(
            message_sender, "cleanup_deleted_thread", new_callable=AsyncMock
        ) as mock_cleanup:
            await send_with_fallback(bot, -100999, "hello", message_thread_id=42)

        assert bot.send_message.await_count == 2  # formatted + plain-text retry
        mock_cleanup.assert_not_called()

    @pytest.mark.asyncio
    async def test_safe_reply_cleans_up_and_still_raises(self) -> None:
        message = MagicMock()
        message.chat_id = -100999
        message.message_thread_id = 42
        message.reply_text = AsyncMock(
            side_effect=BadRequest("Bad Request: message thread not found")
        )
        with patch.object(
            message_sender, "cleanup_deleted_thread", new_callable=AsyncMock
        ) as mock_cleanup:
            with pytest.raises(BadRequest):
                await safe_reply(message, "hello")

        message.reply_text.assert_awaited_once()
        mock_cleanup.assert_awaited_once_with(-100999, 42)

    @pytest.mark.asyncio
    async def test_safe_send_cleans_up_without_retry(self) -> None:
        bot = AsyncMock()
        bot.send_message = AsyncMock(
            side_effect=BadRequest("Bad Request: message thread not found")
        )
        with patch.object(
            message_sender, "cleanup_deleted_thread", new_callable=AsyncMock
        ) as mock_cleanup:
            await safe_send(bot, -100999, "hello", message_thread_id=42)

        bot.send_message.assert_awaited_once()
        mock_cleanup.assert_awaited_once_with(-100999, 42)

    @pytest.mark.asyncio
    async def test_send_photo_cleans_up(self) -> None:
        bot = AsyncMock()
        bot.send_photo = AsyncMock(
            side_effect=BadRequest("Bad Request: message thread not found")
        )
        with patch.object(
            message_sender, "cleanup_deleted_thread", new_callable=AsyncMock
        ) as mock_cleanup:
            await send_photo(
                bot, -100999, [("image/png", b"abc")], message_thread_id=42
            )

        mock_cleanup.assert_awaited_once_with(-100999, 42)

    @pytest.mark.asyncio
    async def test_no_thread_id_never_triggers_cleanup(self) -> None:
        """A private-chat send (no thread) can't be a deleted-topic send —
        must fall through to the normal error path, not crash on thread_id=None."""
        bot = AsyncMock()
        bot.send_message = AsyncMock(
            side_effect=BadRequest("Bad Request: message thread not found")
        )
        with patch.object(
            message_sender, "cleanup_deleted_thread", new_callable=AsyncMock
        ) as mock_cleanup:
            result = await send_with_fallback(bot, 12345, "hello")

        assert result is None
        mock_cleanup.assert_not_called()
