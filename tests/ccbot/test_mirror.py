"""Tests for the auto-topic mirror module (mirror.py).

Mirrors the SessionManager fixture pattern in test_session.py: a fresh
SessionManager with state persistence stubbed out, swapped into
ccbot.mirror's module namespace so bind_thread/unbind_thread/group_chat_ids
behave exactly like production without touching real state.json. Telegram
and tmux calls are mocked — no network, no real tmux server.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import TelegramError

from ccbot import mirror
from ccbot.config import config
from ccbot.session import SessionManager
from ccbot.tmux_manager import TmuxWindow


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    m = SessionManager()
    monkeypatch.setattr(mirror, "session_manager", m)
    return m


@pytest.fixture
def bot() -> AsyncMock:
    b = AsyncMock()
    topic = MagicMock()
    topic.message_thread_id = 99
    b.create_forum_topic = AsyncMock(return_value=topic)
    b.close_forum_topic = AsyncMock(return_value=True)
    return b


@pytest.fixture(autouse=True)
def _mirror_chat_id(monkeypatch):
    """Default a mirror chat id for tests that don't care about the gate."""
    monkeypatch.setattr(config, "mirror_chat_id", -100999)
    yield


def _window(
    window_id: str = "@0",
    window_name: str = "proj",
    cwd: str = "/tmp/proj",
    window_index: str = "",
) -> TmuxWindow:
    return TmuxWindow(
        window_id=window_id,
        window_name=window_name,
        cwd=cwd,
        window_index=window_index,
    )


class TestMirrorUserId:
    def test_picks_lowest_allowed_user(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "allowed_users", {9, 5, 1})
        assert mirror._mirror_user_id() == 1


class TestMirrorTickGate:
    @pytest.mark.asyncio
    async def test_noop_when_chat_id_unset(self, monkeypatch, mgr, bot) -> None:
        monkeypatch.setattr(config, "mirror_chat_id", None)
        monkeypatch.setattr(
            mirror.tmux_manager, "list_windows", AsyncMock(return_value=[_window()])
        )
        mgr.get_window_state("@0").session_id = "sess-1"

        await mirror.mirror_tick(bot)

        bot.create_forum_topic.assert_not_called()


class TestCreateTopics:
    @pytest.mark.asyncio
    async def test_creates_topic_for_unbound_session_window(
        self, monkeypatch, mgr, bot
    ) -> None:
        w = _window(window_id="@0", window_name="proj")
        monkeypatch.setattr(
            mirror.tmux_manager, "list_windows", AsyncMock(return_value=[w])
        )
        mgr.get_window_state("@0").session_id = "sess-1"

        await mirror.mirror_tick(bot)

        bot.create_forum_topic.assert_awaited_once_with(
            chat_id=config.mirror_chat_id, name="proj"
        )
        user_id = mirror._mirror_user_id()
        assert mgr.get_window_for_thread(user_id, 99) == "@0"
        assert mgr.resolve_chat_id(user_id, 99) == config.mirror_chat_id

    @pytest.mark.asyncio
    async def test_uses_cwd_basename_when_no_window_name(
        self, monkeypatch, mgr, bot
    ) -> None:
        w = _window(window_id="@0", window_name="", cwd="/home/eduard/majoratfinder")
        monkeypatch.setattr(
            mirror.tmux_manager, "list_windows", AsyncMock(return_value=[w])
        )
        mgr.get_window_state("@0").session_id = "sess-1"

        await mirror.mirror_tick(bot)

        bot.create_forum_topic.assert_awaited_once_with(
            chat_id=config.mirror_chat_id, name="majoratfinder"
        )

    @pytest.mark.asyncio
    async def test_creates_topic_for_plain_shell_window(
        self, monkeypatch, mgr, bot
    ) -> None:
        """Plain-shell windows (no Claude session yet) get a standing topic
        too, so every numbered terminal has a chat to type cc/cc-new into."""
        w = _window(window_id="@0")
        monkeypatch.setattr(
            mirror.tmux_manager, "list_windows", AsyncMock(return_value=[w])
        )
        # No session_id set on the window state (default empty)

        await mirror.mirror_tick(bot)

        bot.create_forum_topic.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_topic_name_prefixed_with_window_index(
        self, monkeypatch, mgr, bot
    ) -> None:
        """Topics are named 'N — name' when the window index is known."""
        w = _window(window_id="@0", window_name="api", window_index="2")
        monkeypatch.setattr(
            mirror.tmux_manager, "list_windows", AsyncMock(return_value=[w])
        )

        await mirror.mirror_tick(bot)

        bot.create_forum_topic.assert_awaited_once_with(
            chat_id=config.mirror_chat_id, name="2 — api"
        )

    @pytest.mark.asyncio
    async def test_skips_already_bound_window(self, monkeypatch, mgr, bot) -> None:
        w = _window(window_id="@0")
        monkeypatch.setattr(
            mirror.tmux_manager, "list_windows", AsyncMock(return_value=[w])
        )
        mgr.get_window_state("@0").session_id = "sess-1"
        mgr.bind_thread(100, 1, "@0")  # already bound (e.g. phone-created)

        await mirror.mirror_tick(bot)

        bot.create_forum_topic.assert_not_called()

    @pytest.mark.asyncio
    async def test_telegram_error_does_not_bind(self, monkeypatch, mgr, bot) -> None:
        w = _window(window_id="@0")
        monkeypatch.setattr(
            mirror.tmux_manager, "list_windows", AsyncMock(return_value=[w])
        )
        mgr.get_window_state("@0").session_id = "sess-1"
        bot.create_forum_topic = AsyncMock(side_effect=TelegramError("boom"))

        await mirror.mirror_tick(bot)  # should not raise

        assert list(mgr.iter_thread_bindings()) == []


