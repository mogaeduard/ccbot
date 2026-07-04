"""Tests for /term — the bound window's pane text as a code block.

Feature 1(b): text-first terminal view. term_command mirrors
screenshot_command's shape (resolve binding -> find window -> capture pane)
but sends the pane text as a fenced code block with a single Refresh button
instead of a PNG. The CB_TERM_REFRESH branch in callback_handler edits that
message in place, mirroring CB_SCREENSHOT_REFRESH's pattern.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import CallbackQuery, User

from ccbot.bot import callback_handler, term_command
from ccbot.handlers.callback_data import CB_TERM_REFRESH
from ccbot.tmux_manager import TmuxWindow


def _make_update(user_id: int = 1) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.type = "private"
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot = AsyncMock()
    return context


class TestTermCommand:
    @pytest.mark.asyncio
    async def test_not_allowed_user_is_noop(self) -> None:
        update = _make_update()
        with patch("ccbot.bot.is_user_allowed", return_value=False):
            await term_command(update, _make_context())
        update.message.reply_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_session_bound(self) -> None:
        update = _make_update()
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
        ):
            mock_sm.resolve_window_for_thread.return_value = None
            await term_command(update, _make_context())

        assert "No session bound" in update.message.reply_text.call_args.args[0]

    @pytest.mark.asyncio
    async def test_window_no_longer_exists(self) -> None:
        update = _make_update()
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
        ):
            mock_sm.resolve_window_for_thread.return_value = "@1"
            mock_sm.get_display_name.return_value = "myproject"
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)
            await term_command(update, _make_context())

        reply_text = update.message.reply_text.call_args.args[0]
        assert "myproject" in reply_text
        assert "no longer exists" in reply_text

    @pytest.mark.asyncio
    async def test_success_sends_code_block_with_refresh_button(self) -> None:
        update = _make_update()
        window = TmuxWindow(window_id="@1", window_name="proj", cwd="/tmp")
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
        ):
            mock_sm.resolve_window_for_thread.return_value = "@1"
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)
            mock_tmux.capture_pane = AsyncMock(return_value="$ echo hi\nhi\n")
            await term_command(update, _make_context())

        call = update.message.reply_text.call_args
        assert "echo hi" in call.args[0]
        keyboard = call.kwargs["reply_markup"]
        buttons = [btn for row in keyboard.inline_keyboard for btn in row]
        assert len(buttons) == 1
        assert buttons[0].callback_data == f"{CB_TERM_REFRESH}@1"

    @pytest.mark.asyncio
    async def test_no_ansi_capture_used(self) -> None:
        """Text-first: /term must use the plain (no-ANSI) capture, not the
        with_ansi=True variant /screenshot uses — ANSI codes would show as
        garbage inside a code block."""
        update = _make_update()
        window = TmuxWindow(window_id="@1", window_name="proj", cwd="/tmp")
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
        ):
            mock_sm.resolve_window_for_thread.return_value = "@1"
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)
            mock_tmux.capture_pane = AsyncMock(return_value="hi")
            await term_command(update, _make_context())

        mock_tmux.capture_pane.assert_awaited_once_with("@1")


def _make_query(data: str) -> MagicMock:
    query = MagicMock(spec=CallbackQuery)
    query.data = data
    query.edit_message_text = AsyncMock()
    query.answer = AsyncMock()
    return query


def _make_user(user_id: int = 1) -> MagicMock:
    user = MagicMock(spec=User)
    user.id = user_id
    return user


def _make_cb_update(query: MagicMock, user: MagicMock) -> MagicMock:
    update = MagicMock()
    update.callback_query = query
    update.effective_user = user
    update.effective_chat = MagicMock()
    update.effective_chat.type = "private"
    return update


class TestTermRefreshCallback:
    @pytest.mark.asyncio
    async def test_refresh_edits_message_in_place(self) -> None:
        query = _make_query(f"{CB_TERM_REFRESH}@1")
        update = _make_cb_update(query, _make_user())
        window = TmuxWindow(window_id="@1", window_name="proj", cwd="/tmp")

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.tmux_manager") as mock_tmux,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)
            mock_tmux.capture_pane = AsyncMock(return_value="new pane text")
            await callback_handler(update, _make_context())

        query.edit_message_text.assert_awaited_once()
        assert "new pane text" in query.edit_message_text.call_args.args[0]
        query.answer.assert_awaited_once_with("Refreshed")

    @pytest.mark.asyncio
    async def test_refresh_window_gone(self) -> None:
        query = _make_query(f"{CB_TERM_REFRESH}@1")
        update = _make_cb_update(query, _make_user())

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.tmux_manager") as mock_tmux,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)
            await callback_handler(update, _make_context())

        query.edit_message_text.assert_not_awaited()
        query.answer.assert_awaited_once_with(
            "Window no longer exists", show_alert=True
        )
