"""Interactive UI handling for Claude Code prompts.

Handles interactive terminal UIs displayed by Claude Code:
  - AskUserQuestion: Multi-choice question prompts
  - ExitPlanMode: Plan mode exit confirmation
  - Permission Prompt: Tool permission requests
  - RestoreCheckpoint: Checkpoint restoration selection

Provides:
  - Keyboard navigation (up/down/left/right/enter/esc)
  - Terminal capture and display
  - Interactive mode tracking per user and thread

State dicts are keyed by (user_id, thread_id_or_0) for Telegram topic support.
"""

import logging

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from ..session import session_manager
from ..terminal_parser import (
    extract_interactive_content,
    is_interactive_ui,
    parse_numbered_options,
    parse_option_states,
)
from ..tmux_manager import tmux_manager
from .callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_NUM,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_SUBMIT,
    CB_ASK_TAB,
    CB_ASK_UP,
)
from .message_sender import (
    NO_LINK_PREVIEW,
    cleanup_deleted_thread,
    is_thread_deleted_error,
)

logger = logging.getLogger(__name__)

# Tool names that trigger interactive UI via JSONL (terminal capture + inline keyboard)
INTERACTIVE_TOOL_NAMES = frozenset({"AskUserQuestion", "ExitPlanMode"})

# Track interactive UI message IDs: (user_id, thread_id_or_0) -> message_id
_interactive_msgs: dict[tuple[int, int], int] = {}

# Track interactive mode: (user_id, thread_id_or_0) -> window_id
_interactive_mode: dict[tuple[int, int], str] = {}

# Last posted UI content per key — lets the status poller re-run
# handle_interactive_ui every tick (self-healing refresh) without
# hammering Telegram with no-op edit_message_text calls.
_interactive_content: dict[tuple[int, int], str] = {}

# Armed when a multiSelect free-text row ("Type something") has been
# focused via arrow keys and is waiting for the user's next plain message —
# that message gets typed straight into the row instead of Claude's prompt.
_pending_inline_edit: set[tuple[int, int]] = set()

# Option labels that focus a free-text input instead of answering directly.
# Pressing their digit focuses the input; the user's next plain Telegram
# message is then typed into it (message_handler injects text as keystrokes).
_TEXT_INPUT_LABELS = ("type something", "chat about this")

# Button label budget — Telegram clips visually around 25-35 chars on
# phones; keep the number prefix and the label head visible.
_OPTION_LABEL_LIMIT = 36


def _is_text_input_option(label: str) -> bool:
    return label.lower().rstrip(".…").strip() in _TEXT_INPUT_LABELS


def get_interactive_option_label(
    user_id: int, thread_id: int | None, number: int
) -> str | None:
    """Label of option `number` in the currently posted UI, if known."""
    content = _interactive_content.get((user_id, thread_id or 0))
    if not content:
        return None
    for num, label in parse_numbered_options(content):
        if num == number:
            return label
    return None


def get_interactive_option_state(
    user_id: int, thread_id: int | None, number: int
) -> bool | None:
    """Checkbox state of option `number` in the currently posted UI.

    None means either no UI is cached or the option has no checkbox (a
    single-select dialog) — the caller's cue that this isn't multiSelect.
    """
    content = _interactive_content.get((user_id, thread_id or 0))
    if not content:
        return None
    return parse_option_states(content).get(number)


def set_pending_inline_edit(user_id: int, thread_id: int | None = None) -> None:
    """Arm inline-edit mode: the user's next plain message types into a
    focused multiSelect free-text row instead of going to Claude's prompt."""
    _pending_inline_edit.add((user_id, thread_id or 0))


def pop_pending_inline_edit(user_id: int, thread_id: int | None = None) -> bool:
    """Consume the pending inline-edit flag. True if it had been set."""
    try:
        _pending_inline_edit.remove((user_id, thread_id or 0))
        return True
    except KeyError:
        return False


def get_interactive_window(user_id: int, thread_id: int | None = None) -> str | None:
    """Get the window_id for user's interactive mode."""
    return _interactive_mode.get((user_id, thread_id or 0))


