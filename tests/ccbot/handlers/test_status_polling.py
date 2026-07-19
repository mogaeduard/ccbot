"""Tests for status_polling — Settings UI detection via the poller path.

Simulates the user workflow: /model is sent to Claude Code, the Settings
model picker renders in the terminal, and the status poller detects it
on its next 1s tick.
"""

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.config import config
from ccbot.handlers import status_polling
from ccbot.handlers.status_polling import update_status_message
from ccbot.session import SessionManager


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 999
    bot.send_message.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_interactive_state():
    """Ensure interactive state is clean before and after each test."""
    from ccbot.handlers.interactive_ui import _interactive_mode, _interactive_msgs

    _interactive_mode.clear()
    _interactive_msgs.clear()
    yield
    _interactive_mode.clear()
    _interactive_msgs.clear()


@pytest.mark.usefixtures("_clear_interactive_state")
class TestStatusPollerSettingsDetection:
    """Simulate the status poller detecting a Settings UI in the terminal.

    This is the actual code path for /model: no JSONL tool_use entry exists,
    so the status poller (update_status_message) is the only detector.
    """

    @pytest.mark.asyncio
    async def test_settings_ui_detected_and_keyboard_sent(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Poller captures Settings pane → handle_interactive_ui sends keyboard."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_handle_ui.return_value = True

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_handle_ui.assert_called_once_with(mock_bot, 1, window_id, 42)

    @pytest.mark.asyncio
    async def test_normal_pane_no_interactive_ui(self, mock_bot: AsyncMock):
        """Normal pane text → no handle_interactive_ui call, just status check."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        normal_pane = (
            "some output\n"
            "✻ Reading file\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  [Opus 4.6] Context: 50%\n"
        )

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
            patch(
                "ccbot.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=normal_pane)

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_handle_ui.assert_not_called()

    @pytest.mark.asyncio
    async def test_settings_ui_end_to_end_sends_telegram_keyboard(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Full end-to-end: poller → is_interactive_ui → handle_interactive_ui
        → bot.send_message with keyboard.

        Uses real handle_interactive_ui (not mocked) to verify the full path.
        """
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux_poll,
            patch("ccbot.handlers.interactive_ui.tmux_manager") as mock_tmux_ui,
            patch("ccbot.handlers.interactive_ui.session_manager") as mock_sm,
        ):
            mock_tmux_poll.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux_poll.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_tmux_ui.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux_ui.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_sm.resolve_chat_id.return_value = 100

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            # Verify bot.send_message was called with keyboard
            mock_bot.send_message.assert_called_once()
            call_kwargs = mock_bot.send_message.call_args.kwargs
            assert call_kwargs["chat_id"] == 100
            assert call_kwargs["message_thread_id"] == 42
            keyboard = call_kwargs["reply_markup"]
            assert keyboard is not None
            # Verify the message text contains model picker content
            assert "Select model" in call_kwargs["text"]


@pytest.fixture
def mock_bot_document():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 888
    bot.send_message.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_fallback_state():
    from ccbot.handlers.dialog_fallback import _fallback_msgs

    _fallback_msgs.clear()
    yield
    _fallback_msgs.clear()


