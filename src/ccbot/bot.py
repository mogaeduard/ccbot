"""Telegram bot handlers — the main UI layer of CCBot.

Registers all command/callback/message handlers and manages the bot lifecycle.
Each Telegram topic maps 1:1 to a tmux window (Claude session).

Core responsibilities:
  - Command handlers: /start, /history, /screenshot, /esc, /kill, /unbind,
    /lock, /unlock, /sleep, /wake, /killall, /grab, plus forwarding unknown
    /commands to Claude Code via tmux.
  - Callback query handler: directory browser, history pagination,
    interactive UI navigation, screenshot refresh.
  - Topic-based routing: each named topic binds to one tmux window.
    Unbound topics trigger the directory browser to create a new session.
  - Lock gate: while /lock is active (session_manager.is_locked), a
    group=-1 handler (lock_gate_handler) drops every inbound update except
    /unlock before any other handler runs, replying "🔒 locked" at most
    once a minute. Background loops (status polling, session monitor,
    mirror) are separate asyncio tasks and keep running untouched.
  - Photo handling: photos sent by user are downloaded and forwarded
    to Claude Code as file paths (photo_handler).
  - Voice handling: voice messages are transcribed via OpenAI API and
    held as a Send/Cancel-confirmed pending transcript (voice_handler,
    _pending_voice) with a 5-minute TTL — see _expire_pending_voice.
  - Phone-created topics: a new forum topic immediately gets a bound
    plain-shell tmux window (topic_created_handler), so the terminal
    exists before the user picks a project — the directory browser and
    session picker then drive that same window instead of spawning a
    second one (see _create_and_bind_window's reuse path).
  - Automatic cleanup: closing a topic kills the associated window
    (topic_closed_handler). Unsupported content (stickers, etc.)
    is rejected with a warning (unsupported_content_handler).
  - /new <project>: resolves a project name against the directory
    browser's own root (case-insensitive exact/prefix match), creates a
    tmux window running Claude there, and lets the mirror tick create and
    bind its topic (new_command) — usable from anywhere in the group.
  - /dashboard: force-recreates the pinned live terminal overview in the
    group's General topic (see dashboard.py), which is otherwise kept
    current by its own background poll loop.
  - /sleep additionally arms overnight-autonomy checkpointing, /wake
    disarms it and sends the morning report DM (see overnight.py).
  - Bot lifecycle management: post_init, post_shutdown, create_bot.

Handler modules (in handlers/):
  - callback_data: Callback data constants
  - message_queue: Per-user message queue management
  - message_sender: Safe message sending helpers
  - history: Message history pagination
  - directory_browser: Directory browser UI
  - interactive_ui: Interactive UI handling
  - status_polling: Terminal status polling
  - response_builder: Response message building

Key functions: create_bot(), handle_new_message().
"""

import asyncio
import io
import json
import logging
import subprocess
import sys
import httpx
import re
import shlex
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import config
from .handlers.callback_data import (
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
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_EFFORT_CANCEL,
    CB_EFFORT_SET,
    CB_HISTORY_NEXT,
    CB_HISTORY_PREV,
    CB_KILLALL_CANCEL,
    CB_KILLALL_CONFIRM,
    CB_SESSION_ALL,
    CB_SESSION_CANCEL,
    CB_SESSION_NEW,
    CB_SESSION_SELECT,
    CB_KEYS_PREFIX,
    CB_SCREENSHOT_REFRESH,
    CB_TERM_REFRESH,
    CB_WIN_BIND,
    CB_WIN_CANCEL,
    CB_WIN_NEW,
    CB_VOICE_CANCEL,
    CB_VOICE_SEND,
)
from .handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    SESSIONS_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_SESSION,
    STATE_SELECTING_WINDOW,
    UNBOUND_WINDOWS_KEY,
    build_directory_browser,
    build_session_picker,
    build_window_picker,
    clear_browse_state,
    clear_session_picker_state,
    clear_window_picker_state,
)
from .dashboard import dashboard_poll_loop, dashboard_tick, force_recreate
from .handlers.cleanup import clear_topic_state
from .handlers.dialog_fallback import (
    clear_fallback_msg,
    handle_unknown_dialog,
)
from .handlers.history import send_history
from .handlers.interactive_ui import (
    INTERACTIVE_TOOL_NAMES,
    clear_interactive_mode,
    clear_interactive_msg,
    get_interactive_msg_id,
    get_interactive_option_label,
    get_interactive_option_state,
    get_interactive_window,
    handle_interactive_ui,
    pop_pending_inline_edit,
    set_interactive_mode,
    set_pending_inline_edit,
)
from .handlers.message_queue import (
    clear_status_msg_info,
    enqueue_content_message,
    enqueue_status_update,
    enqueue_thinking_update,
    get_message_queue,
    reset_thinking_turn,
    shutdown_workers,
)
from .handlers.message_sender import (
    NO_LINK_PREVIEW,
    safe_edit,
    safe_reply,
    safe_send,
    send_with_fallback,
)
from .markdown_v2 import convert_markdown
from .mirror import mirror_poll_loop, mirror_tick
from .overnight import arm as overnight_arm
from .overnight import disarm_and_report as overnight_disarm_and_report
from .overnight import overnight_poll_loop
from .handlers.response_builder import build_response_parts
from .handlers.status_polling import status_poll_loop
from .screenshot import text_to_image
from .session import session_manager
from .session_monitor import NewMessage, SessionMonitor
from .terminal_parser import (
    extract_bash_output,
    format_pane_text_block,
    is_interactive_ui,
    parse_focused_option,
)
from .tmux_manager import tmux_manager
from .topic_titles import on_ai_title
from .transcribe import close_client as close_transcribe_client
from .transcribe import transcribe_voice
from .utils import ccbot_dir

logger = logging.getLogger(__name__)

# multiSelect options screen: the unnumbered, focused "Submit" pseudo-row
# below the last option — Enter there opens the review screen.
_RE_SUBMIT_ROW = re.compile(r"^\s*❯\s+Submit\s*$", re.MULTILINE)

# Session monitor instance
session_monitor: SessionMonitor | None = None

# Status polling task
_status_poll_task: asyncio.Task | None = None

# Auto-topic mirror polling task (only started when CCBOT_MIRROR_CHAT_ID is set)
_mirror_poll_task: asyncio.Task | None = None

# Live dashboard polling task (only started when CCBOT_MIRROR_CHAT_ID is set)
_dashboard_poll_task: asyncio.Task | None = None

# Overnight-autonomy snapshot polling task (see overnight.py) — started
# unconditionally, gated at runtime by session_manager.is_overnight_armed().
_overnight_poll_task: asyncio.Task | None = None

# Claude Code commands shown in bot menu (forwarded via tmux)
CC_COMMANDS: dict[str, str] = {
    "clear": "↗ Clear conversation history",
    "compact": "↗ Compact conversation context",
    "cost": "↗ Show token/cost usage",
    "help": "↗ Show Claude Code help",
    "memory": "↗ Edit CLAUDE.md",
    "model": "↗ Switch AI model",
    "sync": "↗ Sync the ~/mind shared brain now",
}


def is_user_allowed(user_id: int | None) -> bool:
    return user_id is not None and config.is_user_allowed(user_id)


def _get_thread_id(update: Update) -> int | None:
    """Extract thread_id from an update, returning None if not in a named topic."""
    msg = update.message or (
        update.callback_query.message if update.callback_query else None
    )
    if msg is None:
        return None
    tid = getattr(msg, "message_thread_id", None)
    if tid is None or tid == 1:
        return None
    return tid


# --- Command handlers ---


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    clear_browse_state(context.user_data)

    if update.message:
        await safe_reply(
            update.message,
            "🤖 *Claude Code Monitor*\n\n"
            "Each topic is a session. Create a new topic to start.",
        )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show message history for the active session or bound thread."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    await send_history(update.message, wid)


async def screenshot_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Capture the current tmux pane and send it as an image."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
    if not text:
        await safe_reply(update.message, "❌ Failed to capture pane content.")
        return

    png_bytes = await text_to_image(text, with_ansi=True)
    keyboard = _build_screenshot_keyboard(wid)
    await update.message.reply_document(
        document=io.BytesIO(png_bytes),
        filename="screenshot.png",
        reply_markup=keyboard,
    )


def _build_term_keyboard(window_id: str) -> InlineKeyboardMarkup:
    """Single refresh button for /term's text view."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data=f"{CB_TERM_REFRESH}{window_id}"[:64],
                )
            ]
        ]
    )


async def term_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the bound window's current pane text as a code block — text-first
    terminal view (see /screenshot for an actual picture)."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    text = await tmux_manager.capture_pane(w.window_id)
    if not text:
        await safe_reply(update.message, "❌ Failed to capture pane content.")
        return

    body = format_pane_text_block(text)
    keyboard = _build_term_keyboard(wid)
    await safe_reply(update.message, body, reply_markup=keyboard)


async def unbind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unbind this topic from its Claude session without killing the window."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "❌ This command only works in a topic.")
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    display = session_manager.get_display_name(wid)
    session_manager.unbind_thread(user.id, thread_id)
    await clear_topic_state(user.id, thread_id, context.bot, context.user_data)

    await safe_reply(
        update.message,
        f"✅ Topic unbound from window '{display}'.\n"
        "The Claude session is still running in tmux.\n"
        "Send a message to bind to a new session.",
    )


async def esc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send Escape key to interrupt Claude."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    # Send Escape control character (no enter)
    await tmux_manager.send_keys(w.window_id, "\x1b", enter=False)
    await safe_reply(update.message, "⎋ Sent Escape")


# Claude Code reasoning-effort levels, in ascending order. Kept in sync with
# the TUI's /effort dialog (verified 2026-07-05: `/effort <level>` is a valid
# one-shot — it applies immediately and persists as the account default).
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def _read_default_effort() -> str:
    """Current default effort from ~/.claude/settings.json ('' if unknown).

    Claude Code persists the last `/effort` choice as `effortLevel` there;
    per-session overrides aren't readable from outside the TUI, so this is
    the best "current" indicator available.
    """
    try:
        settings = json.loads(
            (Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
        return str(settings.get("effortLevel", ""))
    except (OSError, json.JSONDecodeError):
        return ""


def _build_effort_keyboard() -> InlineKeyboardMarkup:
    current = _read_default_effort()
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for level in EFFORT_LEVELS:
        label = f"✓ {level}" if level == current else level
        row.append(InlineKeyboardButton(label, callback_data=f"{CB_EFFORT_SET}{level}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Cancel", callback_data=CB_EFFORT_CANCEL)])
    return InlineKeyboardMarkup(rows)


async def effort_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pick a reasoning-effort level for this topic's Claude session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    current = _read_default_effort()
    current_line = f"\ncurrent default: `{current}`" if current else ""
    await safe_reply(
        update.message,
        f"*Effort level*{current_line}\n\nPick the reasoning effort for this session:",
        reply_markup=_build_effort_keyboard(),
    )


async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch Claude Code usage stats from TUI and send to Telegram."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "No session bound to this topic.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        await safe_reply(update.message, f"Window '{wid}' no longer exists.")
        return

    # Send /usage command to Claude Code TUI
    await tmux_manager.send_keys(w.window_id, "/usage")
    # Wait for the modal to render
    await asyncio.sleep(2.0)
    # Capture the pane content
    pane_text = await tmux_manager.capture_pane(w.window_id)
    # Dismiss the modal
    await tmux_manager.send_keys(w.window_id, "Escape", enter=False, literal=False)

    if not pane_text:
        await safe_reply(update.message, "Failed to capture usage info.")
        return

    # Try to parse structured usage info
    from .terminal_parser import parse_usage_output

    usage = parse_usage_output(pane_text)
    if usage and usage.parsed_lines:
        text = "\n".join(usage.parsed_lines)
        await safe_reply(update.message, f"```\n{text}\n```")
    else:
        # Fallback: send raw pane capture trimmed
        trimmed = pane_text.strip()
        if len(trimmed) > 3000:
            trimmed = trimmed[:3000] + "\n... (truncated)"
        await safe_reply(update.message, f"```\n{trimmed}\n```")


async def lock_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Engage the lock kill switch: gate every inbound update except /unlock."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return
    session_manager.set_locked(True)
    await safe_reply(update.message, "🔒 Locked. Send /unlock to resume.")


async def unlock_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Release the lock kill switch."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return
    session_manager.set_locked(False)
    await safe_reply(update.message, "🔓 Unlocked.")


_DEFAULT_WAKE_HHMM = "09:15"


def _parse_hhmm(raw: str) -> tuple[int, int] | None:
    """Parse an 'HH:MM' string; returns None if malformed or out of range."""
    try:
        hh, mm = raw.split(":", 1)
        hour, minute = int(hh), int(mm)
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _next_wake_time(hour: int, minute: int, now: datetime | None = None) -> datetime:
    """Next occurrence of HH:MM — today if still ahead, else tomorrow."""
    now = now or datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


async def sleep_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/sleep [HH:MM]: write a wake-time epoch to ~/.ccbot/quiet-until.

    ccbot itself does not gate anything on this file — it exists purely for
    an external pager script (out of repo scope) to read and decide whether
    to page. No argument defaults to the next 09:15.
    """
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    raw_arg = parts[1].strip() if len(parts) > 1 else _DEFAULT_WAKE_HHMM
    parsed = _parse_hhmm(raw_arg)
    if parsed is None:
        await safe_reply(
            update.message, "❌ Invalid time, use HH:MM (e.g. /sleep 09:30)"
        )
        return

    wake = _next_wake_time(*parsed)
    quiet_until_file = ccbot_dir() / "quiet-until"
    quiet_until_file.parent.mkdir(parents=True, exist_ok=True)
    quiet_until_file.write_text(str(int(wake.timestamp())))
    (ccbot_dir() / "quiet-override").unlink(missing_ok=True)
    overnight_arm()

    await safe_reply(update.message, f"😴 Quiet until {wake.strftime('%H:%M')}")


