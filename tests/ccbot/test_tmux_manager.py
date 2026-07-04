"""Tests for tmux_manager.send_keys — multiline injection safety.

Raw ``tmux send-keys -l`` feeds text straight into the pty, so an embedded
'\\n' in a literal string is itself an Enter keystroke: a multiline
Telegram message used to submit line-by-line (each line its own shell
command / Claude Code turn) instead of arriving as one block. The fix
routes multiline literal sends through tmux's paste-buffer with bracketed
paste (-p) instead — see send_keys's ``_send_literal``.

Two layers of coverage:
  - TestSendKeysLiteralBranch: mocked libtmux objects, asserts which tmux
    primitive gets called (paste-buffer vs send-keys -l) without touching
    a real tmux server.
  - TestSendKeysMultilineRealTmux: drives an actual, throwaway tmux server
    on socket ``-L batchtest`` (never the default socket) running a plain
    zsh, and proves only one shell execution cycle happens for a two-line
    message while single-line behavior stays byte-identical. Requires the
    ``tmux`` binary; skipped otherwise.
"""

import contextlib
import shutil
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ccbot.tmux_manager import TmuxManager


class TestSendKeysLiteralBranch:
    """Mocked: verify the paste-buffer vs send-keys -l branch selection."""

    def _mock_pane_chain(self):
        """Build a TmuxManager whose get_session()/windows/active_pane chain
        is fully mocked, returning (manager, mock_server, mock_pane)."""
        tm = TmuxManager(session_name="ccbot")
        mock_pane = MagicMock()
        mock_window = MagicMock()
        mock_window.active_pane = mock_pane
        mock_session = MagicMock()
        mock_session.windows.get.return_value = mock_window
        mock_server = MagicMock()
        tm._server = mock_server
        return tm, mock_session, mock_pane

    @pytest.mark.asyncio
    async def test_single_line_uses_plain_send_keys_literal(self) -> None:
        tm, mock_session, mock_pane = self._mock_pane_chain()
        with patch.object(tm, "get_session", return_value=mock_session):
            ok = await tm.send_keys("@1", "hello there", enter=True, literal=True)

        assert ok is True
        mock_pane.send_keys.assert_any_call("hello there", enter=False, literal=True)
        mock_pane.paste_buffer.assert_not_called()
        tm.server.set_buffer.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiline_uses_paste_buffer_with_bracket(self) -> None:
        tm, mock_session, mock_pane = self._mock_pane_chain()
        text = "line one\nline two"
        with patch.object(tm, "get_session", return_value=mock_session):
            ok = await tm.send_keys("@1", text, enter=True, literal=True)

        assert ok is True
        # The raw multiline text must never reach send-keys -l.
        for call in mock_pane.send_keys.call_args_list:
            assert call.args[:1] != (text,)
        tm.server.set_buffer.assert_called_once()
        set_buffer_kwargs = tm.server.set_buffer.call_args
        assert set_buffer_kwargs.args[0] == text
        mock_pane.paste_buffer.assert_called_once()
        paste_kwargs = mock_pane.paste_buffer.call_args.kwargs
        assert paste_kwargs["bracket"] is True
        assert paste_kwargs["delete_after"] is True
        # Same unique buffer name used for both calls
        assert paste_kwargs["buffer_name"] == set_buffer_kwargs.kwargs["buffer_name"]

    @pytest.mark.asyncio
    async def test_multiline_still_sends_final_enter(self) -> None:
        tm, mock_session, mock_pane = self._mock_pane_chain()
        with patch.object(tm, "get_session", return_value=mock_session):
            await tm.send_keys("@1", "a\nb", enter=True, literal=True)

        # _send_enter's call: send_keys("", enter=True, literal=False)
        mock_pane.send_keys.assert_any_call("", enter=True, literal=False)

    @pytest.mark.asyncio
    async def test_two_concurrent_multiline_sends_get_distinct_buffer_names(
        self,
    ) -> None:
        """Different windows must not race on tmux's shared default buffer."""
        tm, mock_session, mock_pane = self._mock_pane_chain()
        with patch.object(tm, "get_session", return_value=mock_session):
            await tm.send_keys("@1", "a\nb", enter=True, literal=True)
            await tm.send_keys("@1", "c\nd", enter=True, literal=True)

        names = [c.kwargs["buffer_name"] for c in tm.server.set_buffer.call_args_list]
        assert len(names) == 2
        assert names[0] != names[1]


class TestListWindowsSkipsPlaceholder:
    """Mocked: list_windows() must exclude the __main__ placeholder both by
    name (the normal case) and by index 0 (belt-and-suspenders — index 0 is
    reserved for the placeholder, see get_or_create_session's
    _ensure_main_window_placement, so it's excluded even if something
    external renamed the window away from "__main__")."""

    @staticmethod
    def _mock_window(window_id: str, name: str, index: str) -> MagicMock:
        w = MagicMock()
        w.window_id = window_id
        w.window_name = name
        w.window_index = index
        w.active_pane.pane_current_path = "/tmp"
        w.active_pane.pane_current_command = "zsh"
        return w

    @pytest.mark.asyncio
    async def test_skips_by_name(self) -> None:
        tm = TmuxManager(session_name="ccbot")
        mock_session = MagicMock()
        mock_session.windows = [
            self._mock_window("@0", "__main__", "0"),
            self._mock_window("@1", "proj", "1"),
        ]
        with patch.object(tm, "get_session", return_value=mock_session):
            windows = await tm.list_windows()
        assert [w.window_name for w in windows] == ["proj"]

    @pytest.mark.asyncio
    async def test_skips_by_index_even_if_renamed(self) -> None:
        tm = TmuxManager(session_name="ccbot")
        mock_session = MagicMock()
        mock_session.windows = [
            self._mock_window("@0", "renamed-somehow", "0"),
            self._mock_window("@1", "proj", "1"),
        ]
        with patch.object(tm, "get_session", return_value=mock_session):
            windows = await tm.list_windows()
        assert [w.window_name for w in windows] == ["proj"]


