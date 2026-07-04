"""Terminal status line polling for thread-bound windows.

Provides background polling of terminal status lines for all active users:
  - Detects Claude Code status (working, waiting, etc.)
  - Detects interactive UIs (permission prompts) not triggered via JSONL
  - Detects full-screen dialogs with no specific parser (claude --resume
    picker, /login, trust prompts, ...) and posts a screenshot fallback
    (see dialog_fallback.handle_unknown_dialog)
  - Updates status messages in Telegram
  - Polls thread_bindings (each topic = one window)
  - Active deletion probe: Telegram fires no event when a forum topic is
    deleted, so an idle topic (nothing ever sent into it) would otherwise
    never be noticed as gone. Each tick, round-robins a few of the most
    overdue bindings through a cheap TYPING chat action (skipping any
    binding that already got one recently via the "sustained typing while
    working" path below — that counts as probed too); Telegram's "message
    thread not found" on that call means the topic is gone, so the window
    is killed and the binding unbound (see _probe_topic).
  - Dead-window (window gone, binding stale) cleanup: only runs here when
    the auto-topic mirror (mirror.py) is disabled. When the mirror is
    enabled it owns this job (also deletes the now-orphaned forum topic,
    which this fallback path cannot do) — see mirror.py's module docstring.
  - While a window is "working" (spinner status line visible), sustains a
    Telegram typing indicator in its bound topic — re-fired every
    TYPING_ACTION_INTERVAL since Telegram auto-expires it after ~5s.

Key components:
  - STATUS_POLL_INTERVAL: Polling frequency (1 second)
  - PROBE_INTERVAL: every binding gets an active deletion probe at least this often
  - PROBE_BATCH_SIZE: max probe sends per tick (rate budget)
  - TYPING_ACTION_INTERVAL: Typing-indicator re-fire cadence (4 seconds)
  - status_poll_loop: Background polling task
  - update_status_message: Poll and enqueue status updates
"""

import asyncio
import logging
import time

from telegram import Bot
from telegram.constants import ChatAction
from telegram.error import BadRequest

from ..config import config
from ..session import session_manager
from ..terminal_parser import is_interactive_ui, parse_status_line
from ..tmux_manager import tmux_manager
from .dialog_fallback import (
    clear_fallback_msg,
    get_fallback_msg_id,
    handle_unknown_dialog,
)
from .interactive_ui import (
    clear_interactive_msg,
    get_interactive_window,
    handle_interactive_ui,
)
from .cleanup import clear_topic_state
from .message_queue import enqueue_status_update, get_message_queue
from .message_sender import is_thread_deleted_error

logger = logging.getLogger(__name__)

# Status polling interval
STATUS_POLL_INTERVAL = 1.0  # seconds - faster response (rate limiting at send layer)

# Every bound topic gets an active deletion probe at least this often.
PROBE_INTERVAL = 60.0  # seconds

# Max probe sends per STATUS_POLL_INTERVAL tick — spreads probing across
# ticks (round-robin, oldest-probed-first) instead of bursting all bindings
# at once every PROBE_INTERVAL.
# ponytail: fixed small batch; raise if the binding count grows enough that
# PROBE_BATCH_SIZE * (PROBE_INTERVAL / STATUS_POLL_INTERVAL) can't cover it.
PROBE_BATCH_SIZE = 3

# Typing-indicator re-fire cadence — Telegram auto-expires "typing" after ~5s,
# so re-send well before that while a window is working.
TYPING_ACTION_INTERVAL = 4.0  # seconds

# Last time a typing action was sent per bound topic: (user_id, thread_id) ->
# time.monotonic(). Popped on unbind below; a few stray entries from topics
# closed via other cleanup paths (bot.py, mirror.py) are harmless floats.
# ponytail: no cross-module wiring into cleanup.clear_topic_state — would
# create a status_polling<->cleanup circular import for one float per topic.
_last_typing_sent: dict[tuple[int, int], float] = {}

# Last time a binding was actively probed for deletion (idle path only —
# see _last_typing_sent above for the "working" path, which also counts).
_last_probed: dict[tuple[int, int], float] = {}


