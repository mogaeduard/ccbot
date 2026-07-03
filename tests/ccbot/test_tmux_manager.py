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


TMUX_SOCKET = "batchtest"


def _tmux_available() -> bool:
    return shutil.which("tmux") is not None


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
