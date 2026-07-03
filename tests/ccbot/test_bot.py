"""Tests for bot.py's phone-created-topic flow (change 3) and the daemon
startup reconcile pass (change 4).

topic_created_handler binds a plain-shell window the instant a topic is
created, then posts the directory browser as the first message.
_create_and_bind_window's reuse path is the critical follow-through: once
that shell window exists, picking a directory/session must drive Claude
into it instead of spawning a second, orphaned window.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import CallbackQuery, User

import ccbot.bot as bot_module
from ccbot.config import config
from ccbot.handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
)
from ccbot.session import SessionManager
from ccbot.tmux_manager import TmuxWindow


def _make_topic_created_update(
    user_id: int = 1, thread_id: int = 42, topic_name: str = "my terminal"
) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.message_thread_id = thread_id
    update.message.forum_topic_created = MagicMock()
    update.message.forum_topic_created.name = topic_name
    update.effective_chat = MagicMock()
    update.effective_chat.type = "supergroup"
    update.effective_chat.id = -100999
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


class TestTopicCreatedHandler:
    @pytest.mark.asyncio
    async def test_binds_shell_window_and_posts_browser(self) -> None:
        update = _make_topic_created_update()
        context = _make_context()
        window = TmuxWindow(
            window_id="@7",
            window_name="my terminal",
            cwd="/home/eduard",
            window_index="3",
        )

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = None  # not yet bound
            mock_tmux.create_window = AsyncMock(
                return_value=(True, "Created window 'my terminal'", "my terminal", "@7")
            )
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)

            from ccbot.bot import topic_created_handler

            await topic_created_handler(update, context)

        # Shell window: start_claude=False, cwd=$HOME, named after the topic
        mock_tmux.create_window.assert_awaited_once_with(
            str(Path.home()), window_name="my terminal", start_claude=False
        )
        mock_sm.set_group_chat_id.assert_called_once_with(1, 42, -100999)
        mock_sm.bind_thread.assert_called_once_with(
            1, 42, "@7", window_name="my terminal"
        )

        mock_reply.assert_awaited_once()
        call = mock_reply.call_args
        assert call.args[0] is update.message
        sent_text = call.args[1]
        assert "terminal 3" in sent_text
        assert "pick a project" in sent_text

        # Directory-browser state primed exactly like the unbound-topic flow
        # in text_handler, so CB_DIR_* callbacks work normally afterward.
        assert context.user_data[STATE_KEY] == STATE_BROWSING_DIRECTORY
        assert context.user_data["_pending_thread_id"] == 42
        assert BROWSE_PATH_KEY in context.user_data
        assert BROWSE_PAGE_KEY in context.user_data
        assert BROWSE_DIRS_KEY in context.user_data

    @pytest.mark.asyncio
    async def test_ignores_non_topic_created_messages(self) -> None:
        update = _make_topic_created_update()
        update.message.forum_topic_created = None
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.tmux_manager") as mock_tmux,
        ):
            from ccbot.bot import topic_created_handler

            await topic_created_handler(update, context)

        mock_tmux.create_window.assert_not_called()

    @pytest.mark.asyncio
    async def test_unauthorized_user_ignored(self) -> None:
        update = _make_topic_created_update()
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=False),
            patch("ccbot.bot.tmux_manager") as mock_tmux,
        ):
            from ccbot.bot import topic_created_handler

            await topic_created_handler(update, context)

        mock_tmux.create_window.assert_not_called()

    @pytest.mark.asyncio
    async def test_already_bound_topic_skips_second_window(self) -> None:
        """create_forum_topic() (mirror.py) also generates a
        forum_topic_created service message for topics it creates itself —
        those are already bound synchronously before this handler ever runs,
        so it must not create a redundant second shell window and steal the
        mirror's binding."""
        update = _make_topic_created_update()
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = "@3"  # already bound

            from ccbot.bot import topic_created_handler

            await topic_created_handler(update, context)

        mock_tmux.create_window.assert_not_called()
        mock_sm.bind_thread.assert_not_called()
        mock_reply.assert_not_awaited()


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    """Fresh SessionManager swapped into bot.py's namespace — real
    bind/unbind/get_window_state behavior without touching state.json,
    matching the fixture pattern in test_mirror.py."""
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    m = SessionManager()
    monkeypatch.setattr(bot_module, "session_manager", m)
    return m