async def _send_typing_action(bot: Bot, user_id: int, thread_id: int) -> bool:
    """Send a TYPING chat action. Returns False if it failed because the
    topic is gone (Telegram's "message thread not found"); True otherwise
    (success or any other transient error, which isn't this call's job to
    diagnose)."""
    try:
        await bot.send_chat_action(
            chat_id=session_manager.resolve_chat_id(user_id, thread_id),
            message_thread_id=thread_id,
            action=ChatAction.TYPING,
        )
        return True
    except BadRequest as e:
        if is_thread_deleted_error(e):
            return False
        logger.debug("Typing action failed for thread %d: %s", thread_id, e)
        return True
    except Exception as e:
        logger.debug("Typing action failed for thread %d: %s", thread_id, e)
        return True


async def _maybe_send_typing(bot: Bot, user_id: int, thread_id: int) -> None:
    """Sustain Telegram's typing indicator while a window is working, capped
    to one send per topic per TYPING_ACTION_INTERVAL."""
    key = (user_id, thread_id)
    now = time.monotonic()
    if now - _last_typing_sent.get(key, 0.0) < TYPING_ACTION_INTERVAL:
        return
    _last_typing_sent[key] = now
    await _send_typing_action(bot, user_id, thread_id)


async def _probe_topic(bot: Bot, user_id: int, thread_id: int, window_id: str) -> None:
    """Active deletion probe for an idle topic: send a TYPING action purely
    to check the topic still exists. On "message thread not found", kill the
    bound window and unbind — same cleanup as any other dead-binding path.
    A phantom brief "typing…" flash in an idle topic is an acceptable
    side effect of reusing the same chat action as the real indicator.
    """
    if await _send_typing_action(bot, user_id, thread_id):
        return
    w = await tmux_manager.find_window_by_id(window_id)
    if w:
        await tmux_manager.kill_window(w.window_id)
    session_manager.unbind_thread(user_id, thread_id)
    await clear_topic_state(user_id, thread_id, bot)
    _last_typing_sent.pop((user_id, thread_id), None)
    _last_probed.pop((user_id, thread_id), None)
    logger.info(
        "Probe: topic deleted — killed window_id '%s' and unbound thread %d for user %d",
        window_id,
        thread_id,
        user_id,
    )


async def update_status_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    skip_status: bool = False,
) -> None:
    """Poll terminal and check for interactive UIs and status updates.

    UI detection always happens regardless of skip_status. When skip_status=True,
    only UI detection runs (used when message queue is non-empty to avoid
    flooding the queue with status updates).

    Also detects permission prompt UIs (not triggered via JSONL) and enters
    interactive mode when found.
    """
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        # Window gone, enqueue clear (unless skipping status)
        if not skip_status:
            await enqueue_status_update(
                bot, user_id, window_id, None, thread_id=thread_id
            )
        return

    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        # Transient capture failure - keep existing status message
        return

    interactive_window = get_interactive_window(user_id, thread_id)
    should_check_new_ui = True

    if interactive_window == window_id:
        # User is in interactive mode for THIS window
        if is_interactive_ui(pane_text):
            # Interactive UI still showing — skip status update (user is interacting)
            return
        # Interactive UI gone — clear interactive mode, fall through to status check.
        # Don't re-check for new UI this cycle (the old one just disappeared).
        await clear_interactive_msg(user_id, bot, thread_id)
        should_check_new_ui = False
    elif interactive_window is not None:
        # User is in interactive mode for a DIFFERENT window (window switched)
        # Clear stale interactive mode
        await clear_interactive_msg(user_id, bot, thread_id)

    # Check for permission prompt (interactive UI not triggered via JSONL)
    # ALWAYS check UI, regardless of skip_status
    if should_check_new_ui and is_interactive_ui(pane_text):
        logger.debug(
            "Interactive UI detected in polling (user=%d, window=%s, thread=%s)",
            user_id,
            window_id,
            thread_id,
        )
        await handle_interactive_ui(bot, user_id, window_id, thread_id)
        return

    # Fallback: some other full-screen dialog with no specific parser
    # (claude --resume picker, /login, trust prompts, ...). Also always
    # checked regardless of skip_status, same reasoning as the known-UI
    # check above — and clear any stale screenshot once the dialog is gone.
    if should_check_new_ui:
        posted = await handle_unknown_dialog(bot, user_id, window_id, thread_id)
        if posted:
            return
        if get_fallback_msg_id(user_id, thread_id) is not None:
            await clear_fallback_msg(user_id, bot, thread_id)

    status_line = parse_status_line(pane_text)

    # Typing indicator tracks "working" independent of skip_status — a busy
    # message queue shouldn't stop the user from seeing Claude is thinking.
    if status_line and thread_id is not None:
        await _maybe_send_typing(bot, user_id, thread_id)

    # Normal status line check — skip if queue is non-empty
    if skip_status:
        return

    if status_line:
        await enqueue_status_update(
            bot,
            user_id,
            window_id,
            status_line,
            thread_id=thread_id,
        )
    # If no status line, keep existing status message (don't clear on transient state)