async def wake_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/wake: cancel quiet hours by deleting ~/.ccbot/quiet-until. If
    overnight mode was armed, disarm it and DM the morning report first —
    see overnight.py (the poll loop itself auto-disarms+reports if this
    command is never sent)."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    (ccbot_dir() / "quiet-until").unlink(missing_ok=True)
    if session_manager.is_overnight_armed():
        await overnight_disarm_and_report(context.bot)
    await safe_reply(update.message, "☀️ Awake — pings back on")


# /unmute pierces the pager's quiet gates (dynamic quiet-until + the static
# night window) for this many hours — self-expiring, so the next night's
# quiet hours work again without any action. Written as an epoch to
# ~/.ccbot/quiet-override; /mute and /sleep delete it (explicit silence wins).
_QUIET_OVERRIDE_HOURS = 8


def _quiet_override_epoch() -> int | None:
    """Active quiet-override expiry epoch, or None if absent/expired."""
    try:
        epoch = int((ccbot_dir() / "quiet-override").read_text().strip())
    except (OSError, ValueError):
        return None
    return epoch if epoch > time.time() else None


def _mute_status_text() -> str:
    """Current mute state, read straight from the flag files (source of
    truth for the external pager script too)."""
    mac = (
        "🔇 Mac sounds muted"
        if (ccbot_dir() / "mute-mac").exists()
        else "🔊 Mac sounds on"
    )
    phone = (
        "phone pings muted"
        if (ccbot_dir() / "mute-phone").exists()
        else "phone pings on"
    )
    text = f"{mac} · {phone}"
    override = _quiet_override_epoch()
    if override is not None:
        until = datetime.fromtimestamp(override).strftime("%H:%M")
        text += f"\n🔓 Quiet-hours override active until {until}"
    return text


def _parse_mute_arg(text: str) -> str | None:
    """Parse the optional mac|phone argument. Returns None if invalid."""
    parts = text.split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""
    return arg if arg in ("", "mac", "phone") else None


async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mute [mac|phone]: touch a flag file in ccbot_dir() that the external
    pager script (~/.claude/notify-pager.sh) checks before playing a Mac
    sound / sending a Telegram DM. No argument mutes both channels."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    arg = _parse_mute_arg(update.message.text or "")
    if arg is None:
        await safe_reply(update.message, "❌ Usage: /mute [mac|phone]")
        return

    ccbot_dir().mkdir(parents=True, exist_ok=True)
    if arg in ("", "mac"):
        (ccbot_dir() / "mute-mac").touch()
    if arg in ("", "phone"):
        (ccbot_dir() / "mute-phone").touch()
    # Explicit mute cancels any earlier /unmute quiet-hours override.
    (ccbot_dir() / "quiet-override").unlink(missing_ok=True)

    await safe_reply(update.message, _mute_status_text())


async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/unmute [mac|phone]: reverse of /mute. No argument unmutes both."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    arg = _parse_mute_arg(update.message.text or "")
    if arg is None:
        await safe_reply(update.message, "❌ Usage: /unmute [mac|phone]")
        return

    if arg in ("", "mac"):
        (ccbot_dir() / "mute-mac").unlink(missing_ok=True)
    if arg in ("", "phone"):
        (ccbot_dir() / "mute-phone").unlink(missing_ok=True)
    # Unmute must mean unmute: pierce the pager's quiet gates (dynamic
    # quiet-until AND the static night window) for the next few hours.
    # Self-expires so tomorrow night's quiet hours still apply.
    ccbot_dir().mkdir(parents=True, exist_ok=True)
    (ccbot_dir() / "quiet-until").unlink(missing_ok=True)
    (ccbot_dir() / "quiet-override").write_text(
        str(int(time.time() + _QUIET_OVERRIDE_HOURS * 3600))
    )

    await safe_reply(update.message, _mute_status_text())


def _read_account_info() -> str:
    """Which Claude account this HOST is logged into (file-based default).
    Email/org from ~/.claude.json; plan tier from ~/.claude/.credentials.json
    (Linux only — macOS keeps credentials in the Keychain, so tier is omitted
    there). Sessions launched with a CLAUDE_CODE_OAUTH_TOKEN env override may
    run as a different account; this reports what plain `claude` uses."""
    email = org = tier = None
    try:
        data = json.loads((Path.home() / ".claude.json").read_text())
        oauth_account = data.get("oauthAccount") or {}
        email = oauth_account.get("emailAddress")
        org = oauth_account.get("organizationName")
    except (OSError, ValueError):
        pass
    # Tier: on macOS the Keychain is the live source — the file, if present,
    # can be a stale leftover from token setups (seen 2026-07-05: file said
    # "pro"/expired while the Keychain held "max"). File is Linux's source.
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-s",
                    "Claude Code-credentials",
                    "-w",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if out.returncode == 0:
                tier = (json.loads(out.stdout).get("claudeAiOauth") or {}).get(
                    "subscriptionType"
                )
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    if not tier:
        try:
            creds = json.loads(
                (Path.home() / ".claude" / ".credentials.json").read_text()
            )
            tier = (creds.get("claudeAiOauth") or {}).get("subscriptionType")
        except (OSError, ValueError):
            pass
    if not email:
        return "❌ No Claude login found on this machine (~/.claude.json)"
    lines = [f"👤 Account: {email}"]
    if org:
        lines.append(f"🏢 Org: {org}")
    if tier:
        lines.append(f"📦 Plan: {tier}")
    lines.append("(host default — env-token sessions may differ)")
    return "\n".join(lines)


async def account_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/account: show which Claude account this host's sessions run on."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return
    await safe_reply(update.message, _read_account_info())


# /killall confirmation TTL and pending state (see callback_handler's
# CB_KILLALL_CONFIRM/CANCEL branch). Keyed by user_id -> time.monotonic()
# of the prompt — single-user bot, but keyed defensively rather than a bare
# global flag.
_KILLALL_TTL_SECONDS = 60.0
_pending_killall: dict[int, float] = {}


