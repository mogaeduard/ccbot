"""Tests for /grab <path> — send a file from a bound topic's tmux pane cwd
(or an absolute path) into the topic as a document.

_grab_target is the trust-boundary guard (Telegram user input -> local
filesystem read -> upload): every check runs against the FINAL resolved
path (symlinks followed), so it's tested directly with real files/symlinks
under a fake $HOME. grab_command is tested separately for the
usage/binding wiring and for surfacing _grab_target's rejection reasons.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.bot import _GRAB_MAX_BYTES, _grab_target, grab_command
from ccbot.tmux_manager import TmuxWindow


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A fake $HOME with CCBOT_DIR pointed at its .ccbot subdir, matching
    the real-world default relationship between the two."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CCBOT_DIR", str(home / ".ccbot"))
    return home


class TestGrabTargetSuccess:
    def test_relative_path_resolves_against_cwd(self, fake_home) -> None:
        project = fake_home / "project"
        project.mkdir()
        (project / "notes.txt").write_text("hi")

        resolved, reason = _grab_target("notes.txt", str(project))

        assert reason is None
        assert resolved == project / "notes.txt"

    def test_relative_path_with_subdir(self, fake_home) -> None:
        project = fake_home / "project"
        (project / "sub").mkdir(parents=True)
        (project / "sub" / "f.txt").write_text("hi")

        resolved, reason = _grab_target("sub/f.txt", str(project))

        assert reason is None
        assert resolved == project / "sub" / "f.txt"

    def test_absolute_path_under_home(self, fake_home) -> None:
        (fake_home / "abs.txt").write_text("hi")

        resolved, reason = _grab_target(str(fake_home / "abs.txt"), "/irrelevant/cwd")

        assert reason is None
        assert resolved == fake_home / "abs.txt"


class TestGrabTargetGuards:
    def test_outside_home_is_denied_without_path_echo(
        self, fake_home, tmp_path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("nope")

        resolved, reason = _grab_target(str(outside / "secret.txt"), str(fake_home))

        assert resolved is None
        assert reason == "outside home"
        assert "secret.txt" not in reason
        assert str(outside) not in reason

    def test_relative_escape_via_dotdot_is_denied(self, fake_home, tmp_path) -> None:
        cwd = fake_home / "project"
        cwd.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("nope")

        resolved, reason = _grab_target(f"../../{outside.name}/secret.txt", str(cwd))

        assert resolved is None
        assert reason == "outside home"

    def test_ccbot_dir_subtree_denied(self, fake_home) -> None:
        ccbot_dir = fake_home / ".ccbot"
        ccbot_dir.mkdir()
        (ccbot_dir / ".env").write_text("TELEGRAM_BOT_TOKEN=x")

        resolved, reason = _grab_target(str(ccbot_dir / ".env"), str(fake_home))

        assert resolved is None
        assert reason is not None
        assert "ccbot config dir" in reason

    def test_dotfile_component_denied(self, fake_home) -> None:
        ssh = fake_home / ".ssh"
        ssh.mkdir()
        (ssh / "id_rsa").write_text("private key")

        resolved, reason = _grab_target(str(ssh / "id_rsa"), str(fake_home))

        assert resolved is None
        assert reason is not None
        assert "hidden path" in reason

    def test_dotfile_itself_denied(self, fake_home) -> None:
        (fake_home / ".env").write_text("SECRET=1")

        resolved, reason = _grab_target(".env", str(fake_home))

        assert resolved is None
        assert "hidden path" in (reason or "")

    def test_symlink_escaping_home_is_denied(self, fake_home, tmp_path) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("nope")
        link = fake_home / "link.txt"
        link.symlink_to(outside)

        resolved, reason = _grab_target(str(link), str(fake_home))

        assert resolved is None
        assert reason == "outside home"

    def test_symlink_into_ccbot_dir_is_denied(self, fake_home) -> None:
        ccbot_dir = fake_home / ".ccbot"
        ccbot_dir.mkdir()
        target = ccbot_dir / ".env"
        target.write_text("TELEGRAM_BOT_TOKEN=x")
        link = fake_home / "innocent.txt"
        link.symlink_to(target)

        resolved, reason = _grab_target(str(link), str(fake_home))

        assert resolved is None
        assert "ccbot config dir" in (reason or "")

    def test_not_found(self, fake_home) -> None:
        resolved, reason = _grab_target("nope.txt", str(fake_home))

        assert resolved is None
        assert reason is not None
        assert "not found" in reason

    def test_directory_is_not_a_regular_file(self, fake_home) -> None:
        (fake_home / "adir").mkdir()

        resolved, reason = _grab_target("adir", str(fake_home))

        assert resolved is None
        assert reason is not None
        assert "not a regular file" in reason

    def test_too_large_is_denied(self, fake_home) -> None:
        big = fake_home / "big.bin"
        with open(big, "wb") as f:
            f.truncate(_GRAB_MAX_BYTES + 1)

        resolved, reason = _grab_target("big.bin", str(fake_home))

        assert resolved is None
        assert reason is not None
        assert "too large" in reason

    def test_exactly_max_size_is_allowed(self, fake_home) -> None:
        exact = fake_home / "exact.bin"
        with open(exact, "wb") as f:
            f.truncate(_GRAB_MAX_BYTES)

        resolved, reason = _grab_target("exact.bin", str(fake_home))

        assert reason is None
        assert resolved == exact


def _make_update(text: str, user_id: int = 1) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.message.reply_document = AsyncMock()
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot = AsyncMock()
    return context


class TestGrabCommand:
    @pytest.mark.asyncio
    async def test_not_allowed_user_is_noop(self) -> None:
        update = _make_update("/grab file.txt")
        with patch("ccbot.bot.is_user_allowed", return_value=False):
            await grab_command(update, _make_context())
        update.message.reply_text.assert_not_awaited()
        update.message.reply_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_argument_shows_usage(self) -> None:
        update = _make_update("/grab")
        with patch("ccbot.bot.is_user_allowed", return_value=True):
            await grab_command(update, _make_context())
        reply_text = update.message.reply_text.call_args.args[0]
        assert "Usage" in reply_text
        update.message.reply_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_session_bound(self) -> None:
        update = _make_update("/grab file.txt")
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
        ):
            mock_sm.resolve_window_for_thread.return_value = None
            await grab_command(update, _make_context())

        reply_text = update.message.reply_text.call_args.args[0]
        assert "No session bound" in reply_text

    @pytest.mark.asyncio
    async def test_window_no_longer_exists(self) -> None:
        update = _make_update("/grab file.txt")
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
        ):
            mock_sm.resolve_window_for_thread.return_value = "@1"
            mock_sm.get_display_name.return_value = "myproject"
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)
            await grab_command(update, _make_context())

        reply_text = update.message.reply_text.call_args.args[0]
        assert "myproject" in reply_text
        assert "no longer exists" in reply_text

    @pytest.mark.asyncio
    async def test_success_sends_document(self, fake_home) -> None:
        project = fake_home / "project"
        project.mkdir()
        (project / "notes.txt").write_text("hi")

        update = _make_update("/grab notes.txt")
        window = TmuxWindow(window_id="@1", window_name="project", cwd=str(project))
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
        ):
            mock_sm.resolve_window_for_thread.return_value = "@1"
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)
            await grab_command(update, _make_context())

        update.message.reply_document.assert_awaited_once_with(
            document=str(project / "notes.txt"), filename="notes.txt"
        )
        update.message.reply_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_guard_failure_replies_with_reason(self, fake_home) -> None:
        project = fake_home / "project"
        project.mkdir()

        update = _make_update("/grab ../.ssh/id_rsa")
        window = TmuxWindow(window_id="@1", window_name="project", cwd=str(project))
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
        ):
            mock_sm.resolve_window_for_thread.return_value = "@1"
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)
            await grab_command(update, _make_context())

        reply_text = update.message.reply_text.call_args.args[0]
        assert "hidden path" in reply_text
        update.message.reply_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_absolute_path_argument(self, fake_home) -> None:
        (fake_home / "abs.txt").write_text("hi")

        update = _make_update(f"/grab {fake_home / 'abs.txt'}")
        window = TmuxWindow(window_id="@1", window_name="project", cwd="/anywhere")
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
        ):
            mock_sm.resolve_window_for_thread.return_value = "@1"
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)
            await grab_command(update, _make_context())

        update.message.reply_document.assert_awaited_once_with(
            document=str(fake_home / "abs.txt"), filename="abs.txt"
        )
