"""Tests for message_queue thinking consolidation.

Covers: one edited-in-place Telegram message per turn instead of one message
per thinking block, the trim-to-budget behavior when accumulated blocks
overflow MERGE_MAX_LENGTH, and reset_thinking_turn starting a fresh message
for the next turn.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.message_queue import (
    MERGE_MAX_LENGTH,
    _strip_expandable_quote,
    _thinking_body,
    _thinking_turns,
    enqueue_thinking_update,
    get_message_queue,
    reset_thinking_turn,
    shutdown_workers,
)
from ccbot.transcript_parser import TranscriptParser

EXP_START = TranscriptParser.EXPANDABLE_QUOTE_START
EXP_END = TranscriptParser.EXPANDABLE_QUOTE_END


def _quoted(text: str) -> str:
    return f"{EXP_START}{text}{EXP_END}"


class TestStripExpandableQuote:
    def test_strips_wrapped_text(self):
        assert _strip_expandable_quote(_quoted("hello")) == "hello"

    def test_leaves_unwrapped_text_untouched(self):
        assert _strip_expandable_quote("(thinking)") == "(thinking)"

    def test_empty_string(self):
        assert _strip_expandable_quote("") == ""


class TestThinkingBody:
    """Tests for _thinking_body — header + single expandable quote assembly."""

    def test_header_reports_true_block_count_and_tokens(self):
        body = _thinking_body(["aaaa", "bbbb"], time.monotonic())
        assert "🧠 Thinking · 2 blocks" in body
        assert "~2 tok" in body  # 8 total chars / 4

    def test_content_wrapped_in_a_single_expandable_quote(self):
        body = _thinking_body(["one", "two"], time.monotonic())
        assert body.count(EXP_START) == 1
        assert body.count(EXP_END) == 1
        assert "one\n\ntwo" in body

    def test_header_time_reflects_elapsed_since_first_block(self):
        first_block_at = time.monotonic() - 5.0
        body = _thinking_body(["x"], first_block_at)
        assert "5s" in body

    def test_overflow_drops_oldest_content_keeps_newest(self):
        """When accumulated blocks exceed the merge budget, the header must
        still report the true totals while the CONTENT keeps the most
        recent material (oldest dropped from the front)."""
        blocks = ["OLDEST_MARKER" + "x" * 5000, "NEWEST_MARKER final thought"]
        body = _thinking_body(blocks, time.monotonic())

        assert "2 blocks" in body  # header stays accurate despite trimming
        inner = body[body.index(EXP_START) + len(EXP_START) : body.index(EXP_END)]
        assert "OLDEST_MARKER" not in inner
        assert "NEWEST_MARKER final thought" in inner
        assert len(body) <= MERGE_MAX_LENGTH + 20

    def test_small_content_never_trimmed(self):
        body = _thinking_body(["short thought"], time.monotonic())
        inner = body[body.index(EXP_START) + len(EXP_START) : body.index(EXP_END)]
        assert inner == "short thought"


@pytest.fixture(autouse=True)
async def _isolate_queue_state():
    """Per-user queues/workers and thinking state are module-level globals —
    clear them before and after each test so tests don't leak into each other."""
    _thinking_turns.clear()
    yield
    await shutdown_workers()
    _thinking_turns.clear()


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent = MagicMock()
    sent.message_id = 555
    bot.send_message.return_value = sent
    return bot