async def killall_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Panic button: kill every tmux window in the ccbot session.

    Destructive — requires a Kill all/Cancel confirmation tap (handled in
    callback_handler) before anything is actually killed. Topics are left
    alone; the mirror's next tick deletes them once their windows are gone.
    """
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    windows = await tmux_manager.list_windows()
    n = len(windows)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💀 Kill all", callback_data=CB_KILLALL_CONFIRM),
                InlineKeyboardButton("Cancel", callback_data=CB_KILLALL_CANCEL),
            ]
        ]
    )
    await safe_reply(
        update.message, f"⚠️ Kill all {n} terminals?", reply_markup=keyboard
    )
    _pending_killall[user.id] = time.monotonic()


_GRAB_MAX_BYTES = 50 * 1024 * 1024  # Telegram bot upload limit


def _grab_target(raw_path: str, cwd: str) -> tuple[Path | None, str | None]:
    """Resolve /grab's <path> and run every trust-boundary guard.

    <path> is resolved against the bound window's pane cwd unless it's
    already absolute. Returns (resolved_path, None) on success, or
    (None, reason) with a short user-facing rejection reason.

    This crosses a trust boundary (Telegram user input -> local filesystem
    read -> upload), so every guard checks the FINAL resolved path (after
    resolve() has followed any symlinks) — not the raw input:
      - must live under the home directory
      - must not resolve into ~/.ccbot (holds the .env with the bot token)
      - no path component may start with "." anywhere in the chain (blocks
        .ssh, .aws, .env, etc., not just a top-level dotdir)
      - must exist, must be a regular file, must be <= 50MB
    Do not weaken these checks.
    """
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    resolved = candidate.resolve()

    home = Path.home().resolve()
    try:
        rel = resolved.relative_to(home)
    except ValueError:
        return None, "outside home"

    ccbot_state_dir = ccbot_dir().expanduser().resolve()
    if resolved == ccbot_state_dir or ccbot_state_dir in resolved.parents:
        return None, f"denied (ccbot config dir): {resolved}"

    if any(part.startswith(".") for part in rel.parts):
        return None, f"denied (hidden path): {resolved}"

    if not resolved.exists():
        return None, f"not found: {resolved}"
    if not resolved.is_file():
        return None, f"not a regular file: {resolved}"

    size = resolved.stat().st_size
    if size > _GRAB_MAX_BYTES:
        return None, f"too large ({size / 1_048_576:.1f} MB > 50 MB): {resolved}"

    return resolved, None


# --- /speak: read the last answer aloud in the cloned voice ------------------

TTS_SPEAK_URL = "http://127.0.0.1:8838/speak"
TTS_MAX_CHARS = 800

# Long answers get a spoken-style summary before TTS instead of being read
# in full, so voice notes stay short. Threshold/limits are chosen so both
# the validated CLI summary and the extractive fallback land well under
# TTS_MAX_CHARS.
SUMMARIZE_THRESHOLD_CHARS = 300
SUMMARY_MAX_CHARS = 600
FALLBACK_MAX_CHARS = 250
SUMMARY_CLI_BIN = "/Users/mogaeduard/.local/bin/claude"
SUMMARY_MODEL = "claude-haiku-4-5-20251001"
SUMMARY_TIMEOUT_S = 45.0
_LANGUAGE_NAMES = {"ro": "Romanian", "en": "English"}
SUMMARY_PROMPT = (
    "Summarize the following assistant answer in 1-3 short spoken-style "
    "sentences. Respond ONLY in {language_name} — translate any "
    "foreign-language fragments into {language_name} too. No markdown, no "
    "emoji, no preamble — just the sentences. ANSWER: {text}"
)

# Romanian diacritics (both comma-below and legacy cedilla forms) and a
# closed set of very common Romanian stopwords, matched as whole words.
_RO_DIACRITICS = frozenset("ăâîșțĂÂÎȘȚşţŞŢ")
_RO_STOPWORDS = {"și", "este", "pentru", "să", "mai", "nu", "cu", "de", "la", "un", "o"}
_RO_STOPWORD_RE = re.compile(r"\b(" + "|".join(_RO_STOPWORDS) + r")\b", re.IGNORECASE)


def detect_language_ro_en(text: str) -> str:
    """Detect Romanian vs English for /speak's TTS language hint.

    Romanian if the text has any Romanian diacritic or a whole-word
    Romanian stopword match; English otherwise. Used to tell both the
    Haiku summarizer and the TTS server which language to speak in,
    instead of leaving either to re-guess."""
    if any(ch in _RO_DIACRITICS for ch in text):
        return "ro"
    if _RO_STOPWORD_RE.search(text):
        return "ro"
    return "en"


def last_assistant_text(messages: list[dict]) -> str | None:
    """Pick the most recent assistant text message worth speaking."""
    for m in reversed(messages):
        if (
            m.get("role") == "assistant"
            and m.get("content_type") == "text"
            and (m.get("text") or "").strip()
        ):
            return m["text"].strip()
    return None


def should_summarize(text: str) -> bool:
    """Only long answers pay for a summarization pass; short ones are
    spoken as-is."""
    return len(text) > SUMMARIZE_THRESHOLD_CHARS


def extractive_fallback(text: str, max_chars: int = FALLBACK_MAX_CHARS) -> str:
    """Naive fallback summary: first 1-2 sentences, hard-capped at
    max_chars, with a trailing "…" whenever something was cut off."""
    stripped = text.strip()
    sentences = re.split(r"(?<=[.!?])\s+", stripped)
    summary = " ".join(sentences[:2]).strip()
    truncated = len(sentences) > 2
    if len(summary) > max_chars:
        summary = summary[: max_chars - 1].rstrip()
        truncated = True
    return summary + "…" if truncated else summary


def _is_valid_summary(summary: str) -> bool:
    """A CLI summary is usable if non-empty and within SUMMARY_MAX_CHARS
    after stripping."""
    stripped = summary.strip()
    return bool(stripped) and len(stripped) <= SUMMARY_MAX_CHARS


async def _summarize_via_claude_cli(text: str, language: str = "en") -> str | None:
    """Shell out to the Claude CLI headlessly for a 1-3 sentence
    spoken-style summary in the given language ("ro"/"en" — see
    detect_language_ro_en). --settings disableAllHooks is required so the
    user's own Stop-hook pager doesn't fire a DM for this internal call.
    Returns None on any failure (binary missing, timeout, non-zero exit) —
    the caller falls back to extractive_fallback."""
    language_name = _LANGUAGE_NAMES.get(language, "English")
    try:
        proc = await asyncio.create_subprocess_exec(
            SUMMARY_CLI_BIN,
            "-p",
            "--model",
            SUMMARY_MODEL,
            "--settings",
            '{"disableAllHooks":true}',
            SUMMARY_PROMPT.format(language_name=language_name, text=text),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        logger.warning("/speak: claude CLI unavailable for summary: %s", e)
        return None

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=SUMMARY_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        logger.warning("/speak: claude CLI summary timed out")
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return None

    if proc.returncode != 0:
        logger.warning(
            "/speak: claude CLI summary exited %s: %s",
            proc.returncode,
            stderr.decode("utf-8", errors="replace").strip(),
        )
        return None

    return stdout.decode("utf-8", errors="replace").strip()


async def summarize_for_speech(text: str, language: str = "en") -> str:
    """1-3 sentence spoken-style summary of `text` for /speak, in the given
    language — the Claude CLI when it produces something valid, otherwise a
    naive extractive fallback. Only meant to be called when
    should_summarize(text) is True."""
    summary = await _summarize_via_claude_cli(text, language)
    if summary is not None and _is_valid_summary(summary):
        return summary.strip()
    return extractive_fallback(text)


async def speak_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/speak: synthesize the topic's last answer as a voice note (local
    XTTS server with the cloned voice; RO+EN). Long answers are summarized
    to 1-3 spoken-style sentences first — see summarize_for_speech."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return
    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "❌ Use /speak inside a terminal topic.")
        return
    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid is None:
        await safe_reply(update.message, "❌ No terminal bound to this topic.")
        return

    messages, _ = await session_manager.get_recent_messages(wid)
    text = last_assistant_text(messages)
    if not text:
        await safe_reply(update.message, "🔇 Nothing to speak yet in this terminal.")
        return

    # Optional speaking-speed argument: "/speak 0.8" or "/speak 1.5x"
    # (0.5-2.0; the TTS server rescales the model's duration prediction, so
    # pitch is untouched). Any non-numeric argument is ignored, like before
    # the speed feature existed — "/speak now" must keep producing audio,
    # not a usage lecture.
    speed: float | None = None
    if context.args:
        m = re.fullmatch(r"(\d+(?:[.,]\d+)?)x?", context.args[0].strip().lower())
        if m:
            speed = min(2.0, max(0.5, float(m.group(1).replace(",", "."))))

    # Detect on the raw answer, before summarizing shortens/paraphrases it —
    # both the summarizer and the TTS server are then told the language
    # explicitly instead of re-guessing.
    language = detect_language_ro_en(text)

    try:
        await update.message.chat.send_action(
            ChatAction.RECORD_VOICE, message_thread_id=thread_id
        )
    except Exception:  # cosmetic only
        pass

    if should_summarize(text):
        text = await summarize_for_speech(text, language)
    if len(text) > TTS_MAX_CHARS:
        text = text[: TTS_MAX_CHARS - 1] + "…"

    try:
        client = httpx.AsyncClient(timeout=120.0)
        try:
            payload: dict[str, str | float] = {"text": text, "language": language}
            if speed is not None:
                payload["speed"] = speed
            resp = await client.post(TTS_SPEAK_URL, json=payload)
            resp.raise_for_status()
            audio = resp.content
        finally:
            await client.aclose()
    except Exception as e:
        logger.error("/speak: TTS request failed: %s", e)
        await safe_reply(
            update.message, "⚠ Voice server unavailable (is claude-tts running?)"
        )
        return

    try:
        await update.message.reply_voice(voice=audio, message_thread_id=thread_id)
    except Exception as e:
        logger.error("/speak: sending voice failed: %s", e)
        await safe_reply(update.message, f"⚠ Could not send voice note: {e}")


async def grab_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/grab <path>: send a file from the bound window's cwd (or an absolute
    path) into this topic as a document. See _grab_target for the security
    guards — this reads arbitrary paths named by a Telegram message."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await safe_reply(update.message, "Usage: /grab <path>")
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    resolved, reason = _grab_target(parts[1].strip(), w.cwd)
    if reason is not None:
        await safe_reply(update.message, f"❌ {reason}")
        return

    assert resolved is not None
    await update.message.reply_document(document=str(resolved), filename=resolved.name)


async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-recreate the pinned live dashboard in the group's General topic."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return
    if not config.mirror_chat_id:
        await safe_reply(update.message, "❌ Dashboard requires CCBOT_MIRROR_CHAT_ID.")
        return
    await force_recreate(context.bot)
    await safe_reply(update.message, "📌 Dashboard recreated.")


def _resolve_project(name: str, subdirs: list[str]) -> tuple[str | None, list[str]]:
    """Resolve a project name against directory names, case-insensitively.

    Tries an exact match first, then a unique prefix match. Returns
    (resolved_name, candidates): resolved_name is set on a clean match;
    candidates lists every prefix match when the name is ambiguous
    (empty on a clean match or no match at all).
    """
    lname = name.lower()
    for d in subdirs:
        if d.lower() == lname:
            return d, []
    prefix_matches = [d for d in subdirs if d.lower().startswith(lname)]
    if len(prefix_matches) == 1:
        return prefix_matches[0], []
    return None, prefix_matches


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/new <project>: start Claude in a project dir, usable anywhere in the group.

    Resolves <project> against the directory browser's own root (its
    starting directory — see build_directory_browser / text_handler), then
    creates a tmux window there running the configured claude command. The
    topic itself is created and bound by the next mirror tick, not here —
    creating it eagerly would race the mirror's own topic-creation pass.
    """
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    chat = update.effective_chat
    thread_id = _get_thread_id(update)
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await safe_reply(update.message, "Usage: /new <project>")
        return
    project = parts[1].strip()

    root = Path.cwd()
    _, _, subdirs = build_directory_browser(str(root))
    resolved, candidates = _resolve_project(project, subdirs)
    if resolved is None:
        if candidates:
            listing = ", ".join(sorted(candidates))
            await safe_reply(
                update.message, f"❌ Ambiguous project '{project}': {listing}"
            )
        else:
            await safe_reply(update.message, f"❌ No project matching '{project}'")
        return

    success, message, _wname, _wid = await tmux_manager.create_window(
        str(root / resolved), start_claude=True
    )
    if not success:
        await safe_reply(update.message, f"❌ {message}")
        return

    await safe_reply(
        update.message, f"🚀 {resolved} starting — topic appears in a few seconds"
    )


# --- Lock gate: runs before every other handler (see registration in
# create_bot — group=-1 fires first) ---

_LOCK_REPLY_MIN_INTERVAL_SECONDS = 60.0
_last_lock_reply_ts: float = 0.0


async def lock_gate_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """While locked, swallow every update except /unlock before it reaches
    any other handler.

    Registered at group=-1 for both message and callback-query updates, so
    it always runs first; raising ApplicationHandlerStop prevents every
    later-group handler (text/voice/photo/command/callback) from running at
    all — none of the terminal-injection call sites ever execute. The
    "🔒 locked" reply is rate-limited to once a minute so a compromised
    account can't spam it either. Background loops (status polling, session
    monitor, mirror) are separate asyncio tasks entirely outside Telegram's
    update dispatch, so they, and outbound flow generally, are unaffected.
    """
    global _last_lock_reply_ts
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not session_manager.is_locked():
        return

    if update.message and update.message.text:
        first_word = update.message.text.split(maxsplit=1)[0].split("@")[0]
        if first_word == "/unlock":
            return  # let unlock_command handle it normally

    now = time.monotonic()
    should_reply = now - _last_lock_reply_ts >= _LOCK_REPLY_MIN_INTERVAL_SECONDS
    if should_reply:
        _last_lock_reply_ts = now

    if update.callback_query:
        # Always ack (dismisses the button's loading spinner); only include
        # the visible alert text within the rate-limit budget.
        await update.callback_query.answer(
            "🔒 locked" if should_reply else None, show_alert=should_reply
        )
    elif update.message and should_reply:
        await safe_reply(update.message, "🔒 locked")

    raise ApplicationHandlerStop


# --- Screenshot keyboard with quick control keys ---

# key_id → (tmux_key, enter, literal)
_KEYS_SEND_MAP: dict[str, tuple[str, bool, bool]] = {
    "up": ("Up", False, False),
    "dn": ("Down", False, False),
    "lt": ("Left", False, False),
    "rt": ("Right", False, False),
    "esc": ("Escape", False, False),
    "ent": ("Enter", False, False),
    "spc": ("Space", False, False),
    "tab": ("Tab", False, False),
    "cc": ("C-c", False, False),
}

# key_id → display label (shown in callback answer toast)
_KEY_LABELS: dict[str, str] = {
    "up": "↑",
    "dn": "↓",
    "lt": "←",
    "rt": "→",
    "esc": "⎋ Esc",
    "ent": "⏎ Enter",
    "spc": "␣ Space",
    "tab": "⇥ Tab",
    "cc": "^C",
}


def _build_screenshot_keyboard(window_id: str) -> InlineKeyboardMarkup:
    """Build inline keyboard for screenshot: control keys + refresh."""

    def btn(label: str, key_id: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            label,
            callback_data=f"{CB_KEYS_PREFIX}{key_id}:{window_id}"[:64],
        )

    return InlineKeyboardMarkup(
        [
            [btn("␣ Space", "spc"), btn("↑", "up"), btn("⇥ Tab", "tab")],
            [btn("←", "lt"), btn("↓", "dn"), btn("→", "rt")],
            [btn("⎋ Esc", "esc"), btn("^C", "cc"), btn("⏎ Enter", "ent")],
            [
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data=f"{CB_SCREENSHOT_REFRESH}{window_id}"[:64],
                )
            ],
        ]
    )


