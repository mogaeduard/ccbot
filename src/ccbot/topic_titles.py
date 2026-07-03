"""Auto-rename bound topics (and their tmux windows) from Claude's own titles.

Claude Code writes AI-generated session titles into the transcript JSONL
(see session_monitor.py's ai-title detection). Whenever a monitored
session's title changes, every Telegram topic bound to that session's tmux
window is renamed to "N — <title>" (N = the tmux window index), and the
tmux window itself is renamed to match (rename_window) so `tabs` on the Mac
tells the same story — safe because tmux_manager.create_window always sets
allow-rename off, so Claude Code's own TUI never overwrites it.

Renames are best-effort: Telegram/tmux failures are logged at debug and
otherwise swallowed. Since topic names can't be read back from the Bot API,
the last name successfully attempted per window is tracked in-memory
(_last_set_name) so an unchanged title doesn't re-trigger edit_forum_topic
on every poll tick.

Key functions: build_topic_name(), on_ai_title().
"""

import logging

from telegram import Bot
from telegram.error import TelegramError

from .session import session_manager
from .tmux_manager import tmux_manager

logger = logging.getLogger(__name__)

# Telegram forum topic name limit (name: 1-128 characters).
TOPIC_NAME_LIMIT = 128

# window_id -> last topic/window name we attempted to set.
_last_set_name: dict[str, str] = {}


def build_topic_name(
    window_index: str, ai_title: str, limit: int = TOPIC_NAME_LIMIT
) -> str:
    """Build "N — <title>", truncating the title so the whole name fits `limit`."""
    prefix = f"{window_index} — " if window_index else ""
    available = limit - len(prefix)
    if available <= 0:
        return prefix[:limit]
    if len(ai_title) <= available:
        return prefix + ai_title
    if available == 1:
        return prefix + ai_title[:1]
    return prefix + ai_title[: available - 1].rstrip() + "…"


def _find_window_id_for_session(session_id: str) -> str | None:
    for wid, state in session_manager.window_states.items():
        if state.session_id == session_id:
            return wid
    return None


async def on_ai_title(bot: Bot, session_id: str, ai_title: str) -> None:
    """Rename every topic bound to `session_id`'s window (and the window
    itself) to reflect its new ai-title. No-op if the session's window
    can't be resolved or isn't live."""
    if not ai_title:
        return
    window_id = _find_window_id_for_session(session_id)
    if window_id is None:
        return

    windows = await tmux_manager.list_windows()
    window_index = next(
        (w.window_index for w in windows if w.window_id == window_id), ""
    )
    if not window_index:
        return  # window gone or index unknown — nothing to rename

    name = build_topic_name(window_index, ai_title)
    if _last_set_name.get(window_id) == name:
        return
    _last_set_name[window_id] = name

    for user_id, thread_id, wid in session_manager.iter_thread_bindings():
        if wid != window_id:
            continue
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        try:
            await bot.edit_forum_topic(
                chat_id=chat_id, message_thread_id=thread_id, name=name
            )
        except TelegramError as e:
            logger.debug(
                "Title rename: edit_forum_topic failed for thread %d: %s",
                thread_id,
                e,
            )

    session_manager.update_display_name(window_id, name)
    try:
        await tmux_manager.rename_window(window_id, name)
    except Exception as e:
        logger.debug("Title rename: rename_window failed for %s: %s", window_id, e)
