"""Pinned, auto-updating overview message in the group's General topic.

One line per tmux window ("terminal"): "N · <name/title> · <state>", where
state is derived exactly like status_polling.py derives it: an interactive
UI or unrecognized-dialog fallback showing means 🟠 waiting for input, a
status-line spinner means 🟢 working, otherwise ⚪ idle.

Maintained by its own background poll loop (dashboard_poll_loop), run at
the same cadence as the auto-topic mirror (config.monitor_poll_interval)
and gated the same way — only started when config.mirror_chat_id is set,
since the dashboard lives in that group's General topic
(message_thread_id=None). Kept as a separate task from mirror_poll_loop /
status_poll_loop (rather than piggybacking on either) so this module's
tmux/Telegram calls never surprise those modules' own tests.

Update budget: edits happen only when the rendered content actually
changed AND at least DASHBOARD_MIN_INTERVAL_SECONDS have passed since the
last edit attempt. The message_id is persisted via session_manager
(state.json) so a daemon restart reuses it instead of spawning a
duplicate; if an edit ever fails because the message is gone ("message to
edit not found"), the dashboard is recreated. /dashboard (bot.py) calls
force_recreate() to rebuild it on demand, bypassing the throttle.

Key functions: dashboard_tick(bot), dashboard_poll_loop(bot), force_recreate(bot).
"""

import asyncio
import logging
import time

from telegram import Bot
from telegram.error import BadRequest, TelegramError

from .config import config
from .session import session_manager
from .terminal_parser import (
    is_interactive_ui,
    is_unrecognized_dialog,
    parse_status_line,
)
from .tmux_manager import tmux_manager

logger = logging.getLogger(__name__)

DASHBOARD_MIN_INTERVAL_SECONDS = 5.0

# Not persisted — rebuilt fresh on restart (first tick after restart always
# attempts one edit since _last_content starts as None).
_last_content: str | None = None
_last_attempt_ts: float = 0.0


async def _window_state_emoji(window_id: str) -> str:
    """Derive a terminal's state, mirroring status_polling's precedence:
    interactive UI / unknown dialog (waiting) > status spinner (working) >
    idle."""
    pane_text = await tmux_manager.capture_pane(window_id)
    if not pane_text:
        return "⚪"
    if is_interactive_ui(pane_text) or is_unrecognized_dialog(pane_text):
        return "🟠"
    if parse_status_line(pane_text) is not None:
        return "🟢"
    return "⚪"


def window_state_line(window_index: str, display_name: str, state_emoji: str) -> str:
    """Build one dashboard line: "N · <name/title> · <state>"."""
    return f"{window_index} · {display_name} · {state_emoji}"


async def _build_content() -> str:
    windows = await tmux_manager.list_windows()
    if not windows:
        return "No terminals running."
    lines = []
    for w in sorted(windows, key=lambda w: (w.window_index or "", w.window_id)):
        name = session_manager.get_display_name(w.window_id)
        if name == w.window_id:  # no display name tracked yet — fall back
            name = w.window_name or w.window_id
        state = await _window_state_emoji(w.window_id)
        lines.append(window_state_line(w.window_index or w.window_id, name, state))
    return "\n".join(lines)


async def _create(bot: Bot, chat_id: int, text: str) -> None:
    global _last_content
    sent = await bot.send_message(chat_id=chat_id, text=text)
    session_manager.set_dashboard_message_id(sent.message_id)
    try:
        await bot.pin_chat_message(
            chat_id=chat_id, message_id=sent.message_id, disable_notification=True
        )
    except TelegramError as e:
        logger.debug("Dashboard: pin failed: %s", e)
    _last_content = text


def _is_message_not_found(e: BadRequest) -> bool:
    msg = str(e).lower()
    return "message to edit not found" in msg or "message_id_invalid" in msg


async def dashboard_tick(bot: Bot) -> None:
    """Create/update the live dashboard. No-op when mirror_chat_id is unset."""
    global _last_content, _last_attempt_ts
    chat_id = config.mirror_chat_id
    if not chat_id:
        return

    text = await _build_content()

    msg_id = session_manager.get_dashboard_message_id()
    if msg_id is None:
        await _create(bot, chat_id, text)
        return

    if text == _last_content:
        return
    now = time.monotonic()
    if now - _last_attempt_ts < DASHBOARD_MIN_INTERVAL_SECONDS:
        return
    _last_attempt_ts = now

    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text)
        _last_content = text
    except BadRequest as e:
        if _is_message_not_found(e):
            await _create(bot, chat_id, text)
        else:
            logger.debug("Dashboard: edit failed: %s", e)
    except TelegramError as e:
        logger.debug("Dashboard: edit failed: %s", e)


async def dashboard_poll_loop(bot: Bot) -> None:
    """Background task: run dashboard_tick on the mirror's poll cadence.

    Callers should only start this when config.mirror_chat_id is set —
    dashboard_tick() is a no-op otherwise, so there's no point spinning a task.
    """
    while True:
        try:
            await dashboard_tick(bot)
        except Exception as e:
            logger.error("Dashboard poll loop error: %s", e)
        await asyncio.sleep(config.monitor_poll_interval)


async def force_recreate(bot: Bot) -> None:
    """Force-recreate the dashboard message, bypassing the throttle (used by
    the /dashboard command)."""
    global _last_attempt_ts
    chat_id = config.mirror_chat_id
    if not chat_id:
        return
    text = await _build_content()
    old_id = session_manager.get_dashboard_message_id()
    if old_id is not None:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old_id)
        except TelegramError as e:
            logger.debug("Dashboard: delete old failed: %s", e)
        session_manager.set_dashboard_message_id(None)
    await _create(bot, chat_id, text)
    _last_attempt_ts = time.monotonic()
