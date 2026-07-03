"""Safe message sending helpers with MarkdownV2 fallback.

Provides utility functions for sending Telegram messages with automatic
format conversion and fallback to plain text on failure.

Uses telegramify-markdown for MarkdownV2 formatting.

Functions:
  - send_with_fallback: Send with formatting → plain text fallback
  - send_photo: Photo sending (single or media group)
  - safe_reply: Reply with formatting, fallback to plain text
  - safe_edit: Edit message with formatting, fallback to plain text
  - safe_send: Send message with formatting, fallback to plain text
  - is_thread_deleted_error: Detect Telegram's "message thread not found"
  - cleanup_deleted_thread: Kill the tmux window bound to a deleted topic

Rate limiting is handled globally by AIORateLimiter on the Application.
RetryAfter exceptions are re-raised so callers (queue worker) can handle them.

Deleted-topic detection: Telegram sends no event when a forum topic is
deleted, so it's detected here instead — every function that *sends* a new
message (edits don't take a thread id, so they can't produce this error)
checks the failure against is_thread_deleted_error and, on a match, runs
cleanup_deleted_thread instead of retrying. Centralized here so every send
path (and interactive_ui.py's raw bot.send_message, which bypasses these
helpers for plain-text formatting) shares one detector and one cleanup path.
"""

import io
import logging
from typing import Any

from telegram import Bot, InputMediaPhoto, LinkPreviewOptions, Message
from telegram.error import BadRequest, RetryAfter

from ..markdown_v2 import convert_markdown
from ..transcript_parser import TranscriptParser

logger = logging.getLogger(__name__)


def is_thread_deleted_error(exc: BaseException) -> bool:
    """True if exc is Telegram's error for sending into a deleted forum topic."""
    return (
        isinstance(exc, BadRequest) and "message thread not found" in str(exc).lower()
    )


async def cleanup_deleted_thread(chat_id: int, thread_id: int) -> None:
    """Topic no longer exists on Telegram — kill its tmux window and forget it.

    Does not touch Telegram (the topic and its messages are already gone),
    so this can never re-trigger a send into the dead thread. Killing an
    already-gone window / unbinding an already-unbound thread are no-ops, so
    repeated calls (e.g. from several already-queued tasks for the same
    thread) are harmless.

    Local imports avoid a circular import (this module <- message_queue.py
    <- ... <- cleanup.py, which needs message_queue.py itself).
    """
    from ..session import session_manager
    from ..tmux_manager import tmux_manager
    from .cleanup import clear_topic_state

    for user_id, tid, window_id in list(session_manager.iter_thread_bindings()):
        if tid != thread_id or session_manager.resolve_chat_id(user_id, tid) != chat_id:
            continue
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.kill_window(w.window_id)
        session_manager.unbind_thread(user_id, tid)
        await clear_topic_state(user_id, tid)
        logger.info(
            "Topic thread %d deleted on Telegram: killed window %s, unbound (user=%d)",
            tid,
            window_id,
            user_id,
        )


async def _check_deleted_thread(
    chat_id: int, thread_id: int | None, exc: BaseException
) -> bool:
    """If exc means the topic was deleted, clean up. Returns True if handled."""
    if thread_id is None or not is_thread_deleted_error(exc):
        return False
    await cleanup_deleted_thread(chat_id, thread_id)
    return True


def strip_sentinels(text: str) -> str:
    """Strip expandable quote sentinel markers for plain text fallback."""
    for s in (
        TranscriptParser.EXPANDABLE_QUOTE_START,
        TranscriptParser.EXPANDABLE_QUOTE_END,
    ):
        text = text.replace(s, "")
    return text


def _ensure_formatted(text: str) -> str:
    """Convert markdown to MarkdownV2."""
    return convert_markdown(text)


PARSE_MODE = "MarkdownV2"


# Disable link previews in all messages to reduce visual noise
NO_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def send_with_fallback(
    bot: Bot,
    chat_id: int,
    text: str,
    **kwargs: Any,
) -> Message | None:
    """Send message with MarkdownV2, falling back to plain text on failure.

    Returns the sent Message on success, None on failure.
    RetryAfter is re-raised for caller handling.
    """
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    thread_id = kwargs.get("message_thread_id")
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=_ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception as e:
        if await _check_deleted_thread(chat_id, thread_id, e):
            return None
        try:
            return await bot.send_message(
                chat_id=chat_id, text=strip_sentinels(text), **kwargs
            )
        except RetryAfter:
            raise
        except Exception as e2:
            if await _check_deleted_thread(chat_id, thread_id, e2):
                return None
            logger.error(f"Failed to send message to {chat_id}: {e2}")
            return None


async def send_photo(
    bot: Bot,
    chat_id: int,
    image_data: list[tuple[str, bytes]],
    **kwargs: Any,
) -> None:
    """Send photo(s) to chat. Sends as media group if multiple images.

    Rate limiting is handled globally by AIORateLimiter on the Application.

    Args:
        bot: Telegram Bot instance
        chat_id: Target chat ID
        image_data: List of (media_type, raw_bytes) tuples
        **kwargs: Extra kwargs passed to send_photo/send_media_group
    """
    if not image_data:
        return
    thread_id = kwargs.get("message_thread_id")
    try:
        if len(image_data) == 1:
            _media_type, raw_bytes = image_data[0]
            await bot.send_photo(
                chat_id=chat_id,
                photo=io.BytesIO(raw_bytes),
                **kwargs,
            )
        else:
            media = [
                InputMediaPhoto(media=io.BytesIO(raw_bytes))
                for _media_type, raw_bytes in image_data
            ]
            await bot.send_media_group(
                chat_id=chat_id,
                media=media,
                **kwargs,
            )
    except RetryAfter:
        raise
    except Exception as e:
        if await _check_deleted_thread(chat_id, thread_id, e):
            return
        logger.error("Failed to send photo to %d: %s", chat_id, e)


async def safe_reply(message: Message, text: str, **kwargs: Any) -> Message:
    """Reply with formatting, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    thread_id = message.message_thread_id
    try:
        return await message.reply_text(
            _ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception as e:
        if await _check_deleted_thread(message.chat_id, thread_id, e):
            raise
        try:
            return await message.reply_text(strip_sentinels(text), **kwargs)
        except RetryAfter:
            raise
        except Exception as e2:
            if await _check_deleted_thread(message.chat_id, thread_id, e2):
                raise
            logger.error(f"Failed to reply: {e2}")
            raise


async def safe_edit(target: Any, text: str, **kwargs: Any) -> None:
    """Edit message with formatting, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        await target.edit_message_text(
            _ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            await target.edit_message_text(strip_sentinels(text), **kwargs)
        except RetryAfter:
            raise
        except Exception as e:
            logger.error("Failed to edit message: %s", e)


async def safe_send(
    bot: Bot,
    chat_id: int,
    text: str,
    message_thread_id: int | None = None,
    **kwargs: Any,
) -> None:
    """Send message with formatting, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    if message_thread_id is not None:
        kwargs.setdefault("message_thread_id", message_thread_id)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=_ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception as e:
        if await _check_deleted_thread(chat_id, message_thread_id, e):
            return
        try:
            await bot.send_message(
                chat_id=chat_id, text=strip_sentinels(text), **kwargs
            )
        except RetryAfter:
            raise
        except Exception as e2:
            if await _check_deleted_thread(chat_id, message_thread_id, e2):
                return
            logger.error(f"Failed to send message to {chat_id}: {e2}")