async def topic_created_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle a new forum topic — bind a plain-shell window immediately.

    Telegram gives no event for topic *deletion* (see message_sender.py's
    deleted-thread detection), so ccbot can't afford to wait for the user's
    first text message to react to topic *creation* either — a terminal
    must exist as soon as the topic does, keeping the tmux-windows-set and
    bound-topics-set identical at every mirror tick. The directory browser
    is then posted as the first message so the user can still pick a
    project or resume a session; its callback handlers drive the same
    window instead of creating a second one (see _create_and_bind_window).

    This fires for EVERY forum_topic_created service message, including
    ones create_forum_topic() itself generates for mirror-created topics
    (mirror.py) — those are already bound synchronously by the mirror
    before this update is even delivered, so bail out early rather than
    create a redundant second shell window and steal the binding.
    """
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    msg = update.message
    if not msg or not msg.forum_topic_created:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        return

    if session_manager.get_window_for_thread(user.id, thread_id) is not None:
        return  # already bound (e.g. mirror-created topic) — nothing to do

    topic_name = msg.forum_topic_created.name
    success, message, wname, wid = await tmux_manager.create_window(
        str(Path.home()), window_name=topic_name, start_claude=False
    )
    if not success:
        logger.error("Topic created: failed to create shell window: %s", message)
        await safe_reply(msg, f"⚠ Failed to create terminal: {message}")
        return

    # Only record routing once the window exists — recording it before a
    # failed create leaked a group_chat_ids entry with no binding behind it.
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    session_manager.bind_thread(user.id, thread_id, wid, window_name=wname)
    logger.info(
        "Topic created (phone): bound shell window %s ('%s') to thread %d",
        wid,
        wname,
        thread_id,
    )

    w = await tmux_manager.find_window_by_id(wid)
    label = f"terminal {w.window_index}" if w and w.window_index else f"'{wname}'"
    start_path = str(Path.cwd())
    browser_text, keyboard, subdirs = build_directory_browser(start_path)
    if context.user_data is not None:
        context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
        context.user_data[BROWSE_PATH_KEY] = start_path
        context.user_data[BROWSE_PAGE_KEY] = 0
        context.user_data[BROWSE_DIRS_KEY] = subdirs
        context.user_data["_pending_thread_id"] = thread_id
    await safe_reply(
        msg,
        f"🖥 {label} is live — pick a project to start Claude, or just type\n\n"
        + browser_text,
        reply_markup=keyboard,
    )


async def topic_closed_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle topic closure — kill the associated tmux window and clean up state."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid:
        display = session_manager.get_display_name(wid)
        w = await tmux_manager.find_window_by_id(wid)
        if w:
            await tmux_manager.kill_window(w.window_id)
            logger.info(
                "Topic closed: killed window %s (user=%d, thread=%d)",
                display,
                user.id,
                thread_id,
            )
        else:
            logger.info(
                "Topic closed: window %s already gone (user=%d, thread=%d)",
                display,
                user.id,
                thread_id,
            )
        session_manager.unbind_thread(user.id, thread_id)
        # Clean up all memory state for this topic
        await clear_topic_state(user.id, thread_id, context.bot, context.user_data)
    else:
        logger.debug(
            "Topic closed: no binding (user=%d, thread=%d)", user.id, thread_id
        )


async def topic_edited_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle topic rename — sync new name to tmux window and internal state."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    msg = update.message
    if not msg or not msg.forum_topic_edited:
        return

    new_name = msg.forum_topic_edited.name
    if new_name is None:
        # Icon-only change, no rename needed
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if not wid:
        logger.debug(
            "Topic edited: no binding (user=%d, thread=%d)", user.id, thread_id
        )
        return

    old_name = session_manager.get_display_name(wid)
    await tmux_manager.rename_window(wid, new_name)
    session_manager.update_display_name(wid, new_name)
    logger.info(
        "Topic renamed: '%s' -> '%s' (window=%s, user=%d, thread=%d)",
        old_name,
        new_name,
        wid,
        user.id,
        thread_id,
    )


async def forward_command_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Forward any non-bot command as a slash command to the active Claude Code session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    cmd_text = update.message.text or ""
    # The full text is already a slash command like "/clear" or "/compact foo"
    cc_slash = cmd_text.split("@")[0]  # strip bot mention
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    display = session_manager.get_display_name(wid)
    logger.info(
        "Forwarding command %s to window %s (user=%d)", cc_slash, display, user.id
    )
    try:
        await update.message.chat.send_action(ChatAction.TYPING)
    except Exception as e:
        logger.warning("send_action(TYPING) failed, continuing to injection: %s", e)
    success, message = await session_manager.send_to_window(wid, cc_slash)
    if success:
        await safe_reply(update.message, f"⚡ [{display}] Sent: {cc_slash}")
        # If /clear command was sent, clear the session association
        # so we can detect the new session after first message
        if cc_slash.strip().lower() == "/clear":
            logger.info("Clearing session for window %s after /clear", display)
            session_manager.clear_window_session(wid)

        # Interactive commands (e.g. /model) render a terminal-based UI
        # with no JSONL tool_use entry.  The status poller already detects
        # interactive UIs every 1s (status_polling.py), so no
        # proactive detection needed here — the poller handles it.
    else:
        await safe_reply(update.message, f"❌ {message}")


async def unsupported_content_handler(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Reply to non-text messages (stickers, video, etc.)."""
    if not update.message:
        return
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    logger.debug("Unsupported content from user %d", user.id)
    await safe_reply(
        update.message,
        "⚠ Only text, photo, and voice messages are supported. Stickers, video, and other media cannot be forwarded to Claude Code.",
    )


# --- Image directory for incoming photos ---
_IMAGES_DIR = ccbot_dir() / "images"
_IMAGES_DIR.mkdir(parents=True, exist_ok=True)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photos sent by the user: download and forward path to Claude Code."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.photo:
        return

    chat = update.message.chat
    thread_id = _get_thread_id(update)
    if chat.type in ("group", "supergroup") and thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    # Must be in a named topic
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid is None:
        await safe_reply(
            update.message,
            "❌ No session bound to this topic. Send a text message first to create one.",
        )
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        session_manager.unbind_thread(user.id, thread_id)
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists. Binding removed.\n"
            "Send a message to start a new session.",
        )
        return

    # Download the highest-resolution photo
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()

    # Save to ~/.ccbot/images/<timestamp>_<file_unique_id>.jpg
    filename = f"{int(time.time())}_{photo.file_unique_id}.jpg"
    file_path = _IMAGES_DIR / filename
    await tg_file.download_to_drive(file_path)

    # Build the message to send to Claude Code
    caption = update.message.caption or ""
    if caption:
        text_to_send = f"{caption}\n\n(image attached: {file_path})"
    else:
        text_to_send = f"(image attached: {file_path})"

    try:
        await update.message.chat.send_action(ChatAction.TYPING)
    except Exception as e:
        logger.warning("send_action(TYPING) failed, continuing to injection: %s", e)
    clear_status_msg_info(user.id, thread_id)

    success, message = await session_manager.send_to_window(wid, text_to_send)
    if not success:
        await safe_reply(update.message, f"❌ {message}")
        return

    # Confirm to user
    await safe_reply(update.message, "📷 Image sent to Claude Code.")


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice messages: transcribe via OpenAI and forward text to Claude Code."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.voice:
        return

    if not config.openai_api_key:
        await safe_reply(
            update.message,
            "⚠ Voice transcription requires an OpenAI API key.\n"
            "Set `OPENAI_API_KEY` in your `.env` file and restart the bot.",
        )
        return

    chat = update.message.chat
    thread_id = _get_thread_id(update)
    if chat.type in ("group", "supergroup") and thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid is None:
        await safe_reply(
            update.message,
            "❌ No session bound to this topic. Send a text message first to create one.",
        )
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        session_manager.unbind_thread(user.id, thread_id)
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists. Binding removed.\n"
            "Send a message to start a new session.",
        )
        return

    # Download voice as in-memory bytes
    voice_file = await update.message.voice.get_file()
    ogg_data = bytes(await voice_file.download_as_bytearray())

    # Transcribe
    try:
        text = await transcribe_voice(ogg_data)
    except ValueError as e:
        await safe_reply(update.message, f"⚠ {e}")
        return
    except Exception as e:
        logger.error("Voice transcription failed: %s", e)
        await safe_reply(update.message, f"⚠ Transcription failed: {e}")
        return

    # Confirm-first: show the transcript with Send/Cancel instead of
    # injecting straight away — an ASR slip must never become a command.
    # A new voice note supersedes any earlier un-actioned one in this topic.
    await _expire_pending_voice(context.bot, user.id, thread_id)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Send", callback_data=CB_VOICE_SEND),
                InlineKeyboardButton("❌ Cancel", callback_data=CB_VOICE_CANCEL),
            ]
        ]
    )
    sent = await update.message.reply_text(
        f'🎤 "{text}"',
        reply_markup=keyboard,
        message_thread_id=thread_id,
    )
    _set_pending_voice(user.id, thread_id, text, sent.message_id if sent else None)


# Pending voice transcripts awaiting Send/Cancel, keyed by (user_id, thread_id).
# TTL guards against a Send tapped hours later injecting a stale transcript
# into whatever the terminal happens to be doing by then.
_PENDING_VOICE_TTL_SECONDS = 300.0  # 5 minutes
# Belt-and-braces cap so a burst of un-actioned voice notes can't grow this
# dict unbounded — oldest entry is dropped to make room.
_PENDING_VOICE_MAX_ENTRIES = 50


@dataclass
class _PendingVoice:
    text: str
    ts: float  # time.monotonic() at creation, for TTL/age checks
    message_id: int | None  # the "🎤 ..." prompt message, for expiry edits


_pending_voice: dict[tuple[int, int], _PendingVoice] = {}


def _set_pending_voice(
    user_id: int, thread_id: int, text: str, message_id: int | None
) -> None:
    """Store a new pending voice transcript, evicting the oldest entry first
    if the dict is at capacity."""
    if len(_pending_voice) >= _PENDING_VOICE_MAX_ENTRIES:
        oldest_key = min(_pending_voice, key=lambda k: _pending_voice[k].ts)
        _pending_voice.pop(oldest_key, None)
    _pending_voice[(user_id, thread_id)] = _PendingVoice(
        text=text, ts=time.monotonic(), message_id=message_id
    )


async def _expire_pending_voice(bot: Bot, user_id: int, thread_id: int) -> None:
    """Drop any pending voice transcript for (user_id, thread_id).

    Best-effort edits its prompt message to show expired — a new voice note
    or text message in the same topic makes the old "🎤 ..." Send/Cancel
    prompt stale, so tapping Send on it later must not inject anything.
    """
    entry = _pending_voice.pop((user_id, thread_id), None)
    if entry is None or entry.message_id is None:
        return
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=entry.message_id,
            text="🎤 expired — resend the voice note",
        )
    except Exception:
        pass  # message already edited/deleted/too old — entry is dropped regardless


# Active bash capture tasks: (user_id, thread_id) → asyncio.Task
_bash_capture_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}


def _cancel_bash_capture(user_id: int, thread_id: int) -> None:
    """Cancel any running bash capture for this topic."""
    key = (user_id, thread_id)
    task = _bash_capture_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()