def _make_query() -> MagicMock:
    query = MagicMock(spec=CallbackQuery)
    query.edit_message_text = AsyncMock()
    query.answer = AsyncMock()
    return query


def _make_user(user_id: int = 1) -> MagicMock:
    user = MagicMock(spec=User)
    user.id = user_id
    return user


class TestCreateAndBindWindowReuse:
    """Critical follow-through of change 3: once a topic is already bound to
    a plain-shell window (eager-bound by topic_created_handler), every
    browser/session-picker path must drive Claude into that window instead
    of creating a second, orphaned one."""

    @pytest.mark.asyncio
    async def test_plain_dir_pick_reuses_shell_window(self, monkeypatch, mgr) -> None:
        mgr.bind_thread(1, 42, "@7", window_name="3 — my terminal")  # eager shell bind
        monkeypatch.setattr(
            mgr, "send_to_window", AsyncMock(return_value=(True, "Sent"))
        )
        monkeypatch.setattr(
            mgr, "wait_for_session_map_entry", AsyncMock(return_value=True)
        )
        query = _make_query()
        user = _make_user()
        context = _make_context()

        with patch("ccbot.bot.tmux_manager") as mock_tmux:
            mock_tmux.create_window = AsyncMock()

            from ccbot.bot import _create_and_bind_window

            await _create_and_bind_window(query, context, user, "/home/eduard/proj", 42)

        mock_tmux.create_window.assert_not_called()
        mgr.send_to_window.assert_awaited_once_with(
            "@7", f"cd /home/eduard/proj && {config.claude_command}"
        )
        # Still bound to the SAME window — no second window was left dangling
        assert mgr.get_window_for_thread(1, 42) == "@7"
        query.answer.assert_awaited_once_with("Created")

    @pytest.mark.asyncio
    async def test_resume_pick_reuses_shell_window(self, monkeypatch, mgr) -> None:
        mgr.bind_thread(1, 42, "@7", window_name="3 — my terminal")
        monkeypatch.setattr(
            mgr, "send_to_window", AsyncMock(return_value=(True, "Sent"))
        )
        monkeypatch.setattr(
            mgr, "wait_for_session_map_entry", AsyncMock(return_value=True)
        )
        query = _make_query()
        user = _make_user()
        context = _make_context()

        with patch("ccbot.bot.tmux_manager") as mock_tmux:
            mock_tmux.create_window = AsyncMock()

            from ccbot.bot import _create_and_bind_window

            await _create_and_bind_window(
                query,
                context,
                user,
                "/home/eduard/proj",
                42,
                resume_session_id="sess-123",
            )

        mock_tmux.create_window.assert_not_called()
        mgr.send_to_window.assert_awaited_once_with(
            "@7", f"cd /home/eduard/proj && {config.claude_command} --resume sess-123"
        )
        assert mgr.get_window_for_thread(1, 42) == "@7"
        # Resume override applied to the reused window's state, same as create path
        assert mgr.get_window_state("@7").session_id == "sess-123"

    @pytest.mark.asyncio
    async def test_reuse_target_gone_falls_back_to_new_window(
        self, monkeypatch, mgr
    ) -> None:
        """The eager-bound shell window vanished between bind and pick —
        fall back to creating a fresh one, same as the never-bound case."""
        mgr.bind_thread(1, 42, "@7", window_name="3 — my terminal")
        monkeypatch.setattr(
            mgr, "send_to_window", AsyncMock(return_value=(False, "Window not found"))
        )
        monkeypatch.setattr(
            mgr, "wait_for_session_map_entry", AsyncMock(return_value=True)
        )
        query = _make_query()
        user = _make_user()
        context = _make_context()

        with patch("ccbot.bot.tmux_manager") as mock_tmux:
            mock_tmux.create_window = AsyncMock(
                return_value=(True, "Created window 'proj'", "proj", "@9")
            )

            from ccbot.bot import _create_and_bind_window

            await _create_and_bind_window(query, context, user, "/home/eduard/proj", 42)

        mock_tmux.create_window.assert_awaited_once_with(
            "/home/eduard/proj", resume_session_id=None
        )
        assert mgr.get_window_for_thread(1, 42) == "@9"

    @pytest.mark.asyncio
    async def test_never_bound_thread_creates_new_window(
        self, monkeypatch, mgr
    ) -> None:
        """Baseline: an ordinary unbound topic (no eager shell window) still
        goes through the plain create-window path, unaffected by the reuse
        logic."""
        monkeypatch.setattr(
            mgr, "wait_for_session_map_entry", AsyncMock(return_value=True)
        )
        query = _make_query()
        user = _make_user()
        context = _make_context()

        with patch("ccbot.bot.tmux_manager") as mock_tmux:
            mock_tmux.create_window = AsyncMock(
                return_value=(True, "Created window 'proj'", "proj", "@9")
            )

            from ccbot.bot import _create_and_bind_window

            await _create_and_bind_window(query, context, user, "/home/eduard/proj", 42)

        mock_tmux.create_window.assert_awaited_once_with(
            "/home/eduard/proj", resume_session_id=None
        )
        assert mgr.get_window_for_thread(1, 42) == "@9"