class TestThinkingConsolidationQueue:
    """End-to-end through the real per-user queue + worker (the same
    mechanism status messages use to edit in place)."""

    @pytest.mark.asyncio
    async def test_first_block_sends_a_new_message(self, mock_bot):
        with patch("ccbot.handlers.message_queue.session_manager") as mock_sm:
            mock_sm.resolve_chat_id.return_value = 100
            await enqueue_thinking_update(
                mock_bot, 1, "@1", _quoted("first thought"), thread_id=42
            )
            await get_message_queue(1).join()

        mock_bot.send_message.assert_called_once()
        mock_bot.edit_message_text.assert_not_called()
        state = _thinking_turns[(1, 42)]
        assert state.message_id == 555
        assert state.blocks == ["first thought"]

    @pytest.mark.asyncio
    async def test_second_block_edits_same_message_instead_of_sending_new(
        self, mock_bot
    ):
        with patch("ccbot.handlers.message_queue.session_manager") as mock_sm:
            mock_sm.resolve_chat_id.return_value = 100
            await enqueue_thinking_update(
                mock_bot, 1, "@1", _quoted("first thought"), thread_id=42
            )
            queue = get_message_queue(1)
            await queue.join()

            await enqueue_thinking_update(
                mock_bot, 1, "@1", _quoted("second thought"), thread_id=42
            )
            await queue.join()

        mock_bot.send_message.assert_called_once()  # still just the one send
        mock_bot.edit_message_text.assert_called_once()
        edit_kwargs = mock_bot.edit_message_text.call_args.kwargs
        assert edit_kwargs["message_id"] == 555
        assert "2 blocks" in edit_kwargs["text"]

        state = _thinking_turns[(1, 42)]
        assert state.blocks == ["first thought", "second thought"]

    @pytest.mark.asyncio
    async def test_third_block_edits_again_still_one_message(self, mock_bot):
        with patch("ccbot.handlers.message_queue.session_manager") as mock_sm:
            mock_sm.resolve_chat_id.return_value = 100
            queue = None
            for block in ("one", "two", "three"):
                await enqueue_thinking_update(
                    mock_bot, 1, "@1", _quoted(block), thread_id=42
                )
                queue = get_message_queue(1)
                await queue.join()

        assert mock_bot.send_message.call_count == 1
        assert mock_bot.edit_message_text.call_count == 2
        assert _thinking_turns[(1, 42)].blocks == ["one", "two", "three"]

    @pytest.mark.asyncio
    async def test_reset_thinking_turn_starts_a_fresh_message_for_next_turn(
        self, mock_bot
    ):
        with patch("ccbot.handlers.message_queue.session_manager") as mock_sm:
            mock_sm.resolve_chat_id.return_value = 100
            await enqueue_thinking_update(
                mock_bot, 1, "@1", _quoted("turn one thought"), thread_id=42
            )
            queue = get_message_queue(1)
            await queue.join()

            reset_thinking_turn(1, 42)

            second_sent = MagicMock()
            second_sent.message_id = 999
            mock_bot.send_message.return_value = second_sent
            await enqueue_thinking_update(
                mock_bot, 1, "@1", _quoted("turn two thought"), thread_id=42
            )
            await queue.join()

        assert mock_bot.send_message.call_count == 2  # one per turn
        mock_bot.edit_message_text.assert_not_called()  # never edited across turns
        state = _thinking_turns[(1, 42)]
        assert state.message_id == 999
        assert state.blocks == ["turn two thought"]

    @pytest.mark.asyncio
    async def test_different_threads_track_independent_turns(self, mock_bot):
        with patch("ccbot.handlers.message_queue.session_manager") as mock_sm:
            mock_sm.resolve_chat_id.return_value = 100
            await enqueue_thinking_update(
                mock_bot, 1, "@1", _quoted("thread A thought"), thread_id=1
            )
            await enqueue_thinking_update(
                mock_bot, 1, "@2", _quoted("thread B thought"), thread_id=2
            )
            await get_message_queue(1).join()

        assert mock_bot.send_message.call_count == 2
        assert _thinking_turns[(1, 1)].blocks == ["thread A thought"]
        assert _thinking_turns[(1, 2)].blocks == ["thread B thought"]

    @pytest.mark.asyncio
    async def test_empty_thinking_block_is_a_noop(self, mock_bot):
        with patch("ccbot.handlers.message_queue.session_manager") as mock_sm:
            mock_sm.resolve_chat_id.return_value = 100
            await enqueue_thinking_update(mock_bot, 1, "@1", "", thread_id=42)
            await get_message_queue(1).join()

        mock_bot.send_message.assert_not_called()
        assert (1, 42) not in _thinking_turns
