"""Tests for dialog_fallback — text code-block fallback for unrecognized dialogs."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

from ccbot.handlers.dialog_fallback import (
    _build_fallback_keyboard,
    clear_fallback_msg,
    get_fallback_msg_id,
    handle_unknown_dialog,
)
from ccbot.handlers.callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_UP,
)

_UNKNOWN_DIALOG_PANE = (
    "┌─ Resume Session ──────────────┐\n"
    "│ 1. feature-branch  2h ago      │\n"
    "└────────────────────────────────┘\n"
)


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 777
    bot.send_message.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_fallback_state():
    from ccbot.handlers.dialog_fallback import _fallback_msgs

    _fallback_msgs.clear()
    yield
    _fallback_msgs.clear()


@pytest.mark.usefixtures("_clear_fallback_state")
class TestHandleUnknownDialog:
    @pytest.mark.asyncio
    async def test_detects_and_sends_text_with_keyboard(
        self, mock_bot: AsyncMock
    ) -> None:
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.dialog_fallback.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.dialog_fallback.session_manager") as mock_sm,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=_UNKNOWN_DIALOG_PANE)
            mock_sm.resolve_chat_id.return_value = 100

            result = await handle_unknown_dialog(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is True
        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == 100
        assert call_kwargs["message_thread_id"] == 42
        assert call_kwargs["reply_markup"] is not None
        assert "```" in call_kwargs["text"]
        assert "Resume Session" in call_kwargs["text"]
        assert get_fallback_msg_id(1, 42) == 777

    @pytest.mark.asyncio
    async def test_no_dialog_returns_false(self, mock_bot: AsyncMock) -> None:
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with patch("ccbot.handlers.dialog_fallback.tmux_manager") as mock_tmux:
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value="$ echo hi\nhi\n$\n")

            result = await handle_unknown_dialog(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is False
        mock_bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_known_ui_defers_returns_false(self, mock_bot: AsyncMock) -> None:
        """A recognized UI (e.g. a permission prompt) must never trigger the
        generic text fallback — is_unrecognized_dialog already skips it,
        this just confirms the handler respects that."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        permission_pane = (
            "  Do you want to proceed?\n  Some permission details\n  Esc to cancel\n"
        )

        with patch("ccbot.handlers.dialog_fallback.tmux_manager") as mock_tmux:
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=permission_pane)

            result = await handle_unknown_dialog(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is False
        mock_bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_window_gone_returns_false(self, mock_bot: AsyncMock) -> None:
        with patch("ccbot.handlers.dialog_fallback.tmux_manager") as mock_tmux:
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)

            result = await handle_unknown_dialog(
                mock_bot, user_id=1, window_id="@5", thread_id=42
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_existing_message_is_edited_in_place(
        self, mock_bot: AsyncMock
    ) -> None:
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.dialog_fallback.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.dialog_fallback.session_manager") as mock_sm,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=_UNKNOWN_DIALOG_PANE)
            mock_sm.resolve_chat_id.return_value = 100

            # First call: sends a new message
            await handle_unknown_dialog(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            # Second call: same dialog still showing — must edit, not re-send
            result = await handle_unknown_dialog(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is True
        mock_bot.send_message.assert_called_once()  # only the first call sent
        mock_bot.edit_message_text.assert_called_once()
        edit_kwargs = mock_bot.edit_message_text.call_args.kwargs
        assert edit_kwargs["message_id"] == 777

    @pytest.mark.asyncio
    async def test_deleted_thread_triggers_cleanup(self, mock_bot: AsyncMock) -> None:
        """send_with_fallback (used for the initial send) centrally detects
        "message thread not found" and hands off to cleanup_deleted_thread —
        this just confirms handle_unknown_dialog goes through that shared
        path instead of its own detection, and reports False (not posted)."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        mock_bot.send_message = AsyncMock(
            side_effect=BadRequest("Bad Request: message thread not found")
        )

        with (
            patch("ccbot.handlers.dialog_fallback.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.dialog_fallback.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_sender.cleanup_deleted_thread",
                new_callable=AsyncMock,
            ) as mock_cleanup,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=_UNKNOWN_DIALOG_PANE)
            mock_sm.resolve_chat_id.return_value = -100999

            result = await handle_unknown_dialog(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is False
        mock_cleanup.assert_awaited_once_with(-100999, 42)


@pytest.mark.usefixtures("_clear_fallback_state")
class TestClearFallbackMsg:
    @pytest.mark.asyncio
    async def test_pops_and_deletes(self, mock_bot: AsyncMock) -> None:
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.dialog_fallback.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.dialog_fallback.session_manager") as mock_sm,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=_UNKNOWN_DIALOG_PANE)
            mock_sm.resolve_chat_id.return_value = 100
            await handle_unknown_dialog(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            await clear_fallback_msg(1, mock_bot, 42)

        assert get_fallback_msg_id(1, 42) is None
        mock_bot.delete_message.assert_called_once_with(chat_id=100, message_id=777)

    @pytest.mark.asyncio
    async def test_noop_when_nothing_tracked(self, mock_bot: AsyncMock) -> None:
        await clear_fallback_msg(1, mock_bot, 42)
        mock_bot.delete_message.assert_not_called()


class TestFallbackKeyboard:
    def test_reuses_cb_ask_prefixes(self) -> None:
        """Spec: nav row reuses the existing CB_ASK_* callbacks."""
        keyboard = _build_fallback_keyboard("@5")
        all_cb_data = [
            btn.callback_data for row in keyboard.inline_keyboard for btn in row
        ]
        for prefix in (
            CB_ASK_UP,
            CB_ASK_DOWN,
            CB_ASK_LEFT,
            CB_ASK_RIGHT,
            CB_ASK_ENTER,
            CB_ASK_ESC,
            CB_ASK_REFRESH,
        ):
            assert any(d and d.startswith(prefix) for d in all_cb_data)
