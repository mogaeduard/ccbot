"""Fallback handling for full-screen dialogs ccbot has no specific parser for.

terminal_parser.is_unrecognized_dialog() flags panes that show a closed
box-drawn border (claude --resume picker, /login, trust prompts, ...) but
don't match any of the known UI_PATTERNS (AskUserQuestion, ExitPlanMode,
Permission Prompt, ...). Since there's no text signature to parse, the
fallback posts the pane text as a Telegram code block (text-first — see
/screenshot for an actual picture) with a nav button row that forwards
keystrokes into the window.

The nav row reuses the existing CB_ASK_* callback data prefixes (the same
ones interactive_ui.py's keyboard uses), so bot.py's callback_handler
routes a press to whichever refresh applies: a known UI (text edit, via
handle_interactive_ui) or this fallback (also a text edit, via
handle_unknown_dialog) — see bot.py's _refresh_interactive_or_fallback.

Key function: handle_unknown_dialog().
"""

import logging

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from ..session import session_manager
from ..terminal_parser import format_pane_text_block, is_unrecognized_dialog
from ..tmux_manager import tmux_manager
from .callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_UP,
)
from .message_sender import safe_edit, send_with_fallback

logger = logging.getLogger(__name__)

# Track fallback screenshot message IDs: (user_id, thread_id_or_0) -> message_id
_fallback_msgs: dict[tuple[int, int], int] = {}


def get_fallback_msg_id(user_id: int, thread_id: int | None = None) -> int | None:
    """Get the tracked fallback screenshot message ID for a user, if any."""
    return _fallback_msgs.get((user_id, thread_id or 0))


def _build_fallback_keyboard(window_id: str) -> InlineKeyboardMarkup:
    """Nav row for an unrecognized dialog, reusing the CB_ASK_* callback data."""

    def cb(prefix: str) -> str:
        return f"{prefix}{window_id}"[:64]

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⬆️", callback_data=cb(CB_ASK_UP)),
                InlineKeyboardButton("⬇️", callback_data=cb(CB_ASK_DOWN)),
            ],
            [
                InlineKeyboardButton("⬅️", callback_data=cb(CB_ASK_LEFT)),
                InlineKeyboardButton("➡️", callback_data=cb(CB_ASK_RIGHT)),
            ],
            [
                InlineKeyboardButton("⎋ Esc", callback_data=cb(CB_ASK_ESC)),
                InlineKeyboardButton("⏎", callback_data=cb(CB_ASK_ENTER)),
                InlineKeyboardButton("🔄", callback_data=cb(CB_ASK_REFRESH)),
            ],
        ]
    )


async def handle_unknown_dialog(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> bool:
    """Capture the pane; if it shows an unrecognized dialog, post/refresh the
    pane text as a code block with nav buttons (edit in place, like
    interactive_ui.py).

    Returns True if a dialog was detected and (re)posted, False otherwise.
    Callers are responsible for clearing any previously-tracked message via
    clear_fallback_msg when this returns False (dialog disappeared).
    """
    ikey = (user_id, thread_id or 0)
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        return False

    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text or not is_unrecognized_dialog(pane_text):
        return False

    body = format_pane_text_block(pane_text)
    keyboard = _build_fallback_keyboard(window_id)
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)

    existing_msg_id = _fallback_msgs.get(ikey)
    if existing_msg_id:
        await safe_edit(
            bot,
            body,
            chat_id=chat_id,
            message_id=existing_msg_id,
            reply_markup=keyboard,
        )
        return True

    thread_kwargs: dict[str, int] = {}
    if thread_id is not None:
        thread_kwargs["message_thread_id"] = thread_id
    sent = await send_with_fallback(
        bot, chat_id, body, reply_markup=keyboard, **thread_kwargs
    )
    if sent is None:
        return False

    _fallback_msgs[ikey] = sent.message_id
    return True


async def clear_fallback_msg(
    user_id: int,
    bot: Bot | None = None,
    thread_id: int | None = None,
) -> None:
    """Drop the tracked fallback message (dialog disappeared), deleting it
    from chat if possible — mirrors interactive_ui.clear_interactive_msg."""
    ikey = (user_id, thread_id or 0)
    msg_id = _fallback_msgs.pop(ikey, None)
    if bot and msg_id:
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass  # message may already be deleted or too old
