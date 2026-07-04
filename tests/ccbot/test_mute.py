"""Tests for /mute and /unmute — notification-channel flag files.

Mirrors ~/.ccbot/mute-mac and ~/.ccbot/mute-phone, which the external pager
script (~/.claude/notify-pager.sh, out of repo scope) reads to skip the Mac
sound / Telegram DM respectively. ccbot itself does not gate anything on
these files — same design as /sleep's quiet-until.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.bot import mute_command, unmute_command


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


class TestMuteCommand:
    @pytest.mark.asyncio
    async def test_not_allowed_user_is_noop(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        update = _make_update("/mute")
        with patch("ccbot.bot.is_user_allowed", return_value=False):
            await mute_command(update, _make_context())
        update.message.reply_text.assert_not_awaited()
        assert not (tmp_path / "mute-mac").exists()
        assert not (tmp_path / "mute-phone").exists()

    @pytest.mark.asyncio
    async def test_no_arg_mutes_both(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        update = _make_update("/mute")
        with patch("ccbot.bot.is_user_allowed", return_value=True):
            await mute_command(update, _make_context())

        assert (tmp_path / "mute-mac").exists()
        assert (tmp_path / "mute-phone").exists()
        reply_text = update.message.reply_text.call_args.args[0]
        assert "Mac sounds muted" in reply_text
        assert "phone pings muted" in reply_text

    @pytest.mark.asyncio
    async def test_mac_arg_mutes_only_mac(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        update = _make_update("/mute mac")
        with patch("ccbot.bot.is_user_allowed", return_value=True):
            await mute_command(update, _make_context())

        assert (tmp_path / "mute-mac").exists()
        assert not (tmp_path / "mute-phone").exists()
        reply_text = update.message.reply_text.call_args.args[0]
        assert "🔇 Mac sounds muted · phone pings on" in reply_text

    @pytest.mark.asyncio
    async def test_phone_arg_mutes_only_phone(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        update = _make_update("/mute phone")
        with patch("ccbot.bot.is_user_allowed", return_value=True):
            await mute_command(update, _make_context())

        assert not (tmp_path / "mute-mac").exists()
        assert (tmp_path / "mute-phone").exists()
        reply_text = update.message.reply_text.call_args.args[0]
        assert "🔊 Mac sounds on · phone pings muted" in reply_text

    @pytest.mark.asyncio
    async def test_invalid_arg_replies_usage_and_writes_nothing(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        update = _make_update("/mute garbage")
        with patch("ccbot.bot.is_user_allowed", return_value=True):
            await mute_command(update, _make_context())

        assert not (tmp_path / "mute-mac").exists()
        assert not (tmp_path / "mute-phone").exists()
        reply_text = update.message.reply_text.call_args.args[0]
        assert "Usage" in reply_text


class TestUnmuteCommand:
    @pytest.mark.asyncio
    async def test_not_allowed_user_is_noop(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        (tmp_path / "mute-mac").touch()
        update = _make_update("/unmute")
        with patch("ccbot.bot.is_user_allowed", return_value=False):
            await unmute_command(update, _make_context())
        update.message.reply_text.assert_not_awaited()
        assert (tmp_path / "mute-mac").exists()

    @pytest.mark.asyncio
    async def test_no_arg_unmutes_both(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        (tmp_path / "mute-mac").touch()
        (tmp_path / "mute-phone").touch()
        update = _make_update("/unmute")
        with patch("ccbot.bot.is_user_allowed", return_value=True):
            await unmute_command(update, _make_context())

        assert not (tmp_path / "mute-mac").exists()
        assert not (tmp_path / "mute-phone").exists()
        reply_text = update.message.reply_text.call_args.args[0]
        assert "🔊 Mac sounds on · phone pings on" in reply_text

    @pytest.mark.asyncio
    async def test_mac_arg_unmutes_only_mac(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        (tmp_path / "mute-mac").touch()
        (tmp_path / "mute-phone").touch()
        update = _make_update("/unmute mac")
        with patch("ccbot.bot.is_user_allowed", return_value=True):
            await unmute_command(update, _make_context())

        assert not (tmp_path / "mute-mac").exists()
        assert (tmp_path / "mute-phone").exists()

    @pytest.mark.asyncio
    async def test_missing_files_is_not_an_error(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        update = _make_update("/unmute")
        with patch("ccbot.bot.is_user_allowed", return_value=True):
            await unmute_command(update, _make_context())
        update.message.reply_text.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_arg_replies_usage(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        update = _make_update("/unmute garbage")
        with patch("ccbot.bot.is_user_allowed", return_value=True):
            await unmute_command(update, _make_context())
        reply_text = update.message.reply_text.call_args.args[0]
        assert "Usage" in reply_text


class TestQuietOverride:
    """/unmute must pierce quiet hours; /mute and /sleep must cancel that."""

    @pytest.mark.asyncio
    async def test_unmute_writes_override_and_clears_quiet_until(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        (tmp_path / "quiet-until").write_text("9999999999")
        update = _make_update("/unmute")
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock),
        ):
            await unmute_command(update, _make_context())
        assert not (tmp_path / "quiet-until").exists()
        override = int((tmp_path / "quiet-override").read_text())
        import time as _time

        assert override > _time.time() + 3600  # comfortably in the future

    @pytest.mark.asyncio
    async def test_mute_cancels_override(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        (tmp_path / "quiet-override").write_text("9999999999")
        update = _make_update("/mute")
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock),
        ):
            await mute_command(update, _make_context())
        assert not (tmp_path / "quiet-override").exists()

    @pytest.mark.asyncio
    async def test_sleep_cancels_override(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        (tmp_path / "quiet-override").write_text("9999999999")
        from ccbot.bot import sleep_command

        update = _make_update("/sleep 09:15")
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock),
            patch("ccbot.bot.overnight_arm"),
        ):
            await sleep_command(update, _make_context())
        assert not (tmp_path / "quiet-override").exists()
        assert (tmp_path / "quiet-until").exists()
