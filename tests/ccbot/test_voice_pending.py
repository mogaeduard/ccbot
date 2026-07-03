"""Tests for voice-pending TTL/invalidation (bot.py: voice_handler,
CB_VOICE_SEND/CANCEL in callback_handler, text_handler invalidation).

_pending_voice used to store bare text with no timestamp — a Send tapped
hours later would inject a stale transcript into whatever the terminal
happened to be doing by then, and the dict leaked forever. Now each entry
carries (text, monotonic timestamp, prompt message_id), Send checks age
against a 5-minute TTL, and a new voice/text message in the same topic
invalidates any earlier un-actioned entry.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import CallbackQuery, User

import ccbot.bot as bot_module
from ccbot.bot import (
    _PENDING_VOICE_MAX_ENTRIES,
    _PENDING_VOICE_TTL_SECONDS,
    _pending_voice,
    _PendingVoice,
    _expire_pending_voice,
    _set_pending_voice,
    callback_handler,
    voice_handler,
)
from ccbot.handlers.callback_data import CB_VOICE_SEND


@pytest.fixture(autouse=True)
def _clear_pending_voice():
    _pending_voice.clear()
    yield
    _pending_voice.clear()


def _make_voice_update(user_id: int = 1, thread_id: int = 42) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.message_thread_id = thread_id
    update.message.voice = MagicMock()
    voice_file = AsyncMock()
    voice_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"ogg-bytes"))
    update.message.voice.get_file = AsyncMock(return_value=voice_file)
    update.message.chat = MagicMock()
    update.message.chat.type = "private"
    sent_msg = MagicMock()
    sent_msg.message_id = 555
    update.message.reply_text = AsyncMock(return_value=sent_msg)
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


class TestVoiceHandlerSetsPending:
    @pytest.mark.asyncio
    async def test_stores_text_ts_and_message_id(self) -> None:
        update = _make_voice_update()
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch.object(bot_module.config, "openai_api_key", "sk-test"),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch(
                "ccbot.bot.transcribe_voice",
                new_callable=AsyncMock,
                return_value="hello claude",
            ),
        ):
            mock_sm.get_window_for_thread.return_value = "@1"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())

            before = time.monotonic()
            await voice_handler(update, context)
            after = time.monotonic()

        entry = _pending_voice[(1, 42)]
        assert entry.text == "hello claude"
        assert entry.message_id == 555
        assert before <= entry.ts <= after

    @pytest.mark.asyncio
    async def test_new_voice_note_invalidates_previous_pending(self) -> None:
        """A second voice note in the same topic edits the first prompt to
        expired instead of leaving two live Send/Cancel prompts."""
        _pending_voice[(1, 42)] = _PendingVoice(
            text="old transcript", ts=time.monotonic(), message_id=111
        )
        update = _make_voice_update()
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch.object(bot_module.config, "openai_api_key", "sk-test"),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch(
                "ccbot.bot.transcribe_voice",
                new_callable=AsyncMock,
                return_value="new transcript",
            ),
        ):
            mock_sm.get_window_for_thread.return_value = "@1"
            mock_sm.resolve_chat_id.return_value = 100
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())

            await voice_handler(update, context)

        context.bot.edit_message_text.assert_awaited_once_with(
            chat_id=100,
            message_id=111,
            text="🎤 expired — resend the voice note",
        )
        # The new entry replaced the old one
        assert _pending_voice[(1, 42)].text == "new transcript"
        assert _pending_voice[(1, 42)].message_id == 555


class TestExpirePendingVoice:
    @pytest.mark.asyncio
    async def test_noop_when_nothing_pending(self) -> None:
        bot = AsyncMock()
        await _expire_pending_voice(bot, 1, 42)  # must not raise
        bot.edit_message_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_edit_failure_is_swallowed(self) -> None:
        """Message already deleted/too old — entry still gets dropped."""
        _pending_voice[(1, 42)] = _PendingVoice(
            text="x", ts=time.monotonic(), message_id=111
        )
        bot = AsyncMock()
        bot.edit_message_text = AsyncMock(side_effect=Exception("gone"))

        with patch("ccbot.bot.session_manager") as mock_sm:
            mock_sm.resolve_chat_id.return_value = 100
            await _expire_pending_voice(bot, 1, 42)

        assert (1, 42) not in _pending_voice


class TestSetPendingVoiceCap:
    def test_cap_evicts_oldest(self) -> None:
        for i in range(_PENDING_VOICE_MAX_ENTRIES):
            _set_pending_voice(1, i, f"text {i}", i)
            time.sleep(0)  # keep monotonic() strictly increasing-ish
        assert len(_pending_voice) == _PENDING_VOICE_MAX_ENTRIES

        _set_pending_voice(1, 9999, "one more", 9999)

        assert len(_pending_voice) == _PENDING_VOICE_MAX_ENTRIES
        assert (1, 0) not in _pending_voice  # oldest evicted
        assert (1, 9999) in _pending_voice


def _make_query() -> MagicMock:
    query = MagicMock(spec=CallbackQuery)
    query.data = CB_VOICE_SEND
    query.edit_message_text = AsyncMock()
    query.answer = AsyncMock()
    return query


def _make_user(user_id: int = 1) -> MagicMock:
    user = MagicMock(spec=User)
    user.id = user_id
    return user


def _make_cb_update(query: MagicMock, user: MagicMock, thread_id: int) -> MagicMock:
    update = MagicMock()
    update.callback_query = query
    update.effective_user = user
    update.effective_chat = MagicMock()
    update.effective_chat.type = "private"
    return update


class TestVoiceSendTTL:
    @pytest.mark.asyncio
    async def test_fresh_entry_injects(self) -> None:
        _pending_voice[(1, 42)] = _PendingVoice(
            text="hello", ts=time.monotonic(), message_id=555
        )
        query = _make_query()
        user = _make_user()
        update = _make_cb_update(query, user, 42)
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
        ):
            mock_sm.get_window_for_thread.return_value = "@1"
            mock_sm.send_to_window = AsyncMock(return_value=(True, "Sent"))

            await callback_handler(update, context)

        mock_sm.send_to_window.assert_awaited_once_with("@1", "hello")
        assert (1, 42) not in _pending_voice

    @pytest.mark.asyncio
    async def test_aged_entry_refuses_and_expires(self) -> None:
        stale_ts = time.monotonic() - _PENDING_VOICE_TTL_SECONDS - 1
        _pending_voice[(1, 42)] = _PendingVoice(
            text="hello", ts=stale_ts, message_id=555
        )
        query = _make_query()
        user = _make_user()
        update = _make_cb_update(query, user, 42)
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.safe_edit", new_callable=AsyncMock) as mock_edit,
        ):
            await callback_handler(update, context)

        mock_sm.send_to_window.assert_not_called()
        mock_edit.assert_awaited_once_with(query, "🎤 expired — resend the voice note")
        query.answer.assert_awaited_once_with("Expired", show_alert=True)

    @pytest.mark.asyncio
    async def test_boundary_just_under_ttl_still_sends(self) -> None:
        ts = time.monotonic() - (_PENDING_VOICE_TTL_SECONDS - 5)
        _pending_voice[(1, 42)] = _PendingVoice(text="hi", ts=ts, message_id=555)
        query = _make_query()
        user = _make_user()
        update = _make_cb_update(query, user, 42)
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
        ):
            mock_sm.get_window_for_thread.return_value = "@1"
            mock_sm.send_to_window = AsyncMock(return_value=(True, "Sent"))

            await callback_handler(update, context)

        mock_sm.send_to_window.assert_awaited_once_with("@1", "hi")


class TestTextHandlerInvalidatesPendingVoice:
    @pytest.mark.asyncio
    async def test_new_text_message_expires_pending_voice(self) -> None:
        _pending_voice[(1, 42)] = _PendingVoice(
            text="old", ts=time.monotonic(), message_id=111
        )
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 1
        update.message = MagicMock()
        update.message.text = "typed directly"
        update.message.chat = MagicMock()
        update.message.chat.type = "private"
        update.message.chat.send_action = AsyncMock()
        context = MagicMock()
        context.bot = AsyncMock()
        context.user_data = {}

        window = MagicMock()
        window.window_id = "@1"

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.enqueue_status_update", new_callable=AsyncMock),
            patch(
                "ccbot.bot.is_interactive_ui",
                return_value=False,
            ),
        ):
            mock_sm.get_window_for_thread.return_value = "@1"
            mock_sm.resolve_chat_id.return_value = 100
            mock_sm.get_window_state.return_value = MagicMock(session_id="sess-1")
            mock_sm.send_to_window = AsyncMock(return_value=(True, "Sent"))
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)
            mock_tmux.capture_pane = AsyncMock(return_value="")

            from ccbot.bot import text_handler

            await text_handler(update, context)

        context.bot.edit_message_text.assert_awaited_once_with(
            chat_id=100,
            message_id=111,
            text="🎤 expired — resend the voice note",
        )
        assert (1, 42) not in _pending_voice