class TestCloseDeadTopics:
    @pytest.mark.asyncio
    async def test_closes_mirror_owned_dead_window(self, monkeypatch, mgr, bot) -> None:
        user_id = mirror._mirror_user_id()
        mgr.set_group_chat_id(user_id, 42, config.mirror_chat_id)
        mgr.bind_thread(user_id, 42, "@0", window_name="proj")
        # The OTHER live window proves the listing is genuine — an all-empty
        # listing is treated as degraded and deletes nothing (see below).
        monkeypatch.setattr(
            mirror.tmux_manager,
            "list_windows",
            AsyncMock(return_value=[_window(window_id="@9", window_name="other")]),
        )
        cleanup_mock = AsyncMock()
        monkeypatch.setattr(mirror, "clear_topic_state", cleanup_mock)

        await mirror.mirror_tick(bot)

        bot.delete_forum_topic.assert_awaited_once_with(
            chat_id=config.mirror_chat_id, message_thread_id=42
        )
        assert mgr.get_window_for_thread(user_id, 42) is None
        cleanup_mock.assert_awaited_once_with(user_id, 42, bot)

    @pytest.mark.asyncio
    async def test_closes_phantom_placeholder_binding(
        self, monkeypatch, mgr, bot
    ) -> None:
        """BUG 1 regression: a phantom topic somehow bound to the __main__
        placeholder's window_id (@0) must get cleaned up by this same
        reconciliation, exactly like any other dead binding — no hand-delete
        needed. tmux_manager.list_windows() always excludes the placeholder
        by name/index (see test_tmux_manager.py), so from mirror_tick's
        point of view @0 is indistinguishable from a truly-dead window even
        though the real tmux window is alive and well; the OTHER live
        window (@1) proves this isn't just "no windows at all"."""
        user_id = mirror._mirror_user_id()
        mgr.set_group_chat_id(user_id, 42, config.mirror_chat_id)
        mgr.bind_thread(user_id, 42, "@0", window_name="__main__")
        monkeypatch.setattr(
            mirror.tmux_manager,
            "list_windows",
            AsyncMock(return_value=[_window(window_id="@1", window_name="proj")]),
        )
        cleanup_mock = AsyncMock()
        monkeypatch.setattr(mirror, "clear_topic_state", cleanup_mock)

        await mirror.mirror_tick(bot)

        bot.delete_forum_topic.assert_awaited_once_with(
            chat_id=config.mirror_chat_id, message_thread_id=42
        )
        assert mgr.get_window_for_thread(user_id, 42) is None
        cleanup_mock.assert_awaited_once_with(user_id, 42, bot)

    @pytest.mark.asyncio
    async def test_closes_phone_created_binding_too(
        self, monkeypatch, mgr, bot
    ) -> None:
        """A binding with no group_chat_id tag (e.g. bound before any message
        set one) is treated the same as a mirror-created one: this deployment
        keeps every topic in the single mirror_chat_id forum group, so the
        tmux-windows-set and bound-topics-set must stay identical regardless
        of which flow created the binding.
        """
        mgr.bind_thread(100, 7, "@0", window_name="proj")  # no set_group_chat_id
        monkeypatch.setattr(
            mirror.tmux_manager,
            "list_windows",
            AsyncMock(return_value=[_window(window_id="@9", window_name="other")]),
        )
        cleanup_mock = AsyncMock()
        monkeypatch.setattr(mirror, "clear_topic_state", cleanup_mock)

        await mirror.mirror_tick(bot)

        bot.delete_forum_topic.assert_awaited_once_with(
            chat_id=config.mirror_chat_id, message_thread_id=7
        )
        assert mgr.get_window_for_thread(100, 7) is None
        cleanup_mock.assert_awaited_once_with(100, 7, bot)

    @pytest.mark.asyncio
    async def test_leaves_live_window_bindings_alone(
        self, monkeypatch, mgr, bot
    ) -> None:
        user_id = mirror._mirror_user_id()
        mgr.set_group_chat_id(user_id, 42, config.mirror_chat_id)
        mgr.bind_thread(user_id, 42, "@0", window_name="proj")
        monkeypatch.setattr(
            mirror.tmux_manager,
            "list_windows",
            AsyncMock(return_value=[_window(window_id="@0")]),
        )

        await mirror.mirror_tick(bot)

        bot.delete_forum_topic.assert_not_called()
        assert mgr.get_window_for_thread(user_id, 42) == "@0"

    @pytest.mark.asyncio
    async def test_empty_live_listing_deletes_nothing(
        self, monkeypatch, mgr, bot
    ) -> None:
        """Zero live windows would delete EVERY topic — treated as a
        degraded/failed listing (tmux hiccup before any last-good snapshot
        exists), never acted on: topic deletion is irreversible."""
        user_id = mirror._mirror_user_id()
        mgr.set_group_chat_id(user_id, 42, config.mirror_chat_id)
        mgr.bind_thread(user_id, 42, "@0", window_name="proj")
        monkeypatch.setattr(
            mirror.tmux_manager, "list_windows", AsyncMock(return_value=[])
        )

        await mirror._close_dead_topics(bot, config.mirror_chat_id)

        bot.delete_forum_topic.assert_not_called()
        assert mgr.get_window_for_thread(user_id, 42) == "@0"


