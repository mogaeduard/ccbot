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
        monkeypatch.setattr(
            mirror.tmux_manager, "list_windows", AsyncMock(return_value=[])
        )
        send_mock = AsyncMock()
        monkeypatch.setattr(mirror, "safe_send", send_mock)
        cleanup_mock = AsyncMock()
        monkeypatch.setattr(mirror, "clear_topic_state", cleanup_mock)

        await mirror.mirror_tick(bot)

        bot.close_forum_topic.assert_awaited_once_with(
            chat_id=config.mirror_chat_id, message_thread_id=42
        )
        send_mock.assert_awaited_once()
        assert "terminal ended" in send_mock.await_args.args[2].lower()
        assert mgr.get_window_for_thread(user_id, 42) is None
        cleanup_mock.assert_awaited_once_with(user_id, 42, bot)

    @pytest.mark.asyncio
    async def test_leaves_non_mirror_binding_alone(self, monkeypatch, mgr, bot) -> None:
        """A binding not owned by the mirror (no matching group_chat_id) is
        left for the normal status_poll_loop cleanup — mirror never touches it.
        """
        mgr.bind_thread(100, 7, "@0", window_name="proj")  # no set_group_chat_id
        monkeypatch.setattr(
            mirror.tmux_manager, "list_windows", AsyncMock(return_value=[])
        )
        send_mock = AsyncMock()
        monkeypatch.setattr(mirror, "safe_send", send_mock)

        await mirror.mirror_tick(bot)

        bot.close_forum_topic.assert_not_called()
        send_mock.assert_not_called()
        # Binding untouched
        assert mgr.get_window_for_thread(100, 7) == "@0"

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

        bot.close_forum_topic.assert_not_called()
        assert mgr.get_window_for_thread(user_id, 42) == "@0"