def set_interactive_mode(
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> None:
    """Set interactive mode for a user."""
    logger.debug(
        "Set interactive mode: user=%d, window_id=%s, thread=%s",
        user_id,
        window_id,
        thread_id,
    )
    _interactive_mode[(user_id, thread_id or 0)] = window_id


def clear_interactive_mode(user_id: int, thread_id: int | None = None) -> None:
    """Clear interactive mode for a user (without deleting message)."""
    logger.debug("Clear interactive mode: user=%d, thread=%s", user_id, thread_id)
    _interactive_mode.pop((user_id, thread_id or 0), None)


def get_interactive_msg_id(user_id: int, thread_id: int | None = None) -> int | None:
    """Get the interactive message ID for a user."""
    return _interactive_msgs.get((user_id, thread_id or 0))


def _build_interactive_keyboard(
    window_id: str,
    ui_name: str = "",
    options: list[tuple[int, str]] | None = None,
    states: dict[int, bool] | None = None,
) -> InlineKeyboardMarkup:
    """Build keyboard for interactive UI navigation.

    When ``options`` is non-empty, one button per numbered option is placed
    first (full-width rows, so labels stay readable) — tapping one sends
    that digit to the dialog, which selects it directly. Free-text options
    ("Type something.", "Chat about this") get a ✏️ marker: their digit
    focuses the dialog's text input, and the user's next plain message is
    typed into it.

    ``states`` carries multiSelect checkbox state (``parse_option_states``):
    when an option number is present, its button is prefixed ``☑ `` or
    ``☐ `` (checked/unchecked — digits there only toggle, they don't
    confirm), and a full-width ✅ Submit row is inserted before the nav row
    so the user can jump straight to the dialog's Submit tab and confirm.

    A compact navigation row is kept below for multi-tab dialogs and
    anything the digit shortcut can't reach. ``ui_name`` controls layout:
    ``RestoreCheckpoint`` omits ←/→ keys since only vertical selection is
    needed.
    """
    vertical_only = ui_name == "RestoreCheckpoint"

    rows: list[list[InlineKeyboardButton]] = []

    for num, label in options or []:
        if num > 9:
            continue  # send_keys sends one digit keypress; >9 never occurs in practice
        if _is_text_input_option(label):
            display = f"✏️ {num}. {label}"
        else:
            display = f"{num}. {label}"
        if states and num in states:
            display = f"{'☑' if states[num] else '☐'} {display}"
        if len(display) > _OPTION_LABEL_LIMIT:
            display = display[: _OPTION_LABEL_LIMIT - 1].rstrip() + "…"
        rows.append(
            [
                InlineKeyboardButton(
                    display, callback_data=f"{CB_ASK_NUM}{num}:{window_id}"[:64]
                )
            ]
        )

    if states:
        rows.append(
            [
                InlineKeyboardButton(
                    "✅ Submit", callback_data=f"{CB_ASK_SUBMIT}{window_id}"[:64]
                )
            ]
        )

    # Navigation row: kept even with option buttons — multi-select dialogs
    # need Space/arrows, multi-question dialogs need Tab/←/→.
    nav: list[InlineKeyboardButton] = [
        InlineKeyboardButton("␣", callback_data=f"{CB_ASK_SPACE}{window_id}"[:64]),
        InlineKeyboardButton("↑", callback_data=f"{CB_ASK_UP}{window_id}"[:64]),
        InlineKeyboardButton("↓", callback_data=f"{CB_ASK_DOWN}{window_id}"[:64]),
    ]
    if not vertical_only:
        nav.extend(
            [
                InlineKeyboardButton(
                    "←", callback_data=f"{CB_ASK_LEFT}{window_id}"[:64]
                ),
                InlineKeyboardButton(
                    "→", callback_data=f"{CB_ASK_RIGHT}{window_id}"[:64]
                ),
            ]
        )
    nav.append(InlineKeyboardButton("⇥", callback_data=f"{CB_ASK_TAB}{window_id}"[:64]))
    rows.append(nav)

    # Action row
    rows.append(
        [
            InlineKeyboardButton(
                "⎋ Esc", callback_data=f"{CB_ASK_ESC}{window_id}"[:64]
            ),
            InlineKeyboardButton(
                "🔄", callback_data=f"{CB_ASK_REFRESH}{window_id}"[:64]
            ),
            InlineKeyboardButton(
                "⏎ Enter", callback_data=f"{CB_ASK_ENTER}{window_id}"[:64]
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def handle_interactive_ui(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> bool:
    """Capture terminal and send interactive UI content to user.

    Handles AskUserQuestion, ExitPlanMode, Permission Prompt, and
    RestoreCheckpoint UIs. Returns True if UI was detected and sent,
    False otherwise.
    """
    ikey = (user_id, thread_id or 0)
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        return False

    # Capture plain text (no ANSI colors)
    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        logger.debug("No pane text captured for window_id %s", window_id)
        return False

    # Quick check if it looks like an interactive UI
    if not is_interactive_ui(pane_text):
        logger.debug(
            "No interactive UI detected in window_id %s (last 3 lines: %s)",
            window_id,
            pane_text.strip().split("\n")[-3:],
        )
        return False

    # Extract content between separators
    content = extract_interactive_content(pane_text)
    if not content:
        return False

    # Unchanged content already posted → nothing to do. This makes the
    # status poller's per-tick refresh (self-healing) essentially free.
    existing_msg_id = _interactive_msgs.get(ikey)
    if existing_msg_id and _interactive_content.get(ikey) == content.content:
        _interactive_mode[ikey] = window_id
        return True

    # Build message with per-option buttons + navigation keyboard
    options = parse_numbered_options(content.content)
    states = parse_option_states(content.content)
    keyboard = _build_interactive_keyboard(
        window_id, ui_name=content.name, options=options, states=states
    )

    # Send as plain text (no markdown conversion)
    text = content.content

    # Build thread kwargs for send_message
    thread_kwargs: dict[str, int] = {}
    if thread_id is not None:
        thread_kwargs["message_thread_id"] = thread_id

    # Check if we have an existing interactive message to edit
    if existing_msg_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=existing_msg_id,
                text=text,
                reply_markup=keyboard,
                link_preview_options=NO_LINK_PREVIEW,
            )
            _interactive_mode[ikey] = window_id
            _interactive_content[ikey] = content.content
            return True
        except BadRequest as e:
            if "Message is not modified" in str(e):
                # Content unchanged — keep existing message as-is
                _interactive_mode[ikey] = window_id
                _interactive_content[ikey] = content.content
                return True
            # Other edit failure — fall through to send new message,
            # but keep old message until replacement succeeds
            logger.debug(
                "Edit failed for interactive msg %s: %s, sending new",
                existing_msg_id,
                e,
            )
        except Exception as e:
            logger.debug(
                "Edit failed for interactive msg %s: %s, sending new",
                existing_msg_id,
                e,
            )

    # Send new message (plain text — terminal content is not markdown)
    logger.info(
        "Sending interactive UI to user %d for window_id %s", user_id, window_id
    )
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboard,
            link_preview_options=NO_LINK_PREVIEW,
            **thread_kwargs,  # type: ignore[arg-type]
        )
    except Exception as e:
        if thread_id is not None and is_thread_deleted_error(e):
            await cleanup_deleted_thread(chat_id, thread_id)
        else:
            logger.error("Failed to send interactive UI: %s", e)
        return False
    if sent:
        _interactive_msgs[ikey] = sent.message_id
        _interactive_mode[ikey] = window_id
        _interactive_content[ikey] = content.content
        # New message sent successfully — now safe to delete the old one
        if existing_msg_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=existing_msg_id)
            except Exception:
                pass  # Old message may already be gone
        return True
    return False


async def clear_interactive_msg(
    user_id: int,
    bot: Bot | None = None,
    thread_id: int | None = None,
) -> None:
    """Clear tracked interactive message, delete from chat, and exit interactive mode."""
    ikey = (user_id, thread_id or 0)
    msg_id = _interactive_msgs.pop(ikey, None)
    _interactive_mode.pop(ikey, None)
    _interactive_content.pop(ikey, None)
    _pending_inline_edit.discard(ikey)
    logger.debug(
        "Clear interactive msg: user=%d, thread=%s, msg_id=%s",
        user_id,
        thread_id,
        msg_id,
    )
    if bot and msg_id:
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass  # Message may already be deleted or too old
