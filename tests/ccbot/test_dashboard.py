"""Tests for dashboard.py — the pinned live terminal overview message.

Mirrors the SessionManager fixture pattern in test_mirror.py: a fresh
SessionManager with state persistence stubbed out, swapped into
ccbot.dashboard's module namespace. Telegram and tmux calls are mocked.
"""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

from ccbot import dashboard
from ccbot.config import config
from ccbot.session import SessionManager
from ccbot.tmux_manager import TmuxWindow


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    m = SessionManager()
    monkeypatch.setattr(dashboard, "session_manager", m)
    return m


@pytest.fixture(autouse=True)
def _reset_module_state():
    """dashboard.py's throttle/content tracking is module-level."""
    dashboard._last_content = None
    dashboard._last_attempt_ts = 0.0
    yield
    dashboard._last_content = None
    dashboard._last_attempt_ts = 0.0


@pytest.fixture(autouse=True)
def _mirror_chat_id(monkeypatch):
    monkeypatch.setattr(config, "mirror_chat_id", -100999)
    yield


@pytest.fixture
def bot() -> AsyncMock:
    b = AsyncMock()
    sent = MagicMock()
    sent.message_id = 555
    b.send_message = AsyncMock(return_value=sent)
    return b


def _window(
    window_id: str = "@0", window_name: str = "proj", window_index: str = "1"
) -> TmuxWindow:
    return TmuxWindow(
        window_id=window_id,
        window_name=window_name,
        cwd="/tmp",
        window_index=window_index,
    )


class TestWindowStateEmoji:
    @pytest.mark.asyncio
    async def test_status_spinner_is_working(
        self, monkeypatch, sample_pane_status_line: str
    ) -> None:
        monkeypatch.setattr(
            dashboard.tmux_manager,
            "capture_pane",
            AsyncMock(return_value=sample_pane_status_line),
        )
        assert await dashboard._window_state_emoji("@0") == "🟢"

    @pytest.mark.asyncio
    async def test_interactive_ui_is_waiting(
        self, monkeypatch, sample_pane_exit_plan: str
    ) -> None:
        monkeypatch.setattr(
            dashboard.tmux_manager,
            "capture_pane",
            AsyncMock(return_value=sample_pane_exit_plan),
        )
        assert await dashboard._window_state_emoji("@0") == "🟠"

    @pytest.mark.asyncio
    async def test_no_ui_no_status_is_idle(
        self, monkeypatch, sample_pane_no_ui: str
    ) -> None:
        monkeypatch.setattr(
            dashboard.tmux_manager,
            "capture_pane",
            AsyncMock(return_value=sample_pane_no_ui),
        )
        assert await dashboard._window_state_emoji("@0") == "⚪"

    @pytest.mark.asyncio
    async def test_empty_pane_is_idle(self, monkeypatch) -> None:
        monkeypatch.setattr(
            dashboard.tmux_manager, "capture_pane", AsyncMock(return_value=None)
        )
        assert await dashboard._window_state_emoji("@0") == "⚪"


class TestBuildContent:
    @pytest.mark.asyncio
    async def test_no_windows(self, monkeypatch, mgr) -> None:
        monkeypatch.setattr(
            dashboard.tmux_manager, "list_windows", AsyncMock(return_value=[])
        )
        assert await dashboard._build_content() == "No terminals running."

    @pytest.mark.asyncio
    async def test_one_line_per_window_uses_display_name_and_state(
        self, monkeypatch, mgr, sample_pane_no_ui: str
    ) -> None:
        mgr.bind_thread(100, 1, "@0")
        mgr.update_display_name("@0", "3 — Fix bug")
        monkeypatch.setattr(
            dashboard.tmux_manager,
            "list_windows",
            AsyncMock(return_value=[_window(window_id="@0", window_index="3")]),
        )
        monkeypatch.setattr(
            dashboard.tmux_manager,
            "capture_pane",
            AsyncMock(return_value=sample_pane_no_ui),
        )

        text = await dashboard._build_content()

        assert text == "3 · 3 — Fix bug · ⚪"

    @pytest.mark.asyncio
    async def test_falls_back_to_tmux_window_name_when_undisplayed(
        self, monkeypatch, mgr, sample_pane_no_ui: str
    ) -> None:
        monkeypatch.setattr(
            dashboard.tmux_manager,
            "list_windows",
            AsyncMock(return_value=[_window(window_id="@0", window_name="proj")]),
        )
        monkeypatch.setattr(
            dashboard.tmux_manager,
            "capture_pane",
            AsyncMock(return_value=sample_pane_no_ui),
        )

        text = await dashboard._build_content()

        assert text == "1 · proj · ⚪"