def _due_for_probe(now: float) -> list[tuple[int, int, str]]:
    """Bindings due for an active deletion probe, oldest-probed-first.

    A binding that already got a typing action recently (the "sustained
    typing while working" path) counts as probed too — no need to send a
    second one just to check existence.
    """
    due = [
        (user_id, thread_id, wid)
        for user_id, thread_id, wid in session_manager.iter_thread_bindings()
        if now
        - max(
            _last_probed.get((user_id, thread_id), 0.0),
            _last_typing_sent.get((user_id, thread_id), 0.0),
        )
        >= PROBE_INTERVAL
    ]
    due.sort(key=lambda t: _last_probed.get((t[0], t[1]), 0.0))
    return due[:PROBE_BATCH_SIZE]


async def status_poll_loop(bot: Bot) -> None:
    """Background task to poll terminal status for all thread-bound windows."""
    logger.info("Status polling started (interval: %ss)", STATUS_POLL_INTERVAL)
    while True:
        try:
            # Active deletion probe: round-robin a rate-limited batch of the
            # most-overdue bindings every tick (see _due_for_probe).
            now = time.monotonic()
            for user_id, thread_id, wid in _due_for_probe(now):
                _last_probed[(user_id, thread_id)] = now
                try:
                    await _probe_topic(bot, user_id, thread_id, wid)
                except Exception as e:
                    logger.debug("Probe error for thread %d: %s", thread_id, e)

            for user_id, thread_id, wid in list(session_manager.iter_thread_bindings()):
                try:
                    # Clean up stale bindings (window no longer exists)
                    w = await tmux_manager.find_window_by_id(wid)
                    if not w:
                        if not config.mirror_chat_id:
                            # No mirror running to own dead-window cleanup —
                            # fall back to the pre-mirror behavior (unbind +
                            # clear state; the topic itself is left for the
                            # user to close manually).
                            session_manager.unbind_thread(user_id, thread_id)
                            await clear_topic_state(user_id, thread_id, bot)
                            _last_typing_sent.pop((user_id, thread_id), None)
                            logger.info(
                                "Cleaned up stale binding: user=%d thread=%d window_id=%s",
                                user_id,
                                thread_id,
                                wid,
                            )
                        continue

                    # UI detection happens unconditionally in update_status_message.
                    # Status enqueue is skipped inside update_status_message when
                    # interactive UI is detected (returns early) or when queue is non-empty.
                    queue = get_message_queue(user_id)
                    skip_status = queue is not None and not queue.empty()

                    await update_status_message(
                        bot,
                        user_id,
                        wid,
                        thread_id=thread_id,
                        skip_status=skip_status,
                    )
                except Exception as e:
                    logger.debug(
                        f"Status update error for user {user_id} "
                        f"thread {thread_id}: {e}"
                    )
        except Exception as e:
            logger.error(f"Status poll loop error: {e}")

        await asyncio.sleep(STATUS_POLL_INTERVAL)