class TestPostInitStartupReconcile:
    """Change 4: the daemon must converge topic/window drift accumulated
    while it was down with one deterministic pass before it starts
    processing Telegram updates, not just whenever the poll loop's first
    iteration happens to run."""

    def _make_application(self) -> MagicMock:
        application = MagicMock()
        application.bot = AsyncMock()
        return application

    @pytest.mark.asyncio
    async def test_mirror_tick_awaited_before_poll_loop_starts(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "mirror_chat_id", -100999)
        application = self._make_application()

        with (
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.SessionMonitor") as mock_monitor_cls,
            patch("ccbot.bot.status_poll_loop", new_callable=AsyncMock),
            patch("ccbot.bot.mirror_tick", new_callable=AsyncMock) as mock_tick,
            patch("ccbot.bot.mirror_poll_loop", new_callable=AsyncMock) as mock_loop,
            patch("ccbot.bot.dashboard_tick", new_callable=AsyncMock),
            patch("ccbot.bot.dashboard_poll_loop", new_callable=AsyncMock),
            patch("ccbot.bot.overnight_poll_loop", new_callable=AsyncMock),
        ):
            mock_sm.resolve_stale_ids = AsyncMock()
            mock_monitor_cls.return_value = MagicMock()

            from ccbot.bot import post_init

            await post_init(application)
            await asyncio.sleep(0)  # let the scheduled background tasks run

        mock_tick.assert_awaited_once_with(application.bot)
        mock_loop.assert_called_once_with(application.bot)
        # resolve_stale_ids (window-id re-resolution across tmux restarts)
        # must complete before the reconcile pass runs against live windows.
        mock_sm.resolve_stale_ids.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mirror_disabled_skips_reconcile_and_loop(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "mirror_chat_id", None)
        application = self._make_application()

        with (
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.SessionMonitor") as mock_monitor_cls,
            patch("ccbot.bot.status_poll_loop", new_callable=AsyncMock),
            patch("ccbot.bot.mirror_tick", new_callable=AsyncMock) as mock_tick,
            patch("ccbot.bot.mirror_poll_loop", new_callable=AsyncMock) as mock_loop,
            patch("ccbot.bot.overnight_poll_loop", new_callable=AsyncMock),
        ):
            mock_sm.resolve_stale_ids = AsyncMock()
            mock_monitor_cls.return_value = MagicMock()

            from ccbot.bot import post_init

            await post_init(application)
            await asyncio.sleep(0)

        mock_tick.assert_not_called()
        mock_loop.assert_not_called()
