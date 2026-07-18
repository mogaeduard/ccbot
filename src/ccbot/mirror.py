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
  - ANY binding (mirror-created or bound through the phone/Mac directory
    browser flow — see bot.py) whose tmux window has since disappeared gets
    its forum topic deleted and the binding cleaned up via the same
    unbind_thread/clear_topic_state path bot.py's topic_closed_handler uses.
    Deployment invariant: all topics live in one forum group
    (config.mirror_chat_id), so "every dead window" and "every mirror-owned
    dead window" are the same set here — there is no separate non-mirror
    binding to spare. status_polling.py's own dead-binding cleanup steps
    aside (skips itself) whenever this mirror is enabled, so it stays the
    single owner of this job.

Key function: mirror_tick(bot).
"""

import asyncio
import logging
from pathlib import Path

from telegram import Bot
from telegram.error import TelegramError

from .config import config
from .handlers.cleanup import clear_topic_state
from .session import session_manager
from .tmux_manager import tmux_manager
from .topic_titles import build_topic_name

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

        # A parked binding for this Terminal-N slot (its terminal vanished
        # while ccbot wasn't watching — see session.resolve_stale_ids)
        # adopts the reopened window: same topic, full history, no
        # duplicate. Slot and remembered-cwd guards live in the adopter.
        adopted = session_manager.adopt_parked_binding(w)
        if adopted is not None:
            logger.info(
                "Mirror: parked topic (thread=%d) adopted window %s",
                adopted[1],
                w.window_id,
            )
            continue

        # Every window gets a topic — including plain shells with no Claude
        # session yet, so each numbered terminal has a standing chat you can
        # type cc/cc-new/cc-resume into (Termius-style). Number-first naming
        # keeps topics identifiable as Terminal N.
        name = w.window_name or (Path(w.cwd).name if w.cwd else "") or w.window_id
        if w.window_index:
            name = build_topic_name(w.window_index, name)
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
    """Delete the topic for every binding whose tmux window has disappeared.

    Covers all bindings, not just mirror-created ones: this deployment keeps
    every topic in the single mirror_chat_id forum group, so a phone-bound
    topic whose window died must vanish exactly like a mirror-created one —
    the tmux-windows-set and bound-topics-set must stay identical.
    """
    live_ids = {w.window_id for w in await tmux_manager.list_windows()}
    if not live_ids:
        # Zero live windows would delete EVERY topic — an irreversible mass
        # action we never take on a single observation: an empty list is far
        # more likely a failed/degraded listing (tmux hiccup before any
        # last-good snapshot exists) than the user closing all terminals at
        # once. ponytail: if truly all terminals closed, topics linger until
        # the next window opens; acceptable vs. unrecoverable history loss.
        return

    for user_id, thread_id, window_id in list(session_manager.iter_thread_bindings()):
        if window_id in live_ids:
            continue
        if not session_manager._is_window_id(window_id):
            # Parked binding (value is a topic name, not a window id — see
            # session.resolve_stale_ids): its terminal vanished while ccbot
            # wasn't watching, NOT on our watch. Never delete its topic —
            # that history is irreplaceable; adoption may still revive it.
            # Must be the same id-shape predicate parking used: a topic
            # literally named "@project" parks as "@project" and a bare
            # startswith("@") test would have fed it to deletion.
            continue

        # Delete (not close) the topic: a closetab'd/X-closed terminal should
        # vanish from Telegram entirely, mirroring `tabs` on the Mac.
        try:
            await bot.delete_forum_topic(
                chat_id=mirror_chat_id, message_thread_id=thread_id
            )
        except TelegramError as e:
            logger.debug("Mirror: failed to delete topic %d: %s", thread_id, e)

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