@pytest.mark.usefixtures("_clear_interactive_state", "_clear_fallback_state")
class TestStatusPollerUnknownDialogFallback:
    """The poller's other detector: a full-screen dialog with no specific
    parser (claude --resume picker, /login, ...) gets posted as a text code
    block instead of silently going unnoticed."""

    _DIALOG_PANE = (
        "┌─ Resume Session ──────────────┐\n"
        "│ 1. feature-branch  2h ago      │\n"
        "└────────────────────────────────┘\n"
    )

    @pytest.mark.asyncio
    async def test_unknown_dialog_detected_and_text_sent(
        self, mock_bot_document: AsyncMock
    ):
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.dialog_fallback.tmux_manager") as mock_tmux_fb,
            patch("ccbot.handlers.dialog_fallback.session_manager") as mock_sm,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=self._DIALOG_PANE)
            mock_tmux_fb.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux_fb.capture_pane = AsyncMock(return_value=self._DIALOG_PANE)
            mock_sm.resolve_chat_id.return_value = 100

            await update_status_message(
                mock_bot_document, user_id=1, window_id=window_id, thread_id=42
            )

            mock_bot_document.send_message.assert_called_once()
            call_kwargs = mock_bot_document.send_message.call_args.kwargs
            assert call_kwargs["chat_id"] == 100
            assert call_kwargs["message_thread_id"] == 42
            assert call_kwargs["reply_markup"] is not None
            assert "Resume Session" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_dialog_gone_clears_tracked_screenshot(
        self, mock_bot_document: AsyncMock
    ):
        """Once the dialog disappears, the tracked screenshot message is
        cleared on the next poll tick (edit-in-place / clear pattern, same
        as the known-UI path)."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.dialog_fallback.tmux_manager") as mock_tmux_fb,
            patch("ccbot.handlers.dialog_fallback.session_manager") as mock_sm,
        ):
            mock_tmux_fb.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_sm.resolve_chat_id.return_value = 100

            # Tick 1: dialog present
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=self._DIALOG_PANE)
            mock_tmux_fb.capture_pane = AsyncMock(return_value=self._DIALOG_PANE)
            await update_status_message(
                mock_bot_document, user_id=1, window_id=window_id, thread_id=42
            )
            assert mock_bot_document.send_message.call_count == 1

            # Tick 2: dialog gone — normal idle pane
            idle_pane = (
                "some output\n"
                "──────────────────────────────────────\n"
                "❯ \n"
                "──────────────────────────────────────\n"
                "  [Opus 4.6] Context: 50%\n"
            )
            mock_tmux.capture_pane = AsyncMock(return_value=idle_pane)
            mock_tmux_fb.capture_pane = AsyncMock(return_value=idle_pane)
            await update_status_message(
                mock_bot_document, user_id=1, window_id=window_id, thread_id=42
            )

            mock_bot_document.delete_message.assert_called_once_with(
                chat_id=100, message_id=888
            )

    @pytest.mark.asyncio
    async def test_known_ui_takes_priority_over_fallback(
        self, mock_bot_document: AsyncMock
    ):
        """A recognized UI (Settings, permission prompt, ...) must never
        also trigger the screenshot fallback in the same poll tick."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        permission_pane = (
            "  Do you want to proceed?\n  Some permission details\n  Esc to cancel\n"
        )

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "ccbot.handlers.status_polling.handle_unknown_dialog",
                new_callable=AsyncMock,
            ) as mock_fallback,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=permission_pane)

            await update_status_message(
                mock_bot_document, user_id=1, window_id=window_id, thread_id=42
            )

            mock_fallback.assert_not_called()


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    """Fresh SessionManager swapped into status_polling's module namespace,
    mirroring the pattern in test_mirror.py — real bind/unbind behavior
    without touching real state.json."""
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    m = SessionManager()
    monkeypatch.setattr(status_polling, "session_manager", m)
    return m


async def _run_one_tick(bot: AsyncMock) -> None:
    """Run status_poll_loop just long enough for one full pass, then stop it."""
    task = asyncio.create_task(status_polling.status_poll_loop(bot))
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


