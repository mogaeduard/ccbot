"""Tests for the /lock kill switch: lock_command, unlock_command, and
lock_gate_handler (the group=-1 handler that gates every inbound update).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.ext import ApplicationHandlerStop

import ccbot.bot as bot_module
from ccbot.bot import lock_command, lock_gate_handler, unlock_command


@pytest.fixture(autouse=True)
def _reset_lock_reply_rate_limit(monkeypatch: pytest.MonkeyPatch):
    """_last_lock_reply_ts is module-global — reset it so tests don't leak
    rate-limit state into each other."""
    monkeypatch.setattr(bot_module, "_last_lock_reply_ts", 0.0)


def _make_message_update(user_id: int = 1, text: str = "hello") -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.text = text
    update.callback_query = None
    return update


def _make_callback_update(user_id: int = 1) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = None
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot = AsyncMock()
    return context


class TestLockCommand:
    @pytest.mark.asyncio
    async def test_locks_and_replies(self) -> None:
        update = _make_message_update()
        context = _make_context()
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            await lock_command(update, context)

        mock_sm.set_locked.assert_called_once_with(True)
        mock_reply.assert_awaited_once()
        assert "Locked" in mock_reply.call_args.args[1]

    @pytest.mark.asyncio
    async def test_unauthorized_ignored(self) -> None:
        update = _make_message_update()
        context = _make_context()
        with (
            patch("ccbot.bot.is_user_allowed", return_value=False),
            patch("ccbot.bot.session_manager") as mock_sm,
        ):
            await lock_command(update, context)

        mock_sm.set_locked.assert_not_called()


class TestUnlockCommand:
    @pytest.mark.asyncio
    async def test_unlocks_and_replies(self) -> None:
        update = _make_message_update()
        context = _make_context()
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            await unlock_command(update, context)

        mock_sm.set_locked.assert_called_once_with(False)
        mock_reply.assert_awaited_once()
        assert "Unlocked" in mock_reply.call_args.args[1]


class TestLockGateHandler:
    @pytest.mark.asyncio
    async def test_unauthorized_user_passes_through(self) -> None:
        update = _make_message_update(user_id=999)
        context = _make_context()
        with (
            patch("ccbot.bot.is_user_allowed", return_value=False),
            patch("ccbot.bot.session_manager") as mock_sm,
        ):
            mock_sm.is_locked.return_value = True
            # Must NOT raise — an unauthorized sender isn't gated by /lock,
            # their update falls through to the normal per-handler auth check.
            await lock_gate_handler(update, context)

    @pytest.mark.asyncio
    async def test_not_locked_passes_through(self) -> None:
        update = _make_message_update()
        context = _make_context()
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.session_manager") as mock_sm,
        ):
            mock_sm.is_locked.return_value = False
            await lock_gate_handler(update, context)  # must not raise

    @pytest.mark.asyncio
    async def test_unlock_command_passes_through_even_when_locked(self) -> None:
        update = _make_message_update(text="/unlock")
        context = _make_context()
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.session_manager") as mock_sm,
        ):
            mock_sm.is_locked.return_value = True
            await lock_gate_handler(update, context)  # must not raise

    @pytest.mark.asyncio
    async def test_unlock_with_bot_mention_passes_through(self) -> None:
        """/unlock@botname — bot-mention suffix must still be recognized."""
        update = _make_message_update(text="/unlock@mybot")
        context = _make_context()
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.session_manager") as mock_sm,
        ):
            mock_sm.is_locked.return_value = True
            await lock_gate_handler(update, context)  # must not raise

    @pytest.mark.asyncio
    async def test_lock_command_itself_is_gated_while_locked(self) -> None:
        """Every update except /unlock is ignored — including /lock again."""
        update = _make_message_update(text="/lock")
        context = _make_context()
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.is_locked.return_value = True
            with pytest.raises(ApplicationHandlerStop):
                await lock_gate_handler(update, context)

        mock_reply.assert_awaited_once_with(update.message, "🔒 locked")

    @pytest.mark.asyncio
    async def test_plain_text_gated_with_locked_reply(self) -> None:
        update = _make_message_update(text="yes")
        context = _make_context()
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.is_locked.return_value = True
            with pytest.raises(ApplicationHandlerStop):
                await lock_gate_handler(update, context)

        mock_reply.assert_awaited_once_with(update.message, "🔒 locked")

    @pytest.mark.asyncio
    async def test_reply_rate_limited_to_once_per_minute(self) -> None:
        """A compromised account spamming updates gets at most one visible
        '🔒 locked' reply per minute — every update still gets gated."""
        context = _make_context()
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.is_locked.return_value = True

            for _ in range(5):
                update = _make_message_update(text="spam")
                with pytest.raises(ApplicationHandlerStop):
                    await lock_gate_handler(update, context)

        mock_reply.assert_awaited_once()  # only the first of the 5 replied

    @pytest.mark.asyncio
    async def test_callback_query_gated_and_answered(self) -> None:
        update = _make_callback_update()
        context = _make_context()
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.session_manager") as mock_sm,
        ):
            mock_sm.is_locked.return_value = True
            with pytest.raises(ApplicationHandlerStop):
                await lock_gate_handler(update, context)

        update.callback_query.answer.assert_awaited_once_with(
            "🔒 locked", show_alert=True
        )

    @pytest.mark.asyncio
    async def test_callback_query_rate_limited_still_acked(self) -> None:
        """Even when the visible reply is rate-limited, the callback query
        itself is always acked (bare) so its button doesn't spin forever."""
        context = _make_context()
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.session_manager") as mock_sm,
        ):
            mock_sm.is_locked.return_value = True

            first = _make_callback_update()
            with pytest.raises(ApplicationHandlerStop):
                await lock_gate_handler(first, context)
            second = _make_callback_update()
            with pytest.raises(ApplicationHandlerStop):
                await lock_gate_handler(second, context)

        first.callback_query.answer.assert_awaited_once_with(
            "🔒 locked", show_alert=True
        )
        second.callback_query.answer.assert_awaited_once_with(None, show_alert=False)