class TestParkedBindings:
    """The data-loss guard (2026-07-17 review, finding #6): a binding whose
    terminal vanished while ccbot was NOT watching gets parked at startup
    (value = topic name, not a window id — see session.resolve_stale_ids).
    The mirror must never delete a parked topic — a reboot with a partial
    terminal reopen used to destroy every not-yet-reopened topic's full
    history minutes after boot. A reopened same-slot terminal adopts the
    parked topic instead of getting a duplicate."""

    @pytest.mark.asyncio
    async def test_never_deletes_parked_topics(self, monkeypatch, mgr, bot) -> None:
        user_id = mirror._mirror_user_id()
        mgr.set_group_chat_id(user_id, 42, config.mirror_chat_id)
        mgr.thread_bindings[user_id] = {42: "2 — kolab"}  # parked value
        monkeypatch.setattr(
            mirror.tmux_manager, "list_windows", AsyncMock(return_value=[])
        )
        cleanup_mock = AsyncMock()
        monkeypatch.setattr(mirror, "clear_topic_state", cleanup_mock)

        await mirror.mirror_tick(bot)

        bot.delete_forum_topic.assert_not_called()
        cleanup_mock.assert_not_called()
        assert mgr.get_window_for_thread(user_id, 42) == "2 — kolab"

    @pytest.mark.asyncio
    async def test_reopened_slot_adopts_parked_topic(
        self, monkeypatch, mgr, bot
    ) -> None:
        user_id = mirror._mirror_user_id()
        mgr.set_group_chat_id(user_id, 42, config.mirror_chat_id)
        mgr.thread_bindings[user_id] = {42: "2 — kolab"}
        monkeypatch.setattr(
            mirror.tmux_manager,
            "list_windows",
            AsyncMock(
                return_value=[
                    _window(window_id="@5", window_name="zsh", window_index="2")
                ]
            ),
        )

        await mirror.mirror_tick(bot)

        # Adopted, not duplicated: no new topic, binding re-pointed.
        bot.create_forum_topic.assert_not_called()
        bot.delete_forum_topic.assert_not_called()
        assert mgr.get_window_for_thread(user_id, 42) == "@5"
        assert mgr.get_display_name("@5") == "2 — kolab"

    @pytest.mark.asyncio
    async def test_wrong_slot_gets_fresh_topic_parked_stays(
        self, monkeypatch, mgr, bot
    ) -> None:
        user_id = mirror._mirror_user_id()
        mgr.set_group_chat_id(user_id, 42, config.mirror_chat_id)
        mgr.thread_bindings[user_id] = {42: "2 — kolab"}
        monkeypatch.setattr(
            mirror.tmux_manager,
            "list_windows",
            AsyncMock(
                return_value=[
                    _window(window_id="@5", window_name="zsh", window_index="4")
                ]
            ),
        )

        await mirror.mirror_tick(bot)

        # Slot 4 ≠ parked slot 2: new topic for the window, parked untouched.
        bot.create_forum_topic.assert_called_once()
        bot.delete_forum_topic.assert_not_called()
        assert mgr.get_window_for_thread(user_id, 42) == "2 — kolab"