class TestStatusPollLoopDeadWindowCleanup:
    """Change 1: this loop's own dead-window cleanup must step aside when the
    auto-topic mirror owns the job, so the two never race to unbind/clean up
    the same thread (mirror also deletes the forum topic; this fallback path
    can't)."""

    @pytest.mark.asyncio
    async def test_mirror_enabled_does_not_unbind(
        self, monkeypatch, mgr: SessionManager, mock_bot: AsyncMock
    ) -> None:
        monkeypatch.setattr(config, "mirror_chat_id", -100999)
        mgr.bind_thread(1, 42, "@0", window_name="proj")

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.clear_topic_state",
                new_callable=AsyncMock,
            ) as mock_cleanup,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)
            await _run_one_tick(mock_bot)

            mock_cleanup.assert_not_called()
            assert mgr.get_window_for_thread(1, 42) == "@0"

    @pytest.mark.asyncio
    async def test_mirror_disabled_still_unbinds(
        self, monkeypatch, mgr: SessionManager, mock_bot: AsyncMock
    ) -> None:
        monkeypatch.setattr(config, "mirror_chat_id", None)
        mgr.bind_thread(1, 42, "@0", window_name="proj")

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.clear_topic_state",
                new_callable=AsyncMock,
            ) as mock_cleanup,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)
            await _run_one_tick(mock_bot)

            mock_cleanup.assert_awaited_once_with(1, 42, mock_bot)
            assert mgr.get_window_for_thread(1, 42) is None

    @pytest.mark.asyncio
    async def test_mirror_disabled_parked_binding_preserved(
        self, monkeypatch, mgr: SessionManager, mock_bot: AsyncMock
    ) -> None:
        """A parked binding (topic-name value, see session.resolve_stale_ids)
        is deliberately preserved for adoption — the fallback cleanup must
        never mistake it for a stale binding and tear it down."""
        monkeypatch.setattr(config, "mirror_chat_id", None)
        mgr.bind_thread(1, 42, "2 — proj", window_name="")

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.clear_topic_state",
                new_callable=AsyncMock,
            ) as mock_cleanup,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)
            await _run_one_tick(mock_bot)

            mock_cleanup.assert_not_called()
            assert mgr.get_window_for_thread(1, 42) == "2 — proj"


@pytest.fixture
def _clear_probe_state():
    """(user_id, thread_id) probe/typing timestamps and topic_titles' name
    cache are module-level state — reset around each test so cadence/name
    tests don't see leftovers from other tests."""
    status_polling._last_probed.clear()
    status_polling._last_typing_sent.clear()
    status_polling.topic_titles._last_set_name.clear()
    yield
    status_polling._last_probed.clear()
    status_polling._last_typing_sent.clear()
    status_polling.topic_titles._last_set_name.clear()


