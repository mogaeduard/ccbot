"""Auto-topic mirror — auto-creates Telegram forum topics for tmux windows.

Config-gated: fully off when CCBOT_MIRROR_CHAT_ID is unset. On each poll tick
(mirror_tick, called from bot.py's post_init at the existing monitor poll
cadence):
  - Live tmux windows that already have a tracked Claude session
    (session_map.json, written by the SessionStart hook — fires for
    Mac-launched `claude` too) but no thread binding get a new forum topic
    created and bound via SessionManager.bind_thread, persisted in state.json
    exactly like any other topic-created session. Plain-shell windows with no
    session yet get no topic.
  - Bindings this module created whose tmux window has since disappeared
    (tab closed) get their forum topic closed, a one-line "terminal ended"
    notice posted, and the binding cleaned up via the same
    unbind_thread/clear_topic_state path bot.py's topic_closed_handler uses.

Mirror-owned bindings are identified via SessionManager.group_chat_ids: any
binding whose stored chat_id equals config.mirror_chat_id was created here,
so cleanup never touches phone- or Mac-bound topics created through the
normal directory-browser flow.

Key function: mirror_tick(bot).
"""

import asyncio
import logging
from pathlib import Path

from telegram import Bot
from telegram.error import TelegramError

from .config import config
from .handlers.cleanup import clear_topic_state
from .handlers.message_sender import safe_send
from .session import session_manager
from .tmux_manager import tmux_manager

logger = logging.getLogger(__name__)


def _mirror_user_id() -> int:
    """Stable owner user_id for mirror-created bindings (single-user bot)."""
    return min(config.allowed_users)


async def _create_topics(bot: Bot, mirror_chat_id: int) -> None:
    """Create a forum topic for each live, session-bound, unbound window."""
    windows = await tmux_manager.list_windows()
    bound_window_ids = {wid for _, _, wid in session_manager.iter_thread_bindings()}
    user_id = _mirror_user_id()

    for w in windows:
        if w.window_id in bound_window_ids:
            continue
        state = session_manager.get_window_state(w.window_id)
        if not state.session_id:
            continue  # plain-shell window, no Claude session yet — no topic

        name = w.window_name or (Path(w.cwd).name if w.cwd else "") or w.window_id
        try:
            topic = await bot.create_forum_topic(chat_id=mirror_chat_id, name=name)
        except TelegramError as e:
            logger.warning(
                "Mirror: failed to create topic for window %s: %s", w.window_id, e
            )
            continue

        session_manager.set_group_chat_id(
            user_id, topic.message_thread_id, mirror_chat_id
        )
        session_manager.bind_thread(
            user_id, topic.message_thread_id, w.window_id, window_name=name
        )
        logger.info(
            "Mirror: created topic '%s' (thread=%d) for window %s",
            name,
            topic.message_thread_id,
            w.window_id,
        )


async def _close_dead_topics(bot: Bot, mirror_chat_id: int) -> None:
    """Close mirror-owned topics whose bound window has disappeared."""
    live_ids = {w.window_id for w in await tmux_manager.list_windows()}

    for user_id, thread_id, window_id in list(session_manager.iter_thread_bindings()):
        if window_id in live_ids:
            continue
        key = f"{user_id}:{thread_id}"
        if session_manager.group_chat_ids.get(key) != mirror_chat_id:
            continue  # not a mirror-owned binding — leave to normal cleanup

        display = session_manager.get_display_name(window_id)
        # Notice before close: bots may not be able to post into a closed topic.
        try:
            await safe_send(
                bot,
                mirror_chat_id,
                f"🔌 Terminal ended: {display}",
                message_thread_id=thread_id,
            )
        except TelegramError as e:
            logger.debug(
                "Mirror: failed to post terminal-ended notice for topic %d: %s",
                thread_id,
                e,
            )
        try:
            await bot.close_forum_topic(
                chat_id=mirror_chat_id, message_thread_id=thread_id
            )
        except TelegramError as e:
            logger.debug("Mirror: failed to close topic %d: %s", thread_id, e)

        session_manager.unbind_thread(user_id, thread_id)
        await clear_topic_state(user_id, thread_id, bot)
        logger.info(
            "Mirror: closed topic (thread=%d) for dead window %s", thread_id, window_id
        )


async def mirror_tick(bot: Bot) -> None:
    """Run one mirror poll cycle. No-op when CCBOT_MIRROR_CHAT_ID is unset."""
    mirror_chat_id = config.mirror_chat_id
    if not mirror_chat_id:
        return
    await _create_topics(bot, mirror_chat_id)
    await _close_dead_topics(bot, mirror_chat_id)


async def mirror_poll_loop(bot: Bot) -> None:
    """Background task: run mirror_tick on the session monitor's poll cadence.

    Callers should only start this loop when config.mirror_chat_id is set —
    mirror_tick() is a no-op otherwise, so there's no point spinning a task.
    """
    while True:
        try:
            await mirror_tick(bot)
        except Exception as e:
            logger.error("Mirror poll loop error: %s", e)
        await asyncio.sleep(config.monitor_poll_interval)
