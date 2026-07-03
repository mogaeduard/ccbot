"""Tests for /new <project> — resolution rules and the command handler.

new_command resolves a project name against the directory browser's own
root (build_directory_browser(str(Path.cwd()))'s subdirs — the same
starting point text_handler/topic_created_handler use, per bot.py's
module docstring), then creates a tmux window there. It deliberately does
NOT create/bind the topic itself — the mirror tick does that on its next
pass (see mirror.py).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ccbot.bot import _resolve_project, new_command


class TestResolveProject:
    def test_exact_match_case_insensitive(self) -> None:
        resolved, candidates = _resolve_project(
            "majoratfinder", ["MajoratFinder", "other"]
        )
        assert resolved == "MajoratFinder"
        assert candidates == []

    def test_exact_match_takes_priority_over_prefix(self) -> None:
        """A dir literally named 'cc' must win even if 'ccbot' also prefixes."""
        resolved, candidates = _resolve_project("cc", ["cc", "ccbot"])
        assert resolved == "cc"
        assert candidates == []

    def test_unique_prefix_match(self) -> None:
        resolved, candidates = _resolve_project("majorat", ["majoratfinder", "other"])
        assert resolved == "majoratfinder"
        assert candidates == []

    def test_ambiguous_prefix_returns_candidates(self) -> None:
        resolved, candidates = _resolve_project("cc", ["ccbot", "ccmux", "other"])
        assert resolved is None
        assert sorted(candidates) == ["ccbot", "ccmux"]

    def test_no_match_returns_empty_candidates(self) -> None:
        resolved, candidates = _resolve_project("nope", ["ccbot", "majoratfinder"])
        assert resolved is None
        assert candidates == []

    def test_empty_subdirs(self) -> None:
        resolved, candidates = _resolve_project("anything", [])
        assert resolved is None
        assert candidates == []


def _make_update(text: str, user_id: int = 12345, thread_id: int | None = None):
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.text = text
    update.message.message_thread_id = thread_id
    update.message.reply_text = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.type = "supergroup"
    update.effective_chat.id = -100999
    return update


def _make_context():
    context = MagicMock()
    context.bot = AsyncMock()
    return context


@pytest.fixture(autouse=True)
def _project_dirs(tmp_path, monkeypatch):
    (tmp_path / "majoratfinder").mkdir()
    (tmp_path / "ccbot").mkdir()
    (tmp_path / "ccmux").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestNewCommandHandler:
    @pytest.mark.asyncio
    async def test_missing_argument_shows_usage(self, monkeypatch) -> None:
        from ccbot import bot as bot_module

        update = _make_update("/new")
        context = _make_context()
        create_mock = AsyncMock()
        monkeypatch.setattr(bot_module.tmux_manager, "create_window", create_mock)

        await new_command(update, context)

        create_mock.assert_not_called()
        update.message.reply_text.assert_awaited_once()
        assert "Usage" in update.message.reply_text.call_args.args[0]

    @pytest.mark.asyncio
    async def test_exact_match_creates_window(self, monkeypatch, tmp_path) -> None:
        from ccbot import bot as bot_module

        update = _make_update("/new majoratfinder")
        context = _make_context()
        create_mock = AsyncMock(return_value=(True, "Created", "majoratfinder", "@3"))
        monkeypatch.setattr(bot_module.tmux_manager, "create_window", create_mock)

        await new_command(update, context)

        create_mock.assert_awaited_once_with(
            str(tmp_path / "majoratfinder"), start_claude=True
        )
        reply_text = update.message.reply_text.call_args.args[0]
        assert "🚀" in reply_text
        assert "majoratfinder" in reply_text

    @pytest.mark.asyncio
    async def test_prefix_match_creates_window(self, monkeypatch, tmp_path) -> None:
        from ccbot import bot as bot_module

        update = _make_update("/new majorat")
        context = _make_context()
        create_mock = AsyncMock(return_value=(True, "Created", "majoratfinder", "@3"))
        monkeypatch.setattr(bot_module.tmux_manager, "create_window", create_mock)

        await new_command(update, context)

        create_mock.assert_awaited_once_with(
            str(tmp_path / "majoratfinder"), start_claude=True
        )

    @pytest.mark.asyncio
    async def test_ambiguous_match_replies_with_candidates(self, monkeypatch) -> None:
        from ccbot import bot as bot_module

        update = _make_update("/new cc")
        context = _make_context()
        create_mock = AsyncMock()
        monkeypatch.setattr(bot_module.tmux_manager, "create_window", create_mock)

        await new_command(update, context)

        create_mock.assert_not_called()
        reply_text = update.message.reply_text.call_args.args[0]
        assert "Ambiguous" in reply_text
        assert "ccbot" in reply_text
        assert "ccmux" in reply_text

    @pytest.mark.asyncio
    async def test_no_match_replies_cleanly(self, monkeypatch) -> None:
        from ccbot import bot as bot_module

        update = _make_update("/new nonexistent")
        context = _make_context()
        create_mock = AsyncMock()
        monkeypatch.setattr(bot_module.tmux_manager, "create_window", create_mock)

        await new_command(update, context)

        create_mock.assert_not_called()
        reply_text = update.message.reply_text.call_args.args[0]
        assert "No project matching" in reply_text

    @pytest.mark.asyncio
    async def test_usable_from_general_topic(self, monkeypatch, tmp_path) -> None:
        """thread_id=None (General) must still resolve and create a window."""
        from ccbot import bot as bot_module

        update = _make_update("/new majoratfinder", thread_id=None)
        context = _make_context()
        create_mock = AsyncMock(return_value=(True, "Created", "majoratfinder", "@3"))
        monkeypatch.setattr(bot_module.tmux_manager, "create_window", create_mock)

        await new_command(update, context)

        create_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_window_failure_reports_error(self, monkeypatch) -> None:
        from ccbot import bot as bot_module

        update = _make_update("/new majoratfinder")
        context = _make_context()
        create_mock = AsyncMock(return_value=(False, "boom", "", ""))
        monkeypatch.setattr(bot_module.tmux_manager, "create_window", create_mock)

        await new_command(update, context)

        reply_text = update.message.reply_text.call_args.args[0]
        assert "boom" in reply_text