TMUX_SOCKET = "batchtest"


def _tmux_available() -> bool:
    return shutil.which("tmux") is not None


PLACEHOLDER_SOCKET = "placeholder_test"


@pytest.mark.integration
@pytest.mark.skipif(not _tmux_available(), reason="tmux binary not available")
class TestMainWindowPlacement:
    """BUG 1 regression: the __main__ placeholder must never occupy the slot
    a real terminal would want (index 1, the same as base-index), and must
    never leak into list_windows() — mirror.py's topic-creation relies on
    that to skip it and never make a phantom Telegram topic for it.

    Uses a real, isolated tmux server (never the default socket, killed
    after) since these are libtmux `move-window`/`new-session -n` behaviors
    worth proving against the real binary, not mocks. Relies on this
    machine's ~/.tmux.conf `base-index 1` (loaded by any new tmux server by
    default, any socket) matching production — see TestSendKeysMultilineRealTmux
    above for the same real-server pattern.
    """

    @pytest.fixture
    def server(self):
        import libtmux

        srv = libtmux.Server(socket_name=PLACEHOLDER_SOCKET)
        with contextlib.suppress(Exception):
            srv.kill()
        yield srv
        with contextlib.suppress(Exception):
            srv.kill()

    def test_fresh_session_placeholder_at_index_0(self, server) -> None:
        tm = TmuxManager(session_name="ccbot")
        tm._server = server

        session = tm.get_or_create_session()

        assert session.windows[0].window_name == "__main__"
        assert session.windows[0].window_index == "0"

    def test_real_window_after_placeholder_starts_at_index_1(self, server) -> None:
        tm = TmuxManager(session_name="ccbot")
        tm._server = server
        tm.get_or_create_session()

        session = tm.get_session()
        w = session.new_window(window_name="proj")

        assert w.window_index == "1"

    @pytest.mark.asyncio
    async def test_placeholder_excluded_from_list_windows(self, server) -> None:
        tm = TmuxManager(session_name="ccbot")
        tm._server = server
        tm.get_or_create_session()
        session = tm.get_session()
        session.new_window(window_name="proj")

        windows = await tm.list_windows()

        assert [w.window_name for w in windows] == ["proj"]
        assert all(w.window_id != "@0" for w in windows)

    def test_self_heals_stale_placeholder_at_index_1(self, server) -> None:
        """The current live-bug shape: a placeholder created via the OLD
        create-then-rename path, left stuck at index 1 (base-index) instead
        of moved to 0. get_or_create_session() must move it to 0 the next
        time it's called (e.g. next daemon restart or window creation)
        without touching any real window's index."""
        session = server.new_session(session_name="ccbot", start_directory="/tmp")
        session.windows[0].rename_window("__main__")
        session.new_window(window_name="realproj")

        tm = TmuxManager(session_name="ccbot")
        tm._server = server
        healed = tm.get_or_create_session()

        placeholder = next(w for w in healed.windows if w.window_name == "__main__")
        assert placeholder.window_index == "0"
        real = next(w for w in healed.windows if w.window_name == "realproj")
        assert real.window_index == "2"  # untouched — still its original slot


@pytest.mark.integration
@pytest.mark.skipif(not _tmux_available(), reason="tmux binary not available")
class TestSendKeysMultilineRealTmux:
    """Real, isolated tmux server — never the default socket, killed after."""

    @pytest.fixture
    def batch_tmux(self):
        import libtmux

        server = libtmux.Server(socket_name=TMUX_SOCKET)
        with contextlib.suppress(Exception):
            server.kill()  # clean slate from any leftover run

        session = server.new_session(
            session_name="test",
            start_directory=str(Path.home()),
            window_command="zsh -f",  # no rc files — predictable prompt
        )
        window = session.windows[0]
        pane = window.active_pane
        # Real Enter here (single-line, unaffected by this feature) to
        # establish a stable, recognizable prompt for counting below.
        pane.send_keys("PS1='TESTPROMPT$ '", enter=True, literal=False)
        time.sleep(0.3)

        tm = TmuxManager(session_name="test")
        tm._server = server

        yield tm, window

        with contextlib.suppress(Exception):
            server.kill()

    @staticmethod
    def _capture(window) -> str:
        return "\n".join(window.active_pane.capture_pane())

    @classmethod
    def _prompt_count(cls, window) -> int:
        return sum(
            1
            for line in cls._capture(window).split("\n")
            if line.startswith("TESTPROMPT$")
        )

    @pytest.mark.asyncio
    async def test_multiline_is_one_pasted_block_single_execution(
        self, batch_tmux
    ) -> None:
        tm, window = batch_tmux

        ok = await tm.send_keys(window.window_id, "echo before\necho after")
        assert ok is True
        time.sleep(1.0)

        text = self._capture(window)
        assert "before" in text
        assert "after" in text
        # Broken (line-by-line) behavior produces a fresh prompt after
        # EACH line (2 new prompts); the fix produces exactly one.
        assert self._prompt_count(window) == 2

    @pytest.mark.asyncio
    async def test_single_line_behavior_unchanged(self, batch_tmux) -> None:
        tm, window = batch_tmux

        ok = await tm.send_keys(window.window_id, "echo single line unchanged")
        assert ok is True
        time.sleep(1.0)

        text = self._capture(window)
        assert "echo single line unchanged" in text
        assert "single line unchanged" in text
        assert self._prompt_count(window) == 2