@pytest.mark.usefixtures("_clear_probe_state")
class TestActiveDeletionProbe:
    """An idle topic's deletion is never noticed by the send-failure path
    (nothing is ever sent into it), so a round-robin probe must catch it
    independently — see status_polling.py's module docstring and
    _probe_topic/_due_for_probe.

    The probe uses editForumTopic: it's the only Bot API call that
    validates thread liveness (send_chat_action and
    unpinAllForumTopicMessages both return ok=True on deleted/invalid
    thread ids — verified live against the real Telegram API)."""

    @pytest.mark.asyncio
    async def test_probe_topic_id_invalid_cleans_up(
        self, monkeypatch, mgr: SessionManager, mock_bot: AsyncMock
    ) -> None:
        from telegram.error import BadRequest

        mgr.bind_thread(1, 42, "@0", window_name="proj")
        mock_bot.edit_forum_topic = AsyncMock(
            side_effect=BadRequest("Bad Request: TOPIC_ID_INVALID")
        )

        with patch(
            "ccbot.handlers.status_polling.cleanup_deleted_thread",
            new_callable=AsyncMock,
        ) as mock_cleanup:
            await status_polling._probe_topic(mock_bot, 1, 42, "@0")

            # chat_id resolves to the user_id fallback (no group chat set).
            mock_cleanup.assert_awaited_once_with(1, 42)
        mock_bot.edit_forum_topic.assert_awaited_once_with(
            chat_id=1, message_thread_id=42, name="proj"
        )

    @pytest.mark.asyncio
    async def test_healthy_binding_untouched(
        self, monkeypatch, mgr: SessionManager, mock_bot: AsyncMock
    ) -> None:
        mgr.bind_thread(1, 42, "@0", window_name="proj")

        with patch(
            "ccbot.handlers.status_polling.cleanup_deleted_thread",
            new_callable=AsyncMock,
        ) as mock_cleanup:
            await status_polling._probe_topic(mock_bot, 1, 42, "@0")

            mock_cleanup.assert_not_called()
        mock_bot.edit_forum_topic.assert_awaited_once_with(
            chat_id=1, message_thread_id=42, name="proj"
        )
        assert mgr.get_window_for_thread(1, 42) == "@0"

    @pytest.mark.asyncio
    async def test_probe_prefers_ai_title_over_display_name(
        self, monkeypatch, mgr: SessionManager, mock_bot: AsyncMock
    ) -> None:
        """topic_titles._last_set_name (an ai-title rename) takes priority
        over the plain mirror-created display name."""
        mgr.bind_thread(1, 42, "@0", window_name="1 — proj")
        status_polling.topic_titles._last_set_name["@0"] = "1 — Fix login bug"

        await status_polling._probe_topic(mock_bot, 1, 42, "@0")

        mock_bot.edit_forum_topic.assert_awaited_once_with(
            chat_id=1, message_thread_id=42, name="1 — Fix login bug"
        )

    @pytest.mark.asyncio
    async def test_other_bad_request_does_not_clean_up(
        self, monkeypatch, mgr: SessionManager, mock_bot: AsyncMock
    ) -> None:
        """A BadRequest unrelated to thread deletion must not trigger cleanup."""
        from telegram.error import BadRequest

        mgr.bind_thread(1, 42, "@0", window_name="proj")
        mock_bot.edit_forum_topic = AsyncMock(
            side_effect=BadRequest("Bad Request: TOPIC_NAME_INVALID")
        )

        with patch(
            "ccbot.handlers.status_polling.cleanup_deleted_thread",
            new_callable=AsyncMock,
        ) as mock_cleanup:
            await status_polling._probe_topic(mock_bot, 1, 42, "@0")

            mock_cleanup.assert_not_called()
        assert mgr.get_window_for_thread(1, 42) == "@0"

    def test_rate_budget_caps_bindings_per_tick(self, mgr: SessionManager) -> None:
        """More bindings than PROBE_BATCH_SIZE are due at once — only a
        capped batch goes out this tick; the rest wait for the next one."""
        for i in range(status_polling.PROBE_BATCH_SIZE + 5):
            mgr.bind_thread(1, i, f"@{i}", window_name="proj")

        due = status_polling._due_for_probe(now=10_000.0)

        assert len(due) == status_polling.PROBE_BATCH_SIZE

    def test_due_selection_is_oldest_probed_first(self, mgr: SessionManager) -> None:
        mgr.bind_thread(1, 1, "@1", window_name="a")
        mgr.bind_thread(1, 2, "@2", window_name="b")
        mgr.bind_thread(1, 3, "@3", window_name="c")
        now = 10_000.0
        # thread 1: never probed (oldest, 0.0). thread 3: probed a while ago.
        # thread 2: probed more recently than 3, but both are still overdue.
        status_polling._last_probed[(1, 1)] = 0.0
        status_polling._last_probed[(1, 3)] = now - status_polling.PROBE_INTERVAL - 50
        status_polling._last_probed[(1, 2)] = now - status_polling.PROBE_INTERVAL - 1

        due = status_polling._due_for_probe(now)

        assert [t[1] for t in due] == [1, 3, 2]

    def test_typing_action_does_not_count_as_probed(self, mgr: SessionManager) -> None:
        """Unlike the old design, a recent typing action must NOT exempt a
        binding from probing — send_chat_action proves nothing about topic
        liveness, so the probe (editForumTopic) still has to run."""
        mgr.bind_thread(1, 42, "@0", window_name="proj")
        now = 10_000.0
        status_polling._last_typing_sent[(1, 42)] = now - 1  # just sent

        due = status_polling._due_for_probe(now)

        assert due == [(1, 42, "@0")]

    def test_never_probed_binding_is_immediately_due(self, mgr: SessionManager) -> None:
        mgr.bind_thread(1, 42, "@0", window_name="proj")

        due = status_polling._due_for_probe(now=10_000.0)

        assert due == [(1, 42, "@0")]

    def test_gone_marker_bindings_are_never_probed(self, mgr: SessionManager) -> None:
        """A "gone:@N" parked marker has no known topic name — probing
        would RENAME the user's live topic to the marker string. Parked
        topic-name values still probe (their name is the no-op edit)."""
        mgr.bind_thread(1, 1, "gone:@5", window_name="")
        mgr.bind_thread(1, 2, "2 — proj", window_name="")

        due = status_polling._due_for_probe(now=10_000.0)

        assert [t[2] for t in due] == ["2 — proj"]
