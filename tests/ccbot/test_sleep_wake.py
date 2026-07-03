"""Tests for /sleep and /wake — dynamic quiet hours.

ccbot itself does not gate anything on ~/.ccbot/quiet-until; these commands
only write/delete a plain-text UNIX epoch for an external pager script
(out of repo scope) to read. _parse_hhmm/_next_wake_time are pure helpers
tested directly for the today-vs-tomorrow rollover rule; the command tests
mock _next_wake_time to avoid wall-clock flakiness while still asserting
the parsed (hour, minute) wiring, file contents, and reply text.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import ccbot.bot as bot_module
from ccbot.bot import _next_wake_time, _parse_hhmm, sleep_command, wake_command


def _make_update(text: str, user_id: int = 1) -> MagicMock:
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


class TestParseHHMM:
    def test_valid(self) -> None:
        assert _parse_hhmm("09:15") == (9, 15)
        assert _parse_hhmm("23:59") == (23, 59)
        assert _parse_hhmm("0:0") == (0, 0)

    @pytest.mark.parametrize(
        "raw",
        ["", "garbage", "9", "9:60", "24:00", "9:-1", "9:15:00", "9:xx"],
    )
    def test_invalid(self, raw: str) -> None:
        assert _parse_hhmm(raw) is None


class TestNextWakeTime:
    def test_still_ahead_today(self) -> None:
        now = datetime(2026, 7, 3, 8, 0, 0)
        wake = _next_wake_time(9, 15, now=now)
        assert wake == datetime(2026, 7, 3, 9, 15, 0)

    def test_already_passed_rolls_to_tomorrow(self) -> None:
        now = datetime(2026, 7, 3, 10, 0, 0)
        wake = _next_wake_time(9, 15, now=now)
        assert wake == datetime(2026, 7, 4, 9, 15, 0)

    def test_exact_match_rolls_to_tomorrow(self) -> None:
        """target == now is not 'still ahead' — rolls to tomorrow."""
        now = datetime(2026, 7, 3, 9, 15, 0)
        wake = _next_wake_time(9, 15, now=now)
        assert wake == datetime(2026, 7, 4, 9, 15, 0)


class TestSleepCommand:
    @pytest.mark.asyncio
    async def test_not_allowed_user_is_noop(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        update = _make_update("/sleep")
        with patch("ccbot.bot.is_user_allowed", return_value=False):
            await sleep_command(update, _make_context())
        update.message.reply_text.assert_not_awaited()
        assert not (tmp_path / "quiet-until").exists()

    @pytest.mark.asyncio
    async def test_default_no_arg_uses_0915(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        fixed_wake = datetime(2026, 7, 4, 9, 15, 0)
        mock_next = MagicMock(return_value=fixed_wake)
        monkeypatch.setattr(bot_module, "_next_wake_time", mock_next)

        update = _make_update("/sleep")
        with patch("ccbot.bot.is_user_allowed", return_value=True):
            await sleep_command(update, _make_context())

        mock_next.assert_called_once_with(9, 15)
        quiet_file = tmp_path / "quiet-until"
        assert quiet_file.read_text() == str(int(fixed_wake.timestamp()))
        reply_text = update.message.reply_text.call_args.args[0]
        assert "09:15" in reply_text
        assert "😴" in reply_text

    @pytest.mark.asyncio
    async def test_explicit_hhmm_arg(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        fixed_wake = datetime(2026, 7, 3, 22, 30, 0)
        mock_next = MagicMock(return_value=fixed_wake)
        monkeypatch.setattr(bot_module, "_next_wake_time", mock_next)

        update = _make_update("/sleep 22:30")
        with patch("ccbot.bot.is_user_allowed", return_value=True):
            await sleep_command(update, _make_context())

        mock_next.assert_called_once_with(22, 30)
        quiet_file = tmp_path / "quiet-until"
        assert quiet_file.read_text() == str(int(fixed_wake.timestamp()))
        reply_text = update.message.reply_text.call_args.args[0]
        assert "22:30" in reply_text

    @pytest.mark.asyncio
    async def test_invalid_arg_replies_error_and_writes_nothing(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        update = _make_update("/sleep not-a-time")
        with patch("ccbot.bot.is_user_allowed", return_value=True):
            await sleep_command(update, _make_context())

        assert not (tmp_path / "quiet-until").exists()
        reply_text = update.message.reply_text.call_args.args[0]
        assert "Invalid" in reply_text

    @pytest.mark.asyncio
    async def test_real_clock_wiring_smoke(self, monkeypatch, tmp_path) -> None:
        """End-to-end sanity check without mocking _next_wake_time — catches
        argument-order mistakes a fully-mocked test would hide."""
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        update = _make_update("/sleep 09:15")
        with patch("ccbot.bot.is_user_allowed", return_value=True):
            await sleep_command(update, _make_context())

        expected = _next_wake_time(9, 15)
        quiet_file = tmp_path / "quiet-until"
        # Allow for the (astronomically unlikely) case the real clock ticks
        # past the exact target between the two calls.
        assert int(quiet_file.read_text()) in (
            int(expected.timestamp()),
            int(expected.timestamp()) - 86400,
            int(expected.timestamp()) + 86400,
        )


class TestWakeCommand:
    @pytest.mark.asyncio
    async def test_not_allowed_user_is_noop(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        quiet_file = tmp_path / "quiet-until"
        quiet_file.write_text("123")
        update = _make_update("/wake")
        with patch("ccbot.bot.is_user_allowed", return_value=False):
            await wake_command(update, _make_context())
        update.message.reply_text.assert_not_awaited()
        assert quiet_file.exists()

    @pytest.mark.asyncio
    async def test_deletes_quiet_until_file(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        quiet_file = tmp_path / "quiet-until"
        quiet_file.write_text("123")

        update = _make_update("/wake")
        with patch("ccbot.bot.is_user_allowed", return_value=True):
            await wake_command(update, _make_context())

        assert not quiet_file.exists()
        reply_text = update.message.reply_text.call_args.args[0]
        assert "Awake" in reply_text
        assert "☀️" in reply_text

    @pytest.mark.asyncio
    async def test_missing_file_is_not_an_error(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        update = _make_update("/wake")
        with patch("ccbot.bot.is_user_allowed", return_value=True):
            await wake_command(update, _make_context())
        update.message.reply_text.assert_awaited_once()