async def _capture_bash_output(
    bot: Bot,
    user_id: int,
    thread_id: int,
    window_id: str,
    command: str,
) -> None:
    """Background task: capture ``!`` bash command output from tmux pane.

    Sends the first captured output as a new message, then edits it
    in-place as more output appears.  Stops after 30 s or when cancelled
    (e.g. user sends a new message, which pushes content down).
    """
    try:
        # Wait for the command to start producing output
        await asyncio.sleep(2.0)

        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        msg_id: int | None = None
        last_output: str = ""

        for _ in range(30):
            raw = await tmux_manager.capture_pane(window_id)
            if raw is None:
                return

            output = extract_bash_output(raw, command)
            if not output:
                await asyncio.sleep(1.0)
                continue

            # Skip edit if nothing changed
            if output == last_output:
                await asyncio.sleep(1.0)
                continue

            last_output = output

            # Truncate to fit Telegram's 4096-char limit
            if len(output) > 3800:
                output = "… " + output[-3800:]

            if msg_id is None:
                # First capture — send a new message
                sent = await send_with_fallback(
                    bot,
                    chat_id,
                    output,
                    message_thread_id=thread_id,
                )
                if sent:
                    msg_id = sent.message_id
            else:
                # Subsequent captures — edit in place
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=convert_markdown(output),
                        parse_mode="MarkdownV2",
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                except Exception:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg_id,
                            text=output,
                            link_preview_options=NO_LINK_PREVIEW,
                        )
                    except Exception:
                        pass

            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        return
    finally:
        _bash_capture_tasks.pop((user_id, thread_id), None)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.text:
        return

    thread_id = _get_thread_id(update)

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    text = update.message.text

    # Ignore text in window picker mode (only for the same thread)
    if context.user_data and context.user_data.get(STATE_KEY) == STATE_SELECTING_WINDOW:
        pending_tid = context.user_data.get("_pending_thread_id")
        if pending_tid == thread_id:
            await safe_reply(
                update.message,
                "Please use the window picker above, or tap Cancel.",
            )
            return
        # Stale picker state from a different thread — clear it
        clear_window_picker_state(context.user_data)
        context.user_data.pop("_pending_thread_id", None)
        context.user_data.pop("_pending_thread_text", None)

    # Ignore text in directory browsing mode (only for the same thread)
    if (
        context.user_data
        and context.user_data.get(STATE_KEY) == STATE_BROWSING_DIRECTORY
    ):
        pending_tid = context.user_data.get("_pending_thread_id")
        if pending_tid == thread_id:
            await safe_reply(
                update.message,
                "Please use the directory browser above, or tap Cancel.",
            )
            return
        # Stale browsing state from a different thread — clear it
        clear_browse_state(context.user_data)
        context.user_data.pop("_pending_thread_id", None)
        context.user_data.pop("_pending_thread_text", None)

    # Ignore text in session picker mode (only for the same thread)
    if (
        context.user_data
        and context.user_data.get(STATE_KEY) == STATE_SELECTING_SESSION
    ):
        pending_tid = context.user_data.get("_pending_thread_id")
        if pending_tid == thread_id:
            await safe_reply(
                update.message,
                "Please use the session picker above, or tap Cancel.",
            )
            return
        # Stale picker state from a different thread — clear it
        clear_session_picker_state(context.user_data)
        context.user_data.pop("_pending_thread_id", None)
        context.user_data.pop("_pending_thread_text", None)
        context.user_data.pop("_selected_path", None)

    # Must be in a named topic
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid is None:
        # Unbound topic — check for unbound windows first
        all_windows = await tmux_manager.list_windows()
        bound_ids = {wid for _, _, wid in session_manager.iter_thread_bindings()}
        unbound = [
            (w.window_id, w.window_name, w.cwd)
            for w in all_windows
            if w.window_id not in bound_ids
        ]
        logger.debug(
            "Window picker check: all=%s, bound=%s, unbound=%s",
            [w.window_name for w in all_windows],
            bound_ids,
            [name for _, name, _ in unbound],
        )

        if unbound:
            # Show window picker
            logger.info(
                "Unbound topic: showing window picker (%d unbound windows, user=%d, thread=%d)",
                len(unbound),
                user.id,
                thread_id,
            )
            msg_text, keyboard, win_ids = build_window_picker(unbound)
            if context.user_data is not None:
                context.user_data[STATE_KEY] = STATE_SELECTING_WINDOW
                context.user_data[UNBOUND_WINDOWS_KEY] = win_ids
                context.user_data["_pending_thread_id"] = thread_id
                context.user_data["_pending_thread_text"] = text
            await safe_reply(update.message, msg_text, reply_markup=keyboard)
            return

        # No unbound windows — show directory browser to create a new session
        logger.info(
            "Unbound topic: showing directory browser (user=%d, thread=%d)",
            user.id,
            thread_id,
        )
        start_path = str(Path.cwd())
        msg_text, keyboard, subdirs = build_directory_browser(start_path)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
            context.user_data["_pending_thread_id"] = thread_id
            context.user_data["_pending_thread_text"] = text
        await safe_reply(update.message, msg_text, reply_markup=keyboard)
        return

    # Bound topic — forward to bound window
    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        logger.info(
            "Stale binding: window %s gone, unbinding (user=%d, thread=%d)",
            display,
            user.id,
            thread_id,
        )
        session_manager.unbind_thread(user.id, thread_id)
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists. Binding removed.\n"
            "Send a message to start a new session.",
        )
        return

    # Cosmetic / outbound-Telegram steps below must NEVER abort the handler
    # before the message is injected into tmux. On flaky networks the "typing…"
    # indicator (send_action) and status enqueue time out (telegram.error.TimedOut);
    # since the update offset has already advanced, Telegram won't redeliver, so
    # any exception here silently drops the user's message and forces a resend.
    try:
        await update.message.chat.send_action(ChatAction.TYPING)
    except Exception as e:
        logger.warning("send_action(TYPING) failed, continuing to injection: %s", e)
    try:
        await enqueue_status_update(
            context.bot, user.id, wid, None, thread_id=thread_id
        )
    except Exception as e:
        logger.warning("enqueue_status_update failed, continuing to injection: %s", e)

    # Cancel any running bash capture — new message pushes pane content down
    _cancel_bash_capture(user.id, thread_id)

    # A new text message supersedes any un-actioned pending voice transcript
    # in this topic — it's now stale, so tapping its Send later must not
    # inject anything.
    await _expire_pending_voice(context.bot, user.id, thread_id)

    # Check for pending interactive UI before sending text.
    # This catches UIs (permission prompts, etc.) that status polling might have missed.
    # capture_pane is a local tmux call, but handle_interactive_ui hits the network —
    # isolate the whole block so a failure can't prevent the injection below.
    try:
        pane_text = await tmux_manager.capture_pane(w.window_id)
        if pane_text and is_interactive_ui(pane_text):
            # UI detected — show it to user, then send text (acts as Enter)
            logger.info(
                "Detected pending interactive UI before sending text (user=%d, thread=%s)",
                user.id,
                thread_id,
            )
            await handle_interactive_ui(context.bot, user.id, wid, thread_id)
            # Small delay to let UI render in Telegram before text arrives
            await asyncio.sleep(0.3)
    except Exception as e:
        logger.warning("interactive-UI precheck failed, continuing to injection: %s", e)

    # A focused multiSelect free-text row was armed by CB_ASK_NUM
    # (callback_handler) — type this message straight into that row
    # instead of Claude's prompt. No Enter: Claude Code auto-checks the
    # box on the first keystroke and a trailing Enter toggles it back off.
    if (
        pop_pending_inline_edit(user.id, thread_id)
        and get_interactive_window(user.id, thread_id) == wid
    ):
        # Single-line field — a raw \n is an Enter keystroke and would
        # toggle the checkbox off mid-edit.
        await tmux_manager.send_keys(w.window_id, text.replace("\n", " "), enter=False)
        await asyncio.sleep(0.5)
        await handle_interactive_ui(context.bot, user.id, wid, thread_id)
        return

    success, message = await session_manager.send_to_window(wid, text)
    if not success:
        await safe_reply(update.message, f"❌ {message}")
        return

    # Start background capture for ! bash command output
    if text.startswith("!") and len(text) > 1:
        bash_cmd = text[1:]  # strip leading "!"
        task = asyncio.create_task(
            _capture_bash_output(context.bot, user.id, thread_id, wid, bash_cmd)
        )
        _bash_capture_tasks[(user.id, thread_id)] = task
    elif not session_manager.get_window_state(wid).session_id:
        # Plain-shell window (no Claude session → no transcript to mirror):
        # echo the terminal's own output back, reusing the ! capture task.
        task = asyncio.create_task(
            _capture_bash_output(context.bot, user.id, thread_id, wid, text)
        )
        _bash_capture_tasks[(user.id, thread_id)] = task

    # If in interactive mode, refresh the UI after sending text
    interactive_window = get_interactive_window(user.id, thread_id)
    if interactive_window and interactive_window == wid:
        await asyncio.sleep(0.2)
        await handle_interactive_ui(context.bot, user.id, wid, thread_id)


# --- Window creation helper ---


async def _create_and_bind_window(
    query: object,
    context: ContextTypes.DEFAULT_TYPE,
    user: object,
    selected_path: str,
    pending_thread_id: int | None,
    resume_session_id: str | None = None,
) -> None:
    """Create a tmux window, bind it to a topic, and forward pending text.

    Shared by CB_DIR_CONFIRM (no sessions), CB_SESSION_NEW, and CB_SESSION_SELECT.

    Reuse path: if the topic is already bound to a plain-shell window (bound
    eagerly by topic_created_handler when the topic itself was created — see
    bot.py's module docstring), drive Claude into that window instead of
    spawning a second one. Otherwise a shell window would be left running,
    bound to nothing, and never cleaned up (nothing kills it: it has no
    session, so mirror.py's dead-window sweep never even considers it dead).
    """
    from telegram import CallbackQuery, User

    assert isinstance(query, CallbackQuery)
    assert isinstance(user, User)

    reuse_wid: str | None = None
    if pending_thread_id is not None:
        bound_wid = session_manager.get_window_for_thread(user.id, pending_thread_id)
        if bound_wid and not session_manager.get_window_state(bound_wid).session_id:
            reuse_wid = bound_wid

    if reuse_wid:
        cmd = config.claude_command
        if resume_session_id:
            cmd = f"{cmd} --resume {resume_session_id}"
        created_wname = session_manager.get_display_name(reuse_wid)
        send_ok, send_msg = await session_manager.send_to_window(
            reuse_wid, f"cd {shlex.quote(selected_path)} && {cmd}"
        )
        if send_ok:
            success = True
            created_wid = reuse_wid
            message = f"Started Claude in '{created_wname}' at {selected_path}"
        else:
            # Bound window vanished between eager-bind and here — fall back
            # to creating a fresh one, same as the non-reuse path.
            logger.warning(
                "Reuse target %s gone (%s), creating a new window instead",
                reuse_wid,
                send_msg,
            )
            (
                success,
                message,
                created_wname,
                created_wid,
            ) = await tmux_manager.create_window(
                selected_path, resume_session_id=resume_session_id
            )
    else:
        success, message, created_wname, created_wid = await tmux_manager.create_window(
            selected_path, resume_session_id=resume_session_id
        )

    if success:
        logger.info(
            "Window ready: %s (id=%s) at %s (user=%d, thread=%s, resume=%s, reused=%s)",
            created_wname,
            created_wid,
            selected_path,
            user.id,
            pending_thread_id,
            resume_session_id,
            bool(reuse_wid),
        )
        # Wait for Claude Code's SessionStart hook to register in session_map.
        # Resume sessions take longer to start (loading session state), so use
        # a longer timeout to avoid silently dropping messages.
        hook_timeout = 15.0 if resume_session_id else 5.0
        hook_ok = await session_manager.wait_for_session_map_entry(
            created_wid, timeout=hook_timeout
        )

        # --resume creates a new session_id in the hook, but messages continue
        # writing to the resumed session's JSONL file. Override window_state to
        # track the original session_id so the monitor can route messages back.
        if resume_session_id:
            ws = session_manager.get_window_state(created_wid)
            if not hook_ok:
                # Hook timed out — manually populate window_state so the
                # monitor can still route messages back to this topic.
                logger.warning(
                    "Hook timed out for resume window %s, "
                    "manually setting session_id=%s cwd=%s",
                    created_wid,
                    resume_session_id,
                    selected_path,
                )
                ws.session_id = resume_session_id
                ws.cwd = str(selected_path)
                ws.window_name = created_wname
                session_manager._save_state()
            elif ws.session_id != resume_session_id:
                logger.info(
                    "Resume override: window %s session_id %s -> %s",
                    created_wid,
                    ws.session_id,
                    resume_session_id,
                )
                ws.session_id = resume_session_id
                session_manager._save_state()

        if pending_thread_id is not None:
            # Thread bind flow: bind thread to newly created window
            session_manager.bind_thread(
                user.id, pending_thread_id, created_wid, window_name=created_wname
            )

            if resume_session_id:
                status = "Resumed"
            else:
                status = "Started" if reuse_wid else "Created"
            await safe_edit(
                query,
                f"✅ {message}\n\n{status}. Send messages here.",
            )

            # Send pending text if any
            pending_text = (
                context.user_data.get("_pending_thread_text")
                if context.user_data
                else None
            )
            if pending_text:
                logger.debug(
                    "Forwarding pending text to window %s (len=%d)",
                    created_wname,
                    len(pending_text),
                )
                if context.user_data is not None:
                    context.user_data.pop("_pending_thread_text", None)
                    context.user_data.pop("_pending_thread_id", None)
                send_ok, send_msg = await session_manager.send_to_window(
                    created_wid,
                    pending_text,
                )
                if not send_ok:
                    logger.warning("Failed to forward pending text: %s", send_msg)
                    resolved_chat = session_manager.resolve_chat_id(
                        user.id, pending_thread_id
                    )
                    await safe_send(
                        context.bot,
                        resolved_chat,
                        f"❌ Failed to send pending message: {send_msg}",
                        message_thread_id=pending_thread_id,
                    )
            elif context.user_data is not None:
                context.user_data.pop("_pending_thread_id", None)
        else:
            # Should not happen in topic-only mode, but handle gracefully
            await safe_edit(query, f"✅ {message}")
    else:
        await safe_edit(query, f"❌ {message}")
        if pending_thread_id is not None and context.user_data is not None:
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
    await query.answer("Created" if success else "Failed")