class TestDashboardTick:
    @pytest.mark.asyncio
    async def test_noop_when_mirror_chat_id_unset(self, monkeypatch, mgr, bot) -> None:
        monkeypatch.setattr(config, "mirror_chat_id", None)
        await dashboard.dashboard_tick(bot)
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_when_missing(self, monkeypatch, mgr, bot) -> None:
        monkeypatch.setattr(
            dashboard.tmux_manager, "list_windows", AsyncMock(return_value=[])
        )
        assert mgr.get_dashboard_message_id() is None

        await dashboard.dashboard_tick(bot)

        bot.send_message.assert_awaited_once_with(
            chat_id=-100999, text="No terminals running."
        )
        bot.pin_chat_message.assert_awaited_once_with(
            chat_id=-100999, message_id=555, disable_notification=True
        )
        assert mgr.get_dashboard_message_id() == 555

    @pytest.mark.asyncio
    async def test_pin_failure_is_ignored(self, monkeypatch, mgr, bot) -> None:
        monkeypatch.setattr(
            dashboard.tmux_manager, "list_windows", AsyncMock(return_value=[])
        )
        bot.pin_chat_message = AsyncMock(side_effect=BadRequest("nope"))

        await dashboard.dashboard_tick(bot)  # must not raise

        assert mgr.get_dashboard_message_id() == 555

    @pytest.mark.asyncio
    async def test_skips_edit_when_content_unchanged(
        self, monkeypatch, mgr, bot
    ) -> None:
        monkeypatch.setattr(
            dashboard.tmux_manager, "list_windows", AsyncMock(return_value=[])
        )
        await dashboard.dashboard_tick(bot)  # creates
        bot.send_message.reset_mock()

        await dashboard.dashboard_tick(bot)  # unchanged content

        bot.edit_message_text.assert_not_called()
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_throttles_edit_within_min_interval(
        self, monkeypatch, mgr, bot
    ) -> None:
        mgr.set_dashboard_message_id(555)
        dashboard._last_content = "old content"
        dashboard._last_attempt_ts = time.monotonic()  # just attempted
        monkeypatch.setattr(
            dashboard.tmux_manager,
            "list_windows",
            AsyncMock(return_value=[_window()]),
        )
        monkeypatch.setattr(
            dashboard.tmux_manager, "capture_pane", AsyncMock(return_value=None)
        )

        await dashboard.dashboard_tick(bot)

        bot.edit_message_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_edits_when_content_changed_and_throttle_elapsed(
        self, monkeypatch, mgr, bot
    ) -> None:
        mgr.set_dashboard_message_id(555)
        dashboard._last_content = "old content"
        dashboard._last_attempt_ts = 0.0  # long ago
        monkeypatch.setattr(
            dashboard.tmux_manager, "list_windows", AsyncMock(return_value=[])
        )

        await dashboard.dashboard_tick(bot)

        bot.edit_message_text.assert_awaited_once_with(
            chat_id=-100999, message_id=555, text="No terminals running."
        )
        assert dashboard._last_content == "No terminals running."

    @pytest.mark.asyncio
    async def test_recreates_on_message_not_found(self, monkeypatch, mgr, bot) -> None:
        mgr.set_dashboard_message_id(999)
        dashboard._last_content = "old content"
        dashboard._last_attempt_ts = 0.0
        monkeypatch.setattr(
            dashboard.tmux_manager, "list_windows", AsyncMock(return_value=[])
        )
        bot.edit_message_text = AsyncMock(
            side_effect=BadRequest("Message to edit not found")
        )

        await dashboard.dashboard_tick(bot)

        bot.send_message.assert_awaited_once()
        assert mgr.get_dashboard_message_id() == 555

    @pytest.mark.asyncio
    async def test_other_edit_failure_does_not_recreate(
        self, monkeypatch, mgr, bot
    ) -> None:
        mgr.set_dashboard_message_id(999)
        dashboard._last_content = "old content"
        dashboard._last_attempt_ts = 0.0
        monkeypatch.setattr(
            dashboard.tmux_manager, "list_windows", AsyncMock(return_value=[])
        )
        bot.edit_message_text = AsyncMock(side_effect=BadRequest("some other error"))

        await dashboard.dashboard_tick(bot)  # should not raise

        bot.send_message.assert_not_called()
        assert mgr.get_dashboard_message_id() == 999


class TestForceRecreate:
    @pytest.mark.asyncio
    async def test_deletes_old_and_creates_new(self, monkeypatch, mgr, bot) -> None:
        mgr.set_dashboard_message_id(111)
        monkeypatch.setattr(
            dashboard.tmux_manager, "list_windows", AsyncMock(return_value=[])
        )

        await dashboard.force_recreate(bot)

        bot.delete_message.assert_awaited_once_with(chat_id=-100999, message_id=111)
        bot.send_message.assert_awaited_once()
        assert mgr.get_dashboard_message_id() == 555

    @pytest.mark.asyncio
    async def test_no_prior_message_just_creates(self, monkeypatch, mgr, bot) -> None:
        monkeypatch.setattr(
            dashboard.tmux_manager, "list_windows", AsyncMock(return_value=[])
        )

        await dashboard.force_recreate(bot)

        bot.delete_message.assert_not_called()
        bot.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_noop_when_mirror_chat_id_unset(self, monkeypatch, mgr, bot) -> None:
        monkeypatch.setattr(config, "mirror_chat_id", None)
        await dashboard.force_recreate(bot)
        bot.send_message.assert_not_called()
