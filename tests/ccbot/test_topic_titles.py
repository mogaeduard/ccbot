"""Tests for topic_titles.py — ai-title-driven topic/window renaming.

Mirrors the SessionManager fixture pattern in test_mirror.py: a fresh
SessionManager with state persistence stubbed out, swapped into
ccbot.topic_titles's module namespace. Telegram and tmux calls are mocked.
"""

from unittest.mock import AsyncMock

import pytest
from telegram.error import TelegramError

from ccbot import topic_titles
from ccbot.session import SessionManager
from ccbot.tmux_manager import TmuxWindow


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    m = SessionManager()
    monkeypatch.setattr(topic_titles, "session_manager", m)
    return m


@pytest.fixture(autouse=True)
def _reset_last_set_name():
    """_last_set_name is module-level (mirrors mirror.py's own state)."""
    topic_titles._last_set_name.clear()
    yield
    topic_titles._last_set_name.clear()


@pytest.fixture
def bot() -> AsyncMock:
    return AsyncMock()


def _window(
    window_id: str = "@0", window_name: str = "proj", window_index: str = "3"
) -> TmuxWindow:
    return TmuxWindow(
        window_id=window_id,
        window_name=window_name,
        cwd="/tmp",
        window_index=window_index,
    )


class TestBuildTopicName:
    def test_short_title_no_truncation(self) -> None:
        assert (
            topic_titles.build_topic_name("3", "Fix login bug") == "3 — Fix login bug"
        )

    def test_no_window_index_omits_prefix(self) -> None:
        assert topic_titles.build_topic_name("", "Fix login bug") == "Fix login bug"

    def test_truncates_to_limit(self) -> None:
        title = "x" * 200
        name = topic_titles.build_topic_name("3", title)
        assert len(name) == 128
        assert name.startswith("3 — ")
        assert name.endswith("…")

    def test_exact_limit_no_truncation_marker(self) -> None:
        prefix = "3 — "
        title = "y" * (128 - len(prefix))
        name = topic_titles.build_topic_name("3", title)
        assert name == prefix + title
        assert len(name) == 128
        assert not name.endswith("…")

    def test_one_char_over_limit_truncates_by_one(self) -> None:
        prefix = "3 — "
        title = "y" * (128 - len(prefix) + 1)
        name = topic_titles.build_topic_name("3", title)
        assert len(name) == 128
        assert name.endswith("…")


class TestOnAiTitle:
    @pytest.mark.asyncio
    async def test_no_matching_window_is_noop(self, monkeypatch, mgr, bot) -> None:
        await topic_titles.on_ai_title(bot, "sess-unknown", "New title")
        bot.edit_forum_topic.assert_not_called()

    @pytest.mark.asyncio
    async def test_dead_window_is_noop(self, monkeypatch, mgr, bot) -> None:
        mgr.get_window_state("@0").session_id = "sess-1"
        monkeypatch.setattr(
            topic_titles.tmux_manager, "list_windows", AsyncMock(return_value=[])
        )

        await topic_titles.on_ai_title(bot, "sess-1", "New title")

        bot.edit_forum_topic.assert_not_called()

    @pytest.mark.asyncio
    async def test_renames_bound_topic_and_window(self, monkeypatch, mgr, bot) -> None:
        mgr.get_window_state("@0").session_id = "sess-1"
        mgr.bind_thread(100, 42, "@0", window_name="proj")
        mgr.set_group_chat_id(100, 42, -100999)
        monkeypatch.setattr(
            topic_titles.tmux_manager,
            "list_windows",
            AsyncMock(return_value=[_window(window_index="3")]),
        )
        rename_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(topic_titles.tmux_manager, "rename_window", rename_mock)

        await topic_titles.on_ai_title(bot, "sess-1", "Fix login bug")

        bot.edit_forum_topic.assert_awaited_once_with(
            chat_id=-100999, message_thread_id=42, name="3 — Fix login bug"
        )
        rename_mock.assert_awaited_once_with("@0", "3 — Fix login bug")
        assert mgr.get_display_name("@0") == "3 — Fix login bug"

    @pytest.mark.asyncio
    async def test_renames_all_topics_bound_to_the_window(
        self, monkeypatch, mgr, bot
    ) -> None:
        """Regardless of mirror- vs phone-created origin, every topic bound
        to this session's window gets renamed."""
        mgr.get_window_state("@0").session_id = "sess-1"
        mgr.bind_thread(100, 1, "@0")
        mgr.bind_thread(100, 2, "@0")
        monkeypatch.setattr(
            topic_titles.tmux_manager,
            "list_windows",
            AsyncMock(return_value=[_window()]),
        )
        monkeypatch.setattr(
            topic_titles.tmux_manager, "rename_window", AsyncMock(return_value=True)
        )

        await topic_titles.on_ai_title(bot, "sess-1", "Title")

        assert bot.edit_forum_topic.await_count == 2

    @pytest.mark.asyncio
    async def test_rename_once_semantics_same_name_skips_second_call(
        self, monkeypatch, mgr, bot
    ) -> None:
        """Once a name has been attempted for a window, an identical
        computed name must not call edit_forum_topic again — topic names
        can't be read back from the API, so this is the only guard."""
        mgr.get_window_state("@0").session_id = "sess-1"
        mgr.bind_thread(100, 42, "@0")
        monkeypatch.setattr(
            topic_titles.tmux_manager,
            "list_windows",
            AsyncMock(return_value=[_window()]),
        )
        monkeypatch.setattr(
            topic_titles.tmux_manager, "rename_window", AsyncMock(return_value=True)
        )

        await topic_titles.on_ai_title(bot, "sess-1", "Same title")
        await topic_titles.on_ai_title(bot, "sess-1", "Same title")

        assert bot.edit_forum_topic.await_count == 1

    @pytest.mark.asyncio
    async def test_changed_title_triggers_a_new_rename(
        self, monkeypatch, mgr, bot
    ) -> None:
        mgr.get_window_state("@0").session_id = "sess-1"
        mgr.bind_thread(100, 42, "@0")
        monkeypatch.setattr(
            topic_titles.tmux_manager,
            "list_windows",
            AsyncMock(return_value=[_window()]),
        )
        monkeypatch.setattr(
            topic_titles.tmux_manager, "rename_window", AsyncMock(return_value=True)
        )

        await topic_titles.on_ai_title(bot, "sess-1", "Title one")
        await topic_titles.on_ai_title(bot, "sess-1", "Title two")

        assert bot.edit_forum_topic.await_count == 2

    @pytest.mark.asyncio
    async def test_edit_forum_topic_failure_is_tolerated(
        self, monkeypatch, mgr, bot
    ) -> None:
        mgr.get_window_state("@0").session_id = "sess-1"
        mgr.bind_thread(100, 42, "@0")
        monkeypatch.setattr(
            topic_titles.tmux_manager,
            "list_windows",
            AsyncMock(return_value=[_window()]),
        )
        monkeypatch.setattr(
            topic_titles.tmux_manager, "rename_window", AsyncMock(return_value=True)
        )
        bot.edit_forum_topic = AsyncMock(side_effect=TelegramError("boom"))

        await topic_titles.on_ai_title(bot, "sess-1", "Title")  # should not raise

    @pytest.mark.asyncio
    async def test_empty_title_is_noop(self, monkeypatch, mgr, bot) -> None:
        mgr.get_window_state("@0").session_id = "sess-1"
        mgr.bind_thread(100, 42, "@0")

        await topic_titles.on_ai_title(bot, "sess-1", "")

        bot.edit_forum_topic.assert_not_called()
