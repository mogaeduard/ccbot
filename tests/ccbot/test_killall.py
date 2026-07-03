"""Tests for /killall — the panic/fresh-start button.

killall_command prompts for confirmation (never kills directly); the
CB_KILLALL_CONFIRM/CANCEL branch in callback_handler does the actual
kill_window calls, gated by a 60s TTL on _pending_killall. Topics are
never touched here — the mirror's next tick deletes them once their
windows are gone (see bot.py's comment in the confirm branch).
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import CallbackQuery, User

from ccbot.bot import (
    _KILLALL_TTL_SECONDS,
    _pending_killall,
    callback_handler,
    killall_command,
)
from ccbot.handlers.callback_data import CB_KILLALL_CANCEL, CB_KILLALL_CONFIRM
from ccbot.tmux_manager import TmuxWindow


@pytest.fixture(autouse=True)
def _clear_pending_killall():
    _pending_killall.clear()
    yield
    _pending_killall.clear()


def _windows(n: int) -> list[TmuxWindow]:
    return [
        TmuxWindow(window_id=f"@{i}", window_name=f"win{i}", cwd="/tmp")
        for i in range(n)
    ]


def _make_message_update(user_id: int = 1, text: str = "/killall") -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot = AsyncMock()
    return context


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


class TestKillallCommand:
    @pytest.mark.asyncio
    async def test_not_allowed_user_is_noop(self) -> None:
        update = _make_message_update()
        with (
            patch("ccbot.bot.is_user_allowed", return_value=False),
            patch("ccbot.bot.tmux_manager") as mock_tmux,
        ):
            await killall_command(update, _make_context())
        mock_tmux.list_windows.assert_not_called()
        update.message.reply_text.assert_not_awaited()
        assert not _pending_killall

    @pytest.mark.asyncio
    async def test_prompts_with_count_and_stores_pending(self) -> None:
        update = _make_message_update()
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.tmux_manager") as mock_tmux,
        ):
            mock_tmux.list_windows = AsyncMock(return_value=_windows(3))
            before = time.monotonic()
            await killall_command(update, _make_context())
            after = time.monotonic()

        reply_text = update.message.reply_text.call_args.args[0]
        assert "Kill all 3 terminals" in reply_text
        assert "⚠" in reply_text  # warning sign
        keyboard = update.message.reply_text.call_args.kwargs["reply_markup"]
        callback_datas = {
            btn.callback_data for row in keyboard.inline_keyboard for btn in row
        }
        assert callback_datas == {CB_KILLALL_CONFIRM, CB_KILLALL_CANCEL}
        assert before <= _pending_killall[1] <= after

    @pytest.mark.asyncio
    async def test_zero_windows_still_prompts(self) -> None:
        update = _make_message_update()
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.tmux_manager") as mock_tmux,
        ):
            mock_tmux.list_windows = AsyncMock(return_value=[])
            await killall_command(update, _make_context())

        reply_text = update.message.reply_text.call_args.args[0]
        assert "Kill all 0 terminals" in reply_text


class TestKillallCallback:
    @pytest.mark.asyncio
    async def test_confirm_kills_every_window_and_edits_message(self) -> None:
        _pending_killall[1] = time.monotonic()
        query = _make_query(CB_KILLALL_CONFIRM)
        user = _make_user()
        update = _make_cb_update(query, user)
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.session_manager"),
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_edit", new_callable=AsyncMock) as mock_edit,
        ):
            mock_tmux.list_windows = AsyncMock(return_value=_windows(3))
            mock_tmux.kill_window = AsyncMock(return_value=True)

            await callback_handler(update, context)

        assert mock_tmux.kill_window.await_count == 3
        mock_tmux.kill_window.assert_any_await("@0")
        mock_tmux.kill_window.assert_any_await("@1")
        mock_tmux.kill_window.assert_any_await("@2")
        mock_edit.assert_awaited_once_with(query, "\U0001f480 killed 3 terminals")
        query.answer.assert_awaited_once_with("Killed")
        assert 1 not in _pending_killall

    @pytest.mark.asyncio
    async def test_cancel_edits_cancelled_and_kills_nothing(self) -> None:
        _pending_killall[1] = time.monotonic()
        query = _make_query(CB_KILLALL_CANCEL)
        user = _make_user()
        update = _make_cb_update(query, user)
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.session_manager"),
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_edit", new_callable=AsyncMock) as mock_edit,
        ):
            await callback_handler(update, context)

        mock_tmux.kill_window.assert_not_called()
        mock_edit.assert_awaited_once_with(query, "cancelled")
        query.answer.assert_awaited_once_with("Cancelled")
        assert 1 not in _pending_killall

    @pytest.mark.asyncio
    async def test_no_pending_prompt_shows_expired(self) -> None:
        """Tapping Confirm with nothing pending (e.g. bot restarted, or the
        prompt was already actioned) must not kill anything."""
        query = _make_query(CB_KILLALL_CONFIRM)
        user = _make_user()
        update = _make_cb_update(query, user)
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.session_manager"),
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_edit", new_callable=AsyncMock) as mock_edit,
        ):
            await callback_handler(update, context)

        mock_tmux.list_windows.assert_not_called()
        mock_tmux.kill_window.assert_not_called()
        mock_edit.assert_awaited_once_with(query, "expired, run /killall again")
        query.answer.assert_awaited_once_with("Expired", show_alert=True)

    @pytest.mark.asyncio
    async def test_aged_past_ttl_shows_expired_and_kills_nothing(self) -> None:
        _pending_killall[1] = time.monotonic() - _KILLALL_TTL_SECONDS - 1
        query = _make_query(CB_KILLALL_CONFIRM)
        user = _make_user()
        update = _make_cb_update(query, user)
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.session_manager"),
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_edit", new_callable=AsyncMock) as mock_edit,
        ):
            await callback_handler(update, context)

        mock_tmux.kill_window.assert_not_called()
        mock_edit.assert_awaited_once_with(query, "expired, run /killall again")
        assert 1 not in _pending_killall

    @pytest.mark.asyncio
    async def test_boundary_just_under_ttl_still_confirms(self) -> None:
        _pending_killall[1] = time.monotonic() - (_KILLALL_TTL_SECONDS - 5)
        query = _make_query(CB_KILLALL_CONFIRM)
        user = _make_user()
        update = _make_cb_update(query, user)
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.session_manager"),
            patch("ccbot.bot.tmux_manager") as mock_tmux,
        ):
            mock_tmux.list_windows = AsyncMock(return_value=_windows(1))
            mock_tmux.kill_window = AsyncMock(return_value=True)

            await callback_handler(update, context)

        mock_tmux.kill_window.assert_awaited_once_with("@0")

    @pytest.mark.asyncio
    async def test_not_allowed_user_cannot_confirm(self) -> None:
        """The global callback_handler allowed-user gate covers CB_KILLALL_*
        too — a disallowed user's tap must not kill anything."""
        _pending_killall[1] = time.monotonic()
        query = _make_query(CB_KILLALL_CONFIRM)
        user = _make_user()
        update = _make_cb_update(query, user)
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=False),
            patch("ccbot.bot.tmux_manager") as mock_tmux,
        ):
            await callback_handler(update, context)

        mock_tmux.kill_window.assert_not_called()
        query.answer.assert_awaited_once_with("Not authorized")
        # Still pending — the gate short-circuited before the branch ran.
        assert 1 in _pending_killall
