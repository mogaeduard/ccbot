"""Tmux session/window management via libtmux.

Wraps libtmux to provide async-friendly operations on a single tmux session:
  - list_windows / find_window_by_name: discover Claude Code windows.
  - capture_pane: read terminal content (plain or with ANSI colors).
  - send_keys: forward user input or control keys to a window. Multiline
    literal text is delivered as one bracketed-paste block (see
    send_keys's _send_literal) instead of raw send-keys -l, which would
    otherwise treat each embedded newline as its own Enter keystroke.
  - create_window / kill_window: lifecycle management.

All blocking libtmux calls are wrapped in asyncio.to_thread().

Key class: TmuxManager (singleton instantiated as `tmux_manager`).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import libtmux

from .config import SENSITIVE_ENV_VARS, config

logger = logging.getLogger(__name__)

# list_windows() result cache lifetime. The 1s status poll calls
# find_window_by_id once per bound topic, and every call used to walk the
# libtmux object graph (list-sessions + list-windows + list-panes per
# window) — O(bindings × windows) tmux fork+execs per second, measured at
# ~78% of a core with 5 topics / 6 windows (2026-07-18). Just under the
# poll interval so each tick does exactly one real listing.
_WINDOWS_CACHE_TTL = 0.9  # seconds

# Field separator for the list-panes format string. Unit separator: cannot
# appear in window names, paths, or commands.
_LIST_SEP = "\x1f"
_LIST_FORMAT = _LIST_SEP.join(
    (
        "#{window_id}",
        "#{window_name}",
        "#{window_index}",
        "#{pane_active}",
        "#{pane_current_path}",
        "#{pane_current_command}",
    )
)


@dataclass
class TmuxWindow:
    """Information about a tmux window."""

    window_id: str
    window_name: str
    cwd: str  # Current working directory
    pane_current_command: str = ""  # Process running in active pane
    window_index: str = ""  # Stable window number (Terminal N)


class TmuxManager:
    """Manages tmux windows for Claude Code sessions."""

    def __init__(self, session_name: str | None = None):
        """Initialize tmux manager.

        Args:
            session_name: Name of the tmux session to use (default from config)
        """
        self.session_name = session_name or config.tmux_session_name
        self._server: libtmux.Server | None = None
        self._windows_cache: tuple[float, list[TmuxWindow]] | None = None

    @property
    def server(self) -> libtmux.Server:
        """Get or create tmux server connection."""
        if self._server is None:
            self._server = libtmux.Server()
        return self._server

    def get_session(self) -> libtmux.Session | None:
        """Get the tmux session if it exists."""
        try:
            return self.server.sessions.get(session_name=self.session_name)
        except Exception:
            return None

    def get_or_create_session(self) -> libtmux.Session:
        """Get existing session or create a new one."""
        session = self.get_session()
        if session:
            self._scrub_session_env(session)
            self._ensure_main_window_placement(session)
            self._set_default_size(session)
            return session

        # Create new session with the main (placeholder) window named in the
        # same `tmux new-session -n` call that creates it — not a separate
        # rename_window() call afterward. A separate call would leave a real
        # gap (default-named window, briefly un-skippable by
        # list_windows()'s name check) that a concurrently-running mirror
        # poll tick could see and create a phantom topic for.
        session = self.server.new_session(
            session_name=self.session_name,
            start_directory=str(Path.home()),
            window_name=config.tmux_main_window_name,
        )
        self._ensure_main_window_placement(session)
        self._scrub_session_env(session)
        self._set_default_size(session)
        return session

    def _set_default_size(self, session: libtmux.Session) -> None:
        """Give detached windows a tall default size (80x24 otherwise).

        At 80x24, a long AskUserQuestion dialog plus the pinned task list
        pushes the dialog's "☐ <header>" line out of the viewport, which
        degrades interactive-UI detection and clips /term and /screenshot
        output. Attached clients still win (window-size latest) — this
        only affects windows no client is viewing.
        """
        try:
            session.cmd("set-option", "-t", self.session_name, "default-size", "200x50")
        except Exception as e:
            logger.debug("Failed to set default-size: %s", e)

    @staticmethod
    def _ensure_main_window_placement(session: libtmux.Session) -> None:
        """Keep the __main__ placeholder parked at window index 0.

        base-index is 1, so a freshly-created session's lone window lands at
        index 1 — the same slot a real terminal (zshrc's _cc_tab) would want
        for "terminal 1". Move the placeholder to 0 (always free, never used
        for real work) so real windows start numbering at 1. Self-heals: run
        on every get_or_create_session() call, so a placeholder left over
        from before this fix (still sitting at index 1) gets moved the next
        time any window is created or the daemon restarts.
        """
        for window in session.windows:
            if (
                window.window_name == config.tmux_main_window_name
                and window.window_index != "0"
            ):
                try:
                    window.move_window(destination="0")
                except Exception as e:
                    logger.debug("Failed to move placeholder window to index 0: %s", e)
                break

    @staticmethod
    def _scrub_session_env(session: libtmux.Session) -> None:
        """Remove sensitive env vars from the tmux session environment.

        Prevents new windows (and their child processes like Claude Code)
        from inheriting secrets such as TELEGRAM_BOT_TOKEN.
        """
        for var in SENSITIVE_ENV_VARS:
            try:
                session.unset_environment(var)
            except Exception:
                pass  # var not set in session env — nothing to remove

    def _invalidate_windows_cache(self) -> None:
        """Drop the cached window list after any window mutation, so the
        next list_windows() reflects the create/kill/rename immediately."""
        self._windows_cache = None

    def _tmux_argv(self, *args: str) -> list[str]:
        """Build a tmux CLI invocation aimed at the same server libtmux
        talks to — tests inject a Server on a private socket; production
        uses the default socket (both socket fields None)."""
        argv = ["tmux"]
        if self._server is not None:
            if self._server.socket_name:
                argv += ["-L", self._server.socket_name]
            elif self._server.socket_path:
                argv += ["-S", self._server.socket_path]
        return argv + list(args)

    async def get_server_start_time(self) -> str:
        """The tmux server's start_time, or "" when no server is running.

        Window ids are never recycled while one server lives — they only
        reset (and start colliding with persisted ids) across a server
        restart. Persisting this value lets startup re-resolution know
        whether persisted ids can be trusted verbatim (same server) or
        must pass identity checks (new server).
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._tmux_argv("list-sessions", "-F", "#{start_time}"),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return ""
            lines = stdout.decode("utf-8", "replace").split()
            return lines[0] if lines else ""
        except Exception as e:
            logger.debug("Failed to read tmux server start_time: %s", e)
            return ""

    async def list_windows(self) -> list[TmuxWindow]:
        """List all windows in the session with their working directories.

        One `tmux list-panes -s` call for the whole session, cached for
        _WINDOWS_CACHE_TTL (see the constant's comment for why). Failures
        are not cached.

        Returns:
            List of TmuxWindow with window info and cwd
        """
        now = time.monotonic()
        if self._windows_cache is not None:
            ts, cached = self._windows_cache
            if now - ts < _WINDOWS_CACHE_TTL:
                return cached

        windows: list[TmuxWindow] = []
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._tmux_argv(
                    "list-panes", "-s", "-t", self.session_name, "-F", _LIST_FORMAT
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                # Session doesn't exist (yet) — same as the old
                # get_session() → None path.
                logger.debug("list-panes failed: %s", stderr.decode("utf-8", "replace"))
                return []
        except Exception as e:
            logger.error(f"Failed to list windows: {e}")
            return []

        for line in stdout.decode("utf-8", "replace").splitlines():
            parts = line.split(_LIST_SEP)
            if len(parts) != 6:
                continue
            window_id, name, index, pane_active, cwd, pane_cmd = parts
            # Only the active pane represents its window (matches the old
            # window.active_pane read). Skip the main window (placeholder):
            # checked by name (the normal case) and by index 0
            # (belt-and-suspenders: that slot is reserved for the
            # placeholder — see _ensure_main_window_placement — so it's
            # excluded even if something external renamed it).
            if pane_active != "1":
                continue
            if name == config.tmux_main_window_name or index == "0":
                continue
            windows.append(
                TmuxWindow(
                    window_id=window_id,
                    window_name=name,
                    cwd=cwd,
                    pane_current_command=pane_cmd,
                    window_index=index,
                )
            )

        self._windows_cache = (now, windows)
        return windows

    async def find_window_by_name(self, window_name: str) -> TmuxWindow | None:
        """Find a window by its name.

        Args:
            window_name: The window name to match

        Returns:
            TmuxWindow if found, None otherwise
        """
        windows = await self.list_windows()
        for window in windows:
            if window.window_name == window_name:
                return window
        logger.debug("Window not found by name: %s", window_name)
        return None

    async def find_window_by_id(self, window_id: str) -> TmuxWindow | None:
        """Find a window by its tmux window ID (e.g. '@0', '@12').

        Args:
            window_id: The tmux window ID to match

        Returns:
            TmuxWindow if found, None otherwise
        """
        windows = await self.list_windows()
        for window in windows:
            if window.window_id == window_id:
                return window
        logger.debug("Window not found by id: %s", window_id)
        return None

    async def capture_pane(self, window_id: str, with_ansi: bool = False) -> str | None:
        """Capture the visible text content of a window's active pane.

        Args:
            window_id: The window ID to capture
            with_ansi: If True, capture with ANSI color codes

        Returns:
            The captured text, or None on failure.
        """
        # Single tmux fork either way. A window target (-t @id) resolves to
        # its active pane — same pane the old libtmux path captured.
        args = ["capture-pane", "-p", "-t", window_id]
        if with_ansi:
            args.insert(1, "-e")
        args = self._tmux_argv(*args)
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.error(
                    f"Failed to capture pane {window_id}: "
                    f"{stderr.decode('utf-8', 'replace')}"
                )
                return None
        except Exception as e:
            logger.error(f"Unexpected error capturing pane {window_id}: {e}")
            return None

        text = stdout.decode("utf-8", "replace")
        if with_ansi:
            # Historical contract of the ANSI path: raw output, trailing
            # newline included.
            return text
        # Historical contract of the plain path (libtmux): trailing blank
        # lines stripped, interior blanks kept.
        return text.rstrip("\n")

    async def send_keys(
        self, window_id: str, text: str, enter: bool = True, literal: bool = True
    ) -> bool:
        """Send keys to a specific window.

        Args:
            window_id: The window ID to send to
            text: Text to send
            enter: Whether to press enter after the text
            literal: If True, send text literally. If False, interpret special keys
                     like "Up", "Down", "Left", "Right", "Escape", "Enter".

        Returns:
            True if successful, False otherwise
        """
        if literal and enter:
            # Split into text + delay + Enter via libtmux.
            # Claude Code's TUI sometimes interprets a rapid-fire Enter
            # (arriving in the same input batch as the text) as a newline
            # rather than submit.  A 500ms gap lets the TUI process the
            # text before receiving Enter.
            def _send_literal(chars: str) -> bool:
                session = self.get_session()
                if not session:
                    logger.error("No tmux session found")
                    return False
                try:
                    window = session.windows.get(window_id=window_id)
                    if not window:
                        logger.error(f"Window {window_id} not found")
                        return False
                    pane = window.active_pane
                    if not pane:
                        logger.error(f"No active pane in window {window_id}")
                        return False
                    if "\n" in chars:
                        # `send-keys -l` feeds raw bytes straight into the
                        # pty — an embedded '\n' IS an Enter keystroke, so a
                        # multiline message would submit line-by-line
                        # instead of arriving as one block (each line is a
                        # separate shell command / a separate Claude Code
                        # turn). Route through tmux's paste-buffer with
                        # bracketed paste (-p) instead: a paste-aware line
                        # editor (zsh's zle, readline, Claude Code's ink
                        # TUI) holds off on executing embedded newlines
                        # until the real Enter _send_enter sends afterward.
                        # A unique buffer name avoids two concurrent
                        # multiline sends (different windows) racing on
                        # tmux's shared default buffer.
                        buffer_name = f"ccbot-{uuid.uuid4().hex}"
                        self.server.set_buffer(chars, buffer_name=buffer_name)
                        pane.paste_buffer(
                            buffer_name=buffer_name,
                            bracket=True,
                            delete_after=True,
                            linefeed_separator=True,
                        )
                    else:
                        pane.send_keys(chars, enter=False, literal=True)
                    return True
                except Exception as e:
                    logger.error(f"Failed to send keys to window {window_id}: {e}")
                    return False

            def _send_enter() -> bool:
                session = self.get_session()
                if not session:
                    return False
                try:
                    window = session.windows.get(window_id=window_id)
                    if not window:
                        return False
                    pane = window.active_pane
                    if not pane:
                        return False
                    pane.send_keys("", enter=True, literal=False)
                    return True
                except Exception as e:
                    logger.error(f"Failed to send Enter to window {window_id}: {e}")
                    return False

            # Claude Code's ! command mode: send "!" first so the TUI
            # switches to bash mode, wait 1s, then send the rest.
            if text.startswith("!"):
                if not await asyncio.to_thread(_send_literal, "!"):
                    return False
                rest = text[1:]
                if rest:
                    await asyncio.sleep(1.0)
                    if not await asyncio.to_thread(_send_literal, rest):
                        return False
            else:
                if not await asyncio.to_thread(_send_literal, text):
                    return False
            await asyncio.sleep(0.5)
            return await asyncio.to_thread(_send_enter)

        # Other cases: special keys (literal=False) or no-enter
        def _sync_send_keys() -> bool:
            session = self.get_session()
            if not session:
                logger.error("No tmux session found")
                return False

            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    logger.error(f"Window {window_id} not found")
                    return False

                pane = window.active_pane
                if not pane:
                    logger.error(f"No active pane in window {window_id}")
                    return False

                pane.send_keys(text, enter=enter, literal=literal)
                return True

            except Exception as e:
                logger.error(f"Failed to send keys to window {window_id}: {e}")
                return False

        return await asyncio.to_thread(_sync_send_keys)

    async def rename_window(self, window_id: str, new_name: str) -> bool:
        """Rename a tmux window by its ID."""

        def _sync_rename() -> bool:
            session = self.get_session()
            if not session:
                return False
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    return False
                window.rename_window(new_name)
                logger.info("Renamed window %s to '%s'", window_id, new_name)
                return True
            except Exception as e:
                logger.error(f"Failed to rename window {window_id}: {e}")
                return False

        result = await asyncio.to_thread(_sync_rename)
        self._invalidate_windows_cache()
        return result

    async def kill_window(self, window_id: str) -> bool:
        """Kill a tmux window by its ID."""

        def _sync_kill() -> bool:
            session = self.get_session()
            if not session:
                return False
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    return False
                window.kill()
                logger.info("Killed window %s", window_id)
                return True
            except Exception as e:
                logger.error(f"Failed to kill window {window_id}: {e}")
                return False

        result = await asyncio.to_thread(_sync_kill)
        self._invalidate_windows_cache()
        return result

    async def create_window(
        self,
        work_dir: str,
        window_name: str | None = None,
        start_claude: bool = True,
        resume_session_id: str | None = None,
    ) -> tuple[bool, str, str, str]:
        """Create a new tmux window and optionally start Claude Code.

        Args:
            work_dir: Working directory for the new window
            window_name: Optional window name (defaults to directory name)
            start_claude: Whether to start claude command
            resume_session_id: If set, append --resume <id> to claude command

        Returns:
            Tuple of (success, message, window_name, window_id)
        """
        # Validate directory first
        path = Path(work_dir).expanduser().resolve()
        if not path.exists():
            return False, f"Directory does not exist: {work_dir}", "", ""
        if not path.is_dir():
            return False, f"Not a directory: {work_dir}", "", ""

        # Create window name, adding suffix if name already exists.
        # path.name is empty at filesystem root ("/") — tmux rejects an empty
        # window name, so fall back to a constant.
        final_window_name = window_name or path.name or "term"

        # Check for existing window name
        base_name = final_window_name
        counter = 2
        while await self.find_window_by_name(final_window_name):
            final_window_name = f"{base_name}-{counter}"
            counter += 1

        # Create window in thread
        def _create_and_start() -> tuple[bool, str, str, str]:
            session = self.get_or_create_session()
            try:
                # Create new window
                window = session.new_window(
                    window_name=final_window_name,
                    start_directory=str(path),
                )

                wid = window.window_id or ""

                # Prevent Claude Code from overriding window name
                window.set_window_option("allow-rename", "off")

                # Start Claude Code if requested
                if start_claude:
                    pane = window.active_pane
                    if pane:
                        cmd = config.claude_command
                        if resume_session_id:
                            cmd = f"{cmd} --resume {resume_session_id}"
                        pane.send_keys(cmd, enter=True)

                logger.info(
                    "Created window '%s' (id=%s) at %s",
                    final_window_name,
                    wid,
                    path,
                )
                return (
                    True,
                    f"Created window '{final_window_name}' at {path}",
                    final_window_name,
                    wid,
                )

            except Exception as e:
                logger.error(f"Failed to create window: {e}")
                return False, f"Failed to create window: {e}", "", ""

        result = await asyncio.to_thread(_create_and_start)
        self._invalidate_windows_cache()
        return result


# Global instance with default session name
tmux_manager = TmuxManager()