# --- Callback query handler ---


async def _refresh_interactive_or_fallback(
    bot: Bot, user_id: int, window_id: str, thread_id: int | None
) -> None:
    """After sending a nav key, refresh whichever UI is showing: a known
    interactive prompt (text, via handle_interactive_ui) or an unrecognized
    dialog (screenshot, via handle_unknown_dialog) — shared by every
    CB_ASK_* handler below and its status-poller refresh path, so a
    keypress inside a --resume picker / /login screen keeps its screenshot
    in sync exactly like AskUserQuestion does with text.
    """
    if await handle_interactive_ui(bot, user_id, window_id, thread_id):
        return
    if await handle_unknown_dialog(bot, user_id, window_id, thread_id):
        return
    await clear_fallback_msg(user_id, bot, thread_id)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        await query.answer("Not authorized")
        return

    data = query.data

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    cb_thread_id = _get_thread_id(update)
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, cb_thread_id, chat.id)

    # History: older/newer pagination
    # Format: hp:<page>:<window_id>:<start>:<end> or hn:<page>:<window_id>:<start>:<end>
    if data.startswith(CB_HISTORY_PREV) or data.startswith(CB_HISTORY_NEXT):
        prefix_len = len(CB_HISTORY_PREV)  # same length for both
        rest = data[prefix_len:]
        try:
            parts = rest.split(":")
            if len(parts) < 4:
                # Old format without byte range: page:window_id
                offset_str, window_id = rest.split(":", 1)
                start_byte, end_byte = 0, 0
            else:
                # New format: page:window_id:start:end (window_id may contain colons)
                offset_str = parts[0]
                start_byte = int(parts[-2])
                end_byte = int(parts[-1])
                window_id = ":".join(parts[1:-2])
            offset = int(offset_str)
        except (ValueError, IndexError):
            await query.answer("Invalid data")
            return

        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await send_history(
                query,
                window_id,
                offset=offset,
                edit=True,
                start_byte=start_byte,
                end_byte=end_byte,
                # Don't pass user_id for pagination - offset update only on initial view
                # This prevents offset from going backwards if new messages arrive while paging
            )
        else:
            await safe_edit(query, "Window no longer exists.")
        await query.answer("Page updated")

    # Directory browser handlers
    elif data.startswith(CB_DIR_SELECT):
        # Validate: callback must come from the same topic that started browsing
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        # callback_data contains index, not dir name (to avoid 64-byte limit)
        try:
            idx = int(data[len(CB_DIR_SELECT) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        # Look up dir name from cached subdirs
        cached_dirs: list[str] = (
            context.user_data.get(BROWSE_DIRS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_dirs):
            await query.answer(
                "Directory list changed, please refresh", show_alert=True
            )
            return
        subdir_name = cached_dirs[idx]

        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        new_path = (Path(current_path) / subdir_name).resolve()

        if not new_path.exists() or not new_path.is_dir():
            await query.answer("Directory not found", show_alert=True)
            return

        new_path_str = str(new_path)
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = new_path_str
            context.user_data[BROWSE_PAGE_KEY] = 0

        msg_text, keyboard, subdirs = build_directory_browser(new_path_str)
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == CB_DIR_UP:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        current = Path(current_path).resolve()
        parent = current.parent
        # No restriction - allow navigating anywhere

        parent_path = str(parent)
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = parent_path
            context.user_data[BROWSE_PAGE_KEY] = 0

        msg_text, keyboard, subdirs = build_directory_browser(parent_path)
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data.startswith(CB_DIR_PAGE):
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        try:
            pg = int(data[len(CB_DIR_PAGE) :])
        except ValueError:
            await query.answer("Invalid data")
            return
        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        if context.user_data is not None:
            context.user_data[BROWSE_PAGE_KEY] = pg

        msg_text, keyboard, subdirs = build_directory_browser(current_path, pg)
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == CB_DIR_CONFIRM:
        default_path = str(Path.cwd())
        selected_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        # Check if this was initiated from a thread bind flow
        pending_thread_id: int | None = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )

        # Validate: confirm button must come from the same topic that started browsing
        confirm_thread_id = _get_thread_id(update)
        if pending_thread_id is not None and confirm_thread_id != pending_thread_id:
            clear_browse_state(context.user_data)
            if context.user_data is not None:
                context.user_data.pop("_pending_thread_id", None)
                context.user_data.pop("_pending_thread_text", None)
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return

        clear_browse_state(context.user_data)

        # Check for existing sessions in this directory
        sessions = await session_manager.list_sessions_for_directory(selected_path)
        if sessions:
            # Show session picker — store state for later
            total = session_manager.count_session_files_for_directory(selected_path)
            if context.user_data is not None:
                context.user_data[STATE_KEY] = STATE_SELECTING_SESSION
                context.user_data[SESSIONS_KEY] = sessions
                context.user_data["_selected_path"] = selected_path
            text, keyboard = build_session_picker(sessions, total_count=total)
            await safe_edit(query, text, reply_markup=keyboard)
            await query.answer()
            return

        # No existing sessions — create new window directly
        await _create_and_bind_window(
            query, context, user, selected_path, pending_thread_id
        )

    elif data in (CB_VOICE_SEND, CB_VOICE_CANCEL):
        tid = _get_thread_id(update)
        if tid is None:
            await query.answer("Not in a topic", show_alert=True)
            return
        entry = _pending_voice.pop((user.id, tid), None)
        if data == CB_VOICE_CANCEL:
            await safe_edit(query, "🎤 Cancelled")
            await query.answer("Cancelled")
            return
        if entry is None:
            await query.answer(
                "Nothing pending (already sent or expired)", show_alert=True
            )
            return
        if time.monotonic() - entry.ts > _PENDING_VOICE_TTL_SECONDS:
            await safe_edit(query, "🎤 expired — resend the voice note")
            await query.answer("Expired", show_alert=True)
            return
        text = entry.text
        wid = session_manager.get_window_for_thread(user.id, tid)
        if wid is None:
            await safe_edit(query, "❌ No terminal bound to this topic anymore")
            await query.answer("No terminal")
            return
        clear_status_msg_info(user.id, tid)
        send_ok, send_msg = await session_manager.send_to_window(wid, text)
        if not send_ok:
            await safe_edit(query, f"❌ {send_msg}")
            await query.answer("Failed")
            return
        await safe_edit(query, f'🎤 → "{text}"')
        await query.answer("Sent")

    elif data in (CB_KILLALL_CONFIRM, CB_KILLALL_CANCEL):
        prompted_at = _pending_killall.pop(user.id, None)
        if prompted_at is None or time.monotonic() - prompted_at > _KILLALL_TTL_SECONDS:
            await safe_edit(query, "expired, run /killall again")
            await query.answer("Expired", show_alert=True)
            return
        if data == CB_KILLALL_CANCEL:
            await safe_edit(query, "cancelled")
            await query.answer("Cancelled")
            return
        # Confirmed: kill every window. Do NOT touch topics here — the
        # mirror's next tick deletes them once their windows are gone.
        windows = await tmux_manager.list_windows()
        n = len(windows)
        for w in windows:
            await tmux_manager.kill_window(w.window_id)
        await safe_edit(query, f"💀 killed {n} terminals")
        await query.answer("Killed")

    elif data == CB_DIR_CANCEL:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        clear_browse_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Session picker: resume existing session
    elif data.startswith(CB_SESSION_SELECT):
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        # Fallback: if _pending_thread_id was cleared (e.g. by a message in
        # another topic), recover it from the callback query's message context
        if pending_tid is None:
            pending_tid = _get_thread_id(update)
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        try:
            idx = int(data[len(CB_SESSION_SELECT) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        cached_sessions = (
            context.user_data.get(SESSIONS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_sessions):
            await query.answer("Session not found")
            return

        session = cached_sessions[idx]
        selected_path = (
            context.user_data.get("_selected_path", str(Path.cwd()))
            if context.user_data
            else str(Path.cwd())
        )
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)

        await _create_and_bind_window(
            query,
            context,
            user,
            selected_path,
            pending_tid,
            resume_session_id=session.session_id,
        )

    elif data == CB_SESSION_ALL:
        # Expand the picker to every conversation in this directory
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        selected_path = (
            context.user_data.get("_selected_path", str(Path.cwd()))
            if context.user_data
            else str(Path.cwd())
        )
        all_sessions = await session_manager.list_sessions_for_directory(
            selected_path, limit=None
        )
        if not all_sessions:
            await query.answer("No sessions found")
            return
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_SELECTING_SESSION
            context.user_data[SESSIONS_KEY] = all_sessions
        text, keyboard = build_session_picker(all_sessions, showing_all=True)
        await safe_edit(query, text, reply_markup=keyboard)
        await query.answer(f"{len(all_sessions)} conversations")

    elif data == CB_SESSION_NEW:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is None:
            pending_tid = _get_thread_id(update)
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        selected_path = (
            context.user_data.get("_selected_path", str(Path.cwd()))
            if context.user_data
            else str(Path.cwd())
        )
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)

        await _create_and_bind_window(query, context, user, selected_path, pending_tid)

    elif data == CB_SESSION_CANCEL:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
            context.user_data.pop("_selected_path", None)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Window picker: bind existing window
    elif data.startswith(CB_WIN_BIND):
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        try:
            idx = int(data[len(CB_WIN_BIND) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        cached_windows: list[str] = (
            context.user_data.get(UNBOUND_WINDOWS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_windows):
            await query.answer("Window list changed, please retry", show_alert=True)
            return
        selected_wid = cached_windows[idx]

        # Verify window still exists
        w = await tmux_manager.find_window_by_id(selected_wid)
        if not w:
            display = session_manager.get_display_name(selected_wid)
            await query.answer(f"Window '{display}' no longer exists", show_alert=True)
            return

        thread_id = _get_thread_id(update)
        if thread_id is None:
            await query.answer("Not in a topic", show_alert=True)
            return

        display = w.window_name
        clear_window_picker_state(context.user_data)
        session_manager.bind_thread(
            user.id, thread_id, selected_wid, window_name=display
        )

        await safe_edit(
            query,
            f"✅ Bound to window `{display}`",
        )

        # Forward pending text if any
        pending_text = (
            context.user_data.get("_pending_thread_text") if context.user_data else None
        )
        if context.user_data is not None:
            context.user_data.pop("_pending_thread_text", None)
            context.user_data.pop("_pending_thread_id", None)
        if pending_text:
            send_ok, send_msg = await session_manager.send_to_window(
                selected_wid, pending_text
            )
            if not send_ok:
                logger.warning("Failed to forward pending text: %s", send_msg)
                resolved_chat = session_manager.resolve_chat_id(user.id, thread_id)
                await safe_send(
                    context.bot,
                    resolved_chat,
                    f"❌ Failed to send pending message: {send_msg}",
                    message_thread_id=thread_id,
                )
        await query.answer("Bound")

    # Window picker: new session → transition to directory browser
    elif data == CB_WIN_NEW:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        # Preserve pending thread info, clear only picker state
        clear_window_picker_state(context.user_data)
        start_path = str(Path.cwd())
        msg_text, keyboard, subdirs = build_directory_browser(start_path)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    # Window picker: cancel
    elif data == CB_WIN_CANCEL:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        clear_window_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Screenshot: Refresh
    elif data.startswith(CB_SCREENSHOT_REFRESH):
        window_id = data[len(CB_SCREENSHOT_REFRESH) :]
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window no longer exists", show_alert=True)
            return

        text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
        if not text:
            await query.answer("Failed to capture pane", show_alert=True)
            return

        png_bytes = await text_to_image(text, with_ansi=True)
        keyboard = _build_screenshot_keyboard(window_id)
        try:
            await query.edit_message_media(
                media=InputMediaDocument(
                    media=io.BytesIO(png_bytes), filename="screenshot.png"
                ),
                reply_markup=keyboard,
            )
            await query.answer("Refreshed")
        except Exception as e:
            logger.error(f"Failed to refresh screenshot: {e}")
            await query.answer("Failed to refresh", show_alert=True)

    # /term: Refresh
    elif data.startswith(CB_TERM_REFRESH):
        window_id = data[len(CB_TERM_REFRESH) :]
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window no longer exists", show_alert=True)
            return

        text = await tmux_manager.capture_pane(w.window_id)
        if not text:
            await query.answer("Failed to capture pane", show_alert=True)
            return

        body = format_pane_text_block(text)
        await safe_edit(query, body, reply_markup=_build_term_keyboard(window_id))
        await query.answer("Refreshed")

    elif data == "noop":
        await query.answer()

    # /effort: level picked
    elif data.startswith(CB_EFFORT_SET):
        level = data[len(CB_EFFORT_SET) :]
        if level not in EFFORT_LEVELS:
            await query.answer("Invalid level")
            return
        thread_id = _get_thread_id(update)
        wid = session_manager.resolve_window_for_thread(user.id, thread_id)
        if not wid:
            await safe_edit(query, "❌ No session bound to this topic.")
            await query.answer()
            return
        w = await tmux_manager.find_window_by_id(wid)
        if not w:
            await safe_edit(query, "❌ Window no longer exists.")
            await query.answer()
            return
        display = session_manager.get_display_name(wid)
        success, message = await session_manager.send_to_window(wid, f"/effort {level}")
        if success:
            await safe_edit(query, f"⚡ [{display}] effort → *{level}*")
            await query.answer(f"effort → {level}")
        else:
            await safe_edit(query, f"❌ {message}")
            await query.answer("Failed", show_alert=True)

    elif data == CB_EFFORT_CANCEL:
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Interactive UI: press an option number directly. In single-select
    # Claude Code choice dialogs, digits both select and confirm (verified
    # live 2026-07-05). In multiSelect dialogs, digits only TOGGLE that
    # option's checkbox (verified live 2026-07-05) — confirming requires
    # the ✅ Submit button (CB_ASK_SUBMIT) or navigating there manually.
    # Single-select free-text options ("Type something.", "Chat about
    # this") are a popup: a digit+Enter opens it, then the next message
    # fills it. multiSelect's free-text row is different — it's an
    # inline-editable option (a digit press just toggles an empty checked
    # "Type something" entry, which is useless) — see the elif below.
    elif data.startswith(CB_ASK_NUM):
        rest = data[len(CB_ASK_NUM) :]
        digit, _, window_id = rest.partition(":")
        thread_id = _get_thread_id(update)
        num = int(digit) if digit.isdigit() else -1
        label = get_interactive_option_label(user.id, thread_id, num)
        old_state = get_interactive_option_state(user.id, thread_id, num)
        is_text_option = bool(label) and label.lower().rstrip(".…").strip() in (
            "type something",
            "chat about this",
        )
        w = await tmux_manager.find_window_by_id(window_id)
        if not (w and digit.isdigit()):
            await query.answer("Window no longer exists", show_alert=True)
        elif old_state is not None and is_text_option:
            # multiSelect free-text row: focus it with arrow keys instead
            # of pressing its digit, then arm inline-edit mode so the
            # user's next message types straight into the row.
            pane_text = await tmux_manager.capture_pane(w.window_id)
            focused = parse_focused_option(pane_text) if pane_text else None
            delta = num - focused if focused is not None else 0
            step_key = "Down" if delta > 0 else "Up"
            for _ in range(abs(delta)):
                await tmux_manager.send_keys(
                    w.window_id, step_key, enter=False, literal=False
                )
                await asyncio.sleep(0.15)
            await asyncio.sleep(0.2)
            pane_text = await tmux_manager.capture_pane(w.window_id)
            if (parse_focused_option(pane_text) if pane_text else None) != num:
                await query.answer(
                    "Couldn't focus the option — use ↑/↓", show_alert=True
                )
            else:
                set_pending_inline_edit(user.id, thread_id)
                await _refresh_interactive_or_fallback(
                    context.bot, user.id, window_id, thread_id
                )
                await query.answer(
                    f"✏️ {digit} — send your text as a normal message",
                    show_alert=True,
                )
        elif old_state is not None:
            # multiSelect: the digit just toggles the checkbox — report
            # the new (post-toggle) state, not the stale pre-press one.
            await tmux_manager.send_keys(w.window_id, digit, enter=False)
            await asyncio.sleep(0.6)
            await _refresh_interactive_or_fallback(
                context.bot, user.id, window_id, thread_id
            )
            mark = "☐" if old_state else "☑"
            await query.answer(f"{mark} {digit}. {label or ''}"[:200])
        else:
            await tmux_manager.send_keys(w.window_id, digit, enter=False)
            if is_text_option:
                # Digits only SELECT free-text options (regular options
                # select+confirm) — press Enter to actually open the input
                # box, then the user's next plain message is typed into it.
                await asyncio.sleep(0.4)
                await tmux_manager.send_keys(
                    w.window_id, "Enter", enter=False, literal=False
                )
            await asyncio.sleep(0.6)
            await _refresh_interactive_or_fallback(
                context.bot, user.id, window_id, thread_id
            )
            if is_text_option:
                await query.answer(
                    f"{digit}. {label} — now send your text as a normal message",
                    show_alert=True,
                )
            else:
                await query.answer(f"▶ {digit}. {label or ''}"[:200])

    # Interactive UI: multiSelect Submit — arrow-tab to the dialog's Submit
    # screen and confirm. The dialog has a fixed tab order ending in
    # "✔ Submit", so pressing → up to 5 times always reaches it; landing on
    # the review screen (numbered "1. Submit answers") is already handled
    # by CB_ASK_NUM's normal digit-select-and-confirm path.
    elif data.startswith(CB_ASK_SUBMIT):
        window_id = data[len(CB_ASK_SUBMIT) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            # Route to the review screen from any focus position (verified
            # live 2026-07-05): from a regular option row → presses tab-
            # switch to Submit. From the inline-editable free-text row,
            # Right only moves the text cursor — walk Down instead, onto
            # the unnumbered "❯ Submit" pseudo-row, where Enter opens the
            # review screen ("ctrl+g to edit" in the footer marks that
            # editable-row focus state).
            submitted = False
            for _ in range(6):
                pane_text = await tmux_manager.capture_pane(w.window_id) or ""
                if "Ready to submit" in pane_text:
                    await tmux_manager.send_keys(w.window_id, "1", enter=False)
                    submitted = True
                    break
                if _RE_SUBMIT_ROW.search(pane_text):
                    key = "Enter"
                elif "ctrl+g to edit" in pane_text:
                    key = "Down"
                else:
                    key = "Right"
                await tmux_manager.send_keys(
                    w.window_id, key, enter=False, literal=False
                )
                await asyncio.sleep(0.5)
            if submitted:
                await asyncio.sleep(0.6)
                await _refresh_interactive_or_fallback(
                    context.bot, user.id, window_id, thread_id
                )
                await query.answer("✅ Submitted")
            else:
                await _refresh_interactive_or_fallback(
                    context.bot, user.id, window_id, thread_id
                )
                await query.answer("Couldn't reach Submit tab — use the arrows")
        else:
            await query.answer("Window no longer exists", show_alert=True)

    # Interactive UI: Up arrow
    elif data.startswith(CB_ASK_UP):
        window_id = data[len(CB_ASK_UP) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(w.window_id, "Up", enter=False, literal=False)
            await asyncio.sleep(0.5)
            await _refresh_interactive_or_fallback(
                context.bot, user.id, window_id, thread_id
            )
        await query.answer()

    # Interactive UI: Down arrow
    elif data.startswith(CB_ASK_DOWN):
        window_id = data[len(CB_ASK_DOWN) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Down", enter=False, literal=False
            )
            await asyncio.sleep(0.5)
            await _refresh_interactive_or_fallback(
                context.bot, user.id, window_id, thread_id
            )
        await query.answer()

    # Interactive UI: Left arrow
    elif data.startswith(CB_ASK_LEFT):
        window_id = data[len(CB_ASK_LEFT) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Left", enter=False, literal=False
            )
            await asyncio.sleep(0.5)
            await _refresh_interactive_or_fallback(
                context.bot, user.id, window_id, thread_id
            )
        await query.answer()

    # Interactive UI: Right arrow
    elif data.startswith(CB_ASK_RIGHT):
        window_id = data[len(CB_ASK_RIGHT) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Right", enter=False, literal=False
            )
            await asyncio.sleep(0.5)
            await _refresh_interactive_or_fallback(
                context.bot, user.id, window_id, thread_id
            )
        await query.answer()

    # Interactive UI: Escape
    elif data.startswith(CB_ASK_ESC):
        window_id = data[len(CB_ASK_ESC) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Escape", enter=False, literal=False
            )
            await clear_interactive_msg(user.id, context.bot, thread_id)
            await clear_fallback_msg(user.id, context.bot, thread_id)
        await query.answer("⎋ Esc")

    # Interactive UI: Enter
    elif data.startswith(CB_ASK_ENTER):
        window_id = data[len(CB_ASK_ENTER) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Enter", enter=False, literal=False
            )
            await asyncio.sleep(0.5)
            await _refresh_interactive_or_fallback(
                context.bot, user.id, window_id, thread_id
            )
        await query.answer("⏎ Enter")

    # Interactive UI: Space
    elif data.startswith(CB_ASK_SPACE):
        window_id = data[len(CB_ASK_SPACE) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Space", enter=False, literal=False
            )
            await asyncio.sleep(0.5)
            await _refresh_interactive_or_fallback(
                context.bot, user.id, window_id, thread_id
            )
        await query.answer("␣ Space")

    # Interactive UI: Tab
    elif data.startswith(CB_ASK_TAB):
        window_id = data[len(CB_ASK_TAB) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(w.window_id, "Tab", enter=False, literal=False)
            await asyncio.sleep(0.5)
            await _refresh_interactive_or_fallback(
                context.bot, user.id, window_id, thread_id
            )
        await query.answer("⇥ Tab")

    # Interactive UI: refresh display
    elif data.startswith(CB_ASK_REFRESH):
        window_id = data[len(CB_ASK_REFRESH) :]
        thread_id = _get_thread_id(update)
        await _refresh_interactive_or_fallback(
            context.bot, user.id, window_id, thread_id
        )
        await query.answer("🔄")

    # Screenshot quick keys: send key to tmux window
    elif data.startswith(CB_KEYS_PREFIX):
        rest = data[len(CB_KEYS_PREFIX) :]
        colon_idx = rest.find(":")
        if colon_idx < 0:
            await query.answer("Invalid data")
            return
        key_id = rest[:colon_idx]
        window_id = rest[colon_idx + 1 :]

        key_info = _KEYS_SEND_MAP.get(key_id)
        if not key_info:
            await query.answer("Unknown key")
            return

        tmux_key, enter, literal = key_info
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window not found", show_alert=True)
            return

        await tmux_manager.send_keys(
            w.window_id, tmux_key, enter=enter, literal=literal
        )
        await query.answer(_KEY_LABELS.get(key_id, key_id))

        # Refresh screenshot after key press
        await asyncio.sleep(0.5)
        text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
        if text:
            png_bytes = await text_to_image(text, with_ansi=True)
            keyboard = _build_screenshot_keyboard(window_id)
            try:
                await query.edit_message_media(
                    media=InputMediaDocument(
                        media=io.BytesIO(png_bytes),
                        filename="screenshot.png",
                    ),
                    reply_markup=keyboard,
                )
            except Exception:
                pass  # Screenshot unchanged or message too old


# --- Streaming response / notifications ---


async def handle_new_message(msg: NewMessage, bot: Bot) -> None:
    """Handle a new assistant message — enqueue for sequential processing.

    Messages are queued per-user to ensure status messages always appear last.
    Routes via thread_bindings to deliver to the correct topic.
    """
    status = "complete" if msg.is_complete else "streaming"
    logger.info(
        f"handle_new_message [{status}]: session={msg.session_id}, "
        f"text_len={len(msg.text)}"
    )

    # Find users whose thread-bound window matches this session
    active_users = await session_manager.find_users_for_session(msg.session_id)

    if not active_users:
        logger.info(f"No active users for session {msg.session_id}")
        return

    for user_id, wid, thread_id in active_users:
        # Handle interactive tools specially - capture terminal and send UI
        if msg.tool_name in INTERACTIVE_TOOL_NAMES and msg.content_type == "tool_use":
            # Mark interactive mode BEFORE sleeping so polling skips this window
            set_interactive_mode(user_id, wid, thread_id)
            # Flush pending messages (e.g. plan content) before sending interactive UI
            queue = get_message_queue(user_id)
            if queue:
                await queue.join()
            # Wait briefly for Claude Code to render the question UI
            await asyncio.sleep(0.3)
            handled = await handle_interactive_ui(bot, user_id, wid, thread_id)
            if handled:
                # Update user's read offset
                session = await session_manager.resolve_session_for_window(wid)
                if session and session.file_path:
                    try:
                        file_size = Path(session.file_path).stat().st_size
                        session_manager.update_user_window_offset(
                            user_id, wid, file_size
                        )
                    except OSError:
                        pass
                continue  # Don't send the normal tool_use message
            else:
                # UI not rendered — clear the early-set mode
                clear_interactive_mode(user_id, thread_id)

        # Any non-interactive message means the interaction is complete — delete the UI message
        if get_interactive_msg_id(user_id, thread_id):
            await clear_interactive_msg(user_id, bot, thread_id)

        # Skip tool call notifications when CCBOT_SHOW_TOOL_CALLS=false
        if not config.show_tool_calls and msg.content_type in (
            "tool_use",
            "tool_result",
        ):
            continue

        if msg.role == "user" and msg.content_type == "text":
            # A real user turn just started in the transcript (whether typed
            # on the Mac or injected by ccbot itself via send_to_window) —
            # thinking consolidation resets so the next thinking block starts
            # a fresh message instead of editing the previous turn's.
            # ponytail: relies on this NewMessage actually being delivered,
            # i.e. CCBOT_SHOW_USER_MESSAGES staying enabled (current deploy
            # default). If that ever flips off, move the reset signal into
            # SessionMonitor's turn buffering instead.
            reset_thinking_turn(user_id, thread_id)
            if session_manager.was_recently_injected(wid, msg.text):
                # Echo of text ccbot itself typed into tmux — the user
                # already sees it as their own sent Telegram message, so
                # mirroring it back would show it twice. Messages typed
                # directly on the Mac terminal were never injected here, so
                # they still mirror below (with the 👤 prefix).
                continue

        if msg.is_complete:
            if msg.content_type == "thinking":
                # One edited-in-place message per turn instead of one message
                # per thinking block (Fable-class models think between every
                # tool call).
                await enqueue_thinking_update(
                    bot=bot,
                    user_id=user_id,
                    window_id=wid,
                    text=msg.text,
                    thread_id=thread_id,
                )
            else:
                parts = build_response_parts(
                    msg.text,
                    msg.is_complete,
                    msg.content_type,
                    msg.role,
                )
                # Enqueue content message task
                # Note: tool_result editing is handled inside _process_content_task
                # to ensure sequential processing with tool_use message sending
                await enqueue_content_message(
                    bot=bot,
                    user_id=user_id,
                    window_id=wid,
                    parts=parts,
                    tool_use_id=msg.tool_use_id,
                    content_type=msg.content_type,
                    text=msg.text,
                    thread_id=thread_id,
                    image_data=msg.image_data,
                )

            # Update user's read offset to current file position
            # This marks these messages as "read" for this user
            session = await session_manager.resolve_session_for_window(wid)
            if session and session.file_path:
                try:
                    file_size = Path(session.file_path).stat().st_size
                    session_manager.update_user_window_offset(user_id, wid, file_size)
                except OSError:
                    pass


# --- App lifecycle ---


async def post_init(application: Application) -> None:
    global session_monitor, _status_poll_task, _mirror_poll_task, _dashboard_poll_task
    global _overnight_poll_task

    bot_commands = [
        BotCommand("start", "Show welcome message"),
        BotCommand("new", "Start Claude in <project> (anywhere in the group)"),
        BotCommand(
            "speak", "Voice-note of the last answer; optional speed, e.g. /speak 0.8"
        ),
        BotCommand("history", "Message history for this topic"),
        BotCommand("screenshot", "Terminal screenshot with control keys"),
        BotCommand("term", "Terminal pane text as a code block"),
        BotCommand("esc", "Send Escape to interrupt Claude"),
        BotCommand("effort", "Pick reasoning effort (low…max)"),
        BotCommand("kill", "Kill session and delete topic"),
        BotCommand("unbind", "Unbind topic from session (keeps window running)"),
        BotCommand("dashboard", "Recreate the live terminal dashboard"),
        BotCommand("grab", "Send a file from the terminal's cwd"),
        BotCommand("sleep", "Arm overnight-autonomy quiet hours"),
        BotCommand("wake", "Disarm quiet hours + morning report"),
        BotCommand("mute", "Mute notifications [mac|phone]"),
        BotCommand("unmute", "Unmute notifications [mac|phone]"),
        BotCommand("lock", "Freeze all inbound control (kill switch)"),
        BotCommand("unlock", "Release the lock"),
        BotCommand("killall", "Panic button: kill every session"),
        BotCommand("usage", "Show Claude Code usage remaining"),
        BotCommand("account", "Which Claude account this host runs on"),
    ]
    # Add Claude Code slash commands
    for cmd_name, desc in CC_COMMANDS.items():
        bot_commands.append(BotCommand(cmd_name, desc))

    # Registering the command menu is cosmetic (Telegram's "/" autocomplete) —
    # never let it block startup.
    try:
        await application.bot.delete_my_commands()
        await application.bot.set_my_commands(bot_commands)
    except Exception as e:
        logger.warning("Failed to register bot command menu: %s", e)

    # Re-resolve stale window IDs from persisted state against live tmux windows
    await session_manager.resolve_stale_ids()

    # Pre-fill global rate limiter bucket on restart.
    # AsyncLimiter starts at _level=0 (full burst capacity), but Telegram's
    # server-side counter persists across bot restarts.  Setting _level=max_rate
    # forces the bucket to start "full" so capacity drains in naturally (~1s).
    # AIORateLimiter has no per-private-chat limiter, so max_retries is the
    # primary protection (retry + pause all concurrent requests on 429).
    rate_limiter = application.bot.rate_limiter
    if rate_limiter and rate_limiter._base_limiter:
        rate_limiter._base_limiter._level = rate_limiter._base_limiter.max_rate
        logger.info("Pre-filled global rate limiter bucket")

    monitor = SessionMonitor()

    async def message_callback(msg: NewMessage) -> None:
        await handle_new_message(msg, application.bot)

    async def title_callback(session_id: str, ai_title: str) -> None:
        await on_ai_title(application.bot, session_id, ai_title)

    monitor.set_message_callback(message_callback)
    monitor.set_title_callback(title_callback)
    monitor.start()
    session_monitor = monitor
    logger.info("Session monitor started")

    # Start status polling task
    _status_poll_task = asyncio.create_task(status_poll_loop(application.bot))
    logger.info("Status polling task started")

    # Overnight-autonomy snapshot loop: always running (armed state is a
    # runtime /sleep-/wake toggle, not a startup config gate — see overnight.py).
    _overnight_poll_task = asyncio.create_task(overnight_poll_loop(application.bot))
    logger.info("Overnight polling task started")

    # Auto-topic mirror: converge drift accumulated while the daemon was down
    # (dead windows' topics deleted, live unbound windows get topics) with one
    # synchronous pass *before* the bot starts processing updates — relying on
    # the poll loop's first iteration would race incoming updates against a
    # not-yet-reconciled state. resolve_stale_ids() above already ran, so
    # window IDs are current even if the tmux server itself was restarted.
    # No-op when config.mirror_chat_id is unset, so only bother when enabled.
    if config.mirror_chat_id:
        await mirror_tick(application.bot)
        _mirror_poll_task = asyncio.create_task(mirror_poll_loop(application.bot))
        logger.info("Mirror polling task started (chat_id=%d)", config.mirror_chat_id)

        # Live dashboard: separate task from the mirror above (see
        # dashboard.py's module docstring) but the same gate/cadence — the
        # dashboard lives in this same forum group's General topic.
        await dashboard_tick(application.bot)
        _dashboard_poll_task = asyncio.create_task(dashboard_poll_loop(application.bot))
        logger.info("Dashboard polling task started")


async def post_shutdown(application: Application) -> None:
    global _status_poll_task, _mirror_poll_task, _dashboard_poll_task
    global _overnight_poll_task

    # Stop status polling
    if _status_poll_task:
        _status_poll_task.cancel()
        try:
            await _status_poll_task
        except asyncio.CancelledError:
            pass
        _status_poll_task = None
        logger.info("Status polling stopped")

    # Stop mirror polling
    if _mirror_poll_task:
        _mirror_poll_task.cancel()
        try:
            await _mirror_poll_task
        except asyncio.CancelledError:
            pass
        _mirror_poll_task = None
        logger.info("Mirror polling stopped")

    # Stop dashboard polling
    if _dashboard_poll_task:
        _dashboard_poll_task.cancel()
        try:
            await _dashboard_poll_task
        except asyncio.CancelledError:
            pass
        _dashboard_poll_task = None
        logger.info("Dashboard polling stopped")

    # Stop overnight polling
    if _overnight_poll_task:
        _overnight_poll_task.cancel()
        try:
            await _overnight_poll_task
        except asyncio.CancelledError:
            pass
        _overnight_poll_task = None
        logger.info("Overnight polling stopped")

    # Stop all queue workers
    await shutdown_workers()

    if session_monitor:
        session_monitor.stop()
        logger.info("Session monitor stopped")

    await close_transcribe_client()


def create_bot() -> Application:
    application = (
        Application.builder()
        .token(config.telegram_bot_token)
        .rate_limiter(AIORateLimiter(max_retries=5))
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Lock gate: group=-1 runs before every other handler below. While
    # locked, it drops the update (raises ApplicationHandlerStop) before any
    # command/text/voice/photo/callback handler ever sees it.
    application.add_handler(MessageHandler(filters.ALL, lock_gate_handler), group=-1)
    application.add_handler(CallbackQueryHandler(lock_gate_handler), group=-1)

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("new", new_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("screenshot", screenshot_command))
    application.add_handler(CommandHandler("term", term_command))
    application.add_handler(CommandHandler("esc", esc_command))
    application.add_handler(CommandHandler("effort", effort_command))
    application.add_handler(CommandHandler("unbind", unbind_command))
    application.add_handler(CommandHandler("usage", usage_command))
    application.add_handler(CommandHandler("dashboard", dashboard_command))
    application.add_handler(CommandHandler("lock", lock_command))
    application.add_handler(CommandHandler("unlock", unlock_command))
    application.add_handler(CommandHandler("sleep", sleep_command))
    application.add_handler(CommandHandler("wake", wake_command))
    application.add_handler(CommandHandler("account", account_command))
    application.add_handler(CommandHandler("mute", mute_command))
    application.add_handler(CommandHandler("unmute", unmute_command))
    application.add_handler(CommandHandler("killall", killall_command))
    application.add_handler(CommandHandler("grab", grab_command))
    application.add_handler(CommandHandler("speak", speak_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    # Topic created event — eagerly bind a plain-shell window
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_CREATED,
            topic_created_handler,
        )
    )
    # Topic closed event — auto-kill associated window
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_CLOSED,
            topic_closed_handler,
        )
    )
    # Topic edited event — sync renamed topic to tmux window
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_EDITED,
            topic_edited_handler,
        )
    )
    # Forward any other /command to Claude Code
    application.add_handler(MessageHandler(filters.COMMAND, forward_command_handler))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )
    # Photos: download and forward file path to Claude Code
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    # Voice: transcribe via OpenAI and forward text to Claude Code
    application.add_handler(MessageHandler(filters.VOICE, voice_handler))
    # Catch-all: non-text content (stickers, video, etc.)
    application.add_handler(
        MessageHandler(
            ~filters.COMMAND & ~filters.TEXT & ~filters.StatusUpdate.ALL,
            unsupported_content_handler,
        )
    )

    return application
