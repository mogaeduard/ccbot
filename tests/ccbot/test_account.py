"""Tests for /account — reports which Claude account this host runs on.

_read_account_info reads ~/.claude.json (email/org) and
~/.claude/.credentials.json (plan tier, Linux only). Tests point Path.home()
at a tmp dir; no real credentials are touched.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.bot import _read_account_info, account_command


def _make_update(text: str = "/account", user_id: int = 1) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _write_home(tmp_path, oauth=None, creds=None) -> None:
    if oauth is not None:
        (tmp_path / ".claude.json").write_text(json.dumps({"oauthAccount": oauth}))
    if creds is not None:
        (tmp_path / ".claude").mkdir(exist_ok=True)
        (tmp_path / ".claude" / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": creds})
        )


class TestReadAccountInfo:
    def test_full_info(self, tmp_path) -> None:
        _write_home(
            tmp_path,
            oauth={"emailAddress": "a@b.com", "organizationName": "Org"},
            creds={"subscriptionType": "max"},
        )
        with (
            patch("ccbot.bot.Path.home", return_value=tmp_path),
            patch("ccbot.bot.sys.platform", "linux"),
        ):
            text = _read_account_info()
        assert "a@b.com" in text
        assert "Org" in text
        assert "max" in text

    def test_email_only_no_credentials_file(self, tmp_path) -> None:
        _write_home(tmp_path, oauth={"emailAddress": "a@b.com"})
        with (
            patch("ccbot.bot.Path.home", return_value=tmp_path),
            patch("ccbot.bot.sys.platform", "linux"),
        ):
            text = _read_account_info()
        assert "a@b.com" in text
        assert "Plan" not in text

    def test_no_login_found(self, tmp_path) -> None:
        with (
            patch("ccbot.bot.Path.home", return_value=tmp_path),
            patch("ccbot.bot.sys.platform", "linux"),
        ):
            text = _read_account_info()
        assert text.startswith("❌")

    def test_malformed_claude_json(self, tmp_path) -> None:
        (tmp_path / ".claude.json").write_text("{not json")
        with (
            patch("ccbot.bot.Path.home", return_value=tmp_path),
            patch("ccbot.bot.sys.platform", "linux"),
        ):
            text = _read_account_info()
        assert text.startswith("❌")


class TestAccountCommand:
    @pytest.mark.asyncio
    async def test_not_allowed_user_is_noop(self) -> None:
        update = _make_update()
        with patch("ccbot.bot.is_user_allowed", return_value=False):
            await account_command(update, MagicMock())
        update.message.reply_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_replies_with_account_info(self, tmp_path) -> None:
        _write_home(tmp_path, oauth={"emailAddress": "a@b.com"})
        update = _make_update()
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.Path.home", return_value=tmp_path),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as reply,
        ):
            await account_command(update, MagicMock())
        reply.assert_awaited_once()
        assert "a@b.com" in reply.await_args.args[1]


class TestKeychainPreference:
    def test_darwin_prefers_keychain_over_stale_file(self, tmp_path) -> None:
        _write_home(
            tmp_path,
            oauth={"emailAddress": "a@b.com"},
            creds={"subscriptionType": "pro"},  # stale file
        )
        keychain = MagicMock()
        keychain.returncode = 0
        keychain.stdout = json.dumps({"claudeAiOauth": {"subscriptionType": "max"}})
        with (
            patch("ccbot.bot.Path.home", return_value=tmp_path),
            patch("ccbot.bot.sys.platform", "darwin"),
            patch("ccbot.bot.subprocess.run", return_value=keychain),
        ):
            text = _read_account_info()
        assert "max" in text
        assert "pro" not in text

    def test_darwin_falls_back_to_file_when_keychain_fails(self, tmp_path) -> None:
        _write_home(
            tmp_path,
            oauth={"emailAddress": "a@b.com"},
            creds={"subscriptionType": "team"},
        )
        keychain = MagicMock()
        keychain.returncode = 1
        keychain.stdout = ""
        with (
            patch("ccbot.bot.Path.home", return_value=tmp_path),
            patch("ccbot.bot.sys.platform", "darwin"),
            patch("ccbot.bot.subprocess.run", return_value=keychain),
        ):
            text = _read_account_info()
        assert "team" in text
