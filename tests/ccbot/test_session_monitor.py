"""Unit tests for SessionMonitor JSONL reading and offset handling."""

import json

import pytest

from ccbot.monitor_state import TrackedSession
from ccbot.session_monitor import TEXT_FLUSH_QUIET_SECONDS, SessionMonitor
from ccbot.transcript_parser import ParsedEntry


class TestReadNewLinesOffsetRecovery:
    """Tests for _read_new_lines offset corruption recovery."""

    @pytest.fixture
    def monitor(self, tmp_path):
        """Create a SessionMonitor with temp state file."""
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.mark.asyncio
    async def test_mid_line_offset_recovery(self, monitor, tmp_path, make_jsonl_entry):
        """Recover from corrupted offset pointing mid-line."""
        # Create JSONL file with two valid lines
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first message")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second message")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Calculate offset pointing into the middle of line 1
        line1_bytes = len(json.dumps(entry1).encode("utf-8")) // 2
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=line1_bytes,  # Mid-line (corrupted)
        )

        # Read should recover and return empty (offset moved to next line)
        result = await monitor._read_new_lines(session, jsonl_file)

        # Should return empty list (recovery skips to next line, no new content yet)
        assert result == []

        # Offset should now point to start of line 2
        line1_full = len(json.dumps(entry1).encode("utf-8")) + 1  # +1 for newline
        assert session.last_byte_offset == line1_full

    @pytest.mark.asyncio
    async def test_valid_offset_reads_normally(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """Normal reading when offset points to line start."""
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Offset at 0 should read both lines
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        assert len(result) == 2
        assert session.last_byte_offset == jsonl_file.stat().st_size

    @pytest.mark.asyncio
    async def test_truncation_detection(self, monitor, tmp_path, make_jsonl_entry):
        """Detect file truncation and reset offset."""
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(msg_type="assistant", content="content")
        jsonl_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        # Set offset beyond file size (simulates truncation)
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=9999,  # Beyond file size
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        # Should reset offset to 0 and read the line
        assert session.last_byte_offset == jsonl_file.stat().st_size
        assert len(result) == 1


class TestTurnTextBuffering:
    """Tests for _apply_turn_buffering — mid-turn assistant narration is
    buffered per session; only the final text before turn-end passes
    through (see PROGRESS NOISE fix)."""

    @pytest.fixture
    def monitor(self, tmp_path):
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    def test_single_text_block_buffered_not_emitted_immediately(self, monitor):
        entries = [
            ParsedEntry(role="assistant", text="Let me check", content_type="text")
        ]
        result = monitor._apply_turn_buffering("sess1", entries)
        assert result == []
        assert monitor._pending_text["sess1"].text == "Let me check"

    def test_later_text_block_replaces_earlier_buffered_one(self, monitor):
        monitor._apply_turn_buffering(
            "sess1",
            [ParsedEntry(role="assistant", text="narration one", content_type="text")],
        )
        monitor._apply_turn_buffering(
            "sess1",
            [ParsedEntry(role="assistant", text="final answer", content_type="text")],
        )
        assert monitor._pending_text["sess1"].text == "final answer"

    def test_tool_use_and_tool_result_pass_through_untouched(self, monitor):
        entries = [
            ParsedEntry(
                role="assistant",
                text="**Read**(file.py)",
                content_type="tool_use",
                tool_use_id="t1",
                tool_name="Read",
            ),
            ParsedEntry(
                role="assistant",
                text="  ⎿  Read 3 lines",
                content_type="tool_result",
                tool_use_id="t1",
            ),
        ]
        result = monitor._apply_turn_buffering("sess1", entries)
        assert result == entries
        assert "sess1" not in monitor._pending_text

    def test_thinking_passes_through_untouched(self, monitor):
        entries = [
            ParsedEntry(role="assistant", text="deep thought", content_type="thinking")
        ]
        result = monitor._apply_turn_buffering("sess1", entries)
        assert result == entries
        assert "sess1" not in monitor._pending_text

    def test_new_user_message_flushes_buffered_text_before_itself(self, monitor):
        monitor._apply_turn_buffering(
            "sess1",
            [ParsedEntry(role="assistant", text="the answer", content_type="text")],
        )
        user_entry = ParsedEntry(role="user", text="thanks", content_type="text")
        result = monitor._apply_turn_buffering("sess1", [user_entry])

        assert len(result) == 2
        assert result[0].role == "assistant"
        assert result[0].text == "the answer"
        assert result[1] is user_entry
        assert "sess1" not in monitor._pending_text

    def test_new_user_message_with_no_pending_text_passes_through_alone(self, monitor):
        user_entry = ParsedEntry(role="user", text="hi", content_type="text")
        result = monitor._apply_turn_buffering("sess1", [user_entry])
        assert result == [user_entry]

    def test_sessions_buffer_independently(self, monitor):
        monitor._apply_turn_buffering(
            "sess1", [ParsedEntry(role="assistant", text="a1", content_type="text")]
        )
        monitor._apply_turn_buffering(
            "sess2", [ParsedEntry(role="assistant", text="a2", content_type="text")]
        )
        assert monitor._pending_text["sess1"].text == "a1"
        assert monitor._pending_text["sess2"].text == "a2"

    def test_exit_plan_mode_preview_text_not_buffered(self, monitor):
        """ExitPlanMode's plan preview is UI content, not narration — it
        must render immediately, not wait behind turn buffering."""
        entries = [
            ParsedEntry(
                role="assistant", text="Here is my plan...", content_type="text"
            ),
            ParsedEntry(
                role="assistant",
                text="**ExitPlanMode**",
                content_type="tool_use",
                tool_use_id="t1",
                tool_name="ExitPlanMode",
            ),
        ]
        result = monitor._apply_turn_buffering("sess1", entries)
        assert result == entries
        assert "sess1" not in monitor._pending_text


class TestFlushStalePendingText:
    """Tests for _flush_stale_pending_text — the quiet-timer fallback that
    guarantees buffered turn text is never held forever."""

    @pytest.fixture
    def monitor(self, tmp_path):
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    def test_not_flushed_before_quiet_window_elapses(self, monitor):
        monitor._apply_turn_buffering(
            "sess1", [ParsedEntry(role="assistant", text="answer", content_type="text")]
        )
        started_at = monitor._pending_text["sess1"].updated_at

        flushed = monitor._flush_stale_pending_text(
            now=started_at + TEXT_FLUSH_QUIET_SECONDS - 0.1
        )

        assert flushed == []
        assert "sess1" in monitor._pending_text

    def test_flushed_after_quiet_window_elapses(self, monitor):
        monitor._apply_turn_buffering(
            "sess1", [ParsedEntry(role="assistant", text="answer", content_type="text")]
        )
        started_at = monitor._pending_text["sess1"].updated_at

        flushed = monitor._flush_stale_pending_text(
            now=started_at + TEXT_FLUSH_QUIET_SECONDS
        )

        assert len(flushed) == 1
        session_id, entry = flushed[0]
        assert session_id == "sess1"
        assert entry.text == "answer"
        assert entry.role == "assistant"
        assert entry.content_type == "text"
        assert "sess1" not in monitor._pending_text

    def test_no_pending_text_flushes_nothing(self, monitor):
        assert monitor._flush_stale_pending_text() == []


class TestEntryToNewMessage:
    @pytest.fixture
    def monitor(self, tmp_path):
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    def test_builds_new_message_from_entry(self, monitor):
        entry = ParsedEntry(
            role="assistant",
            text="hello",
            content_type="text",
            tool_use_id="t1",
            tool_name="Read",
        )
        msg = monitor._entry_to_new_message("sess1", entry)

        assert msg.session_id == "sess1"
        assert msg.text == "hello"
        assert msg.is_complete is True
        assert msg.content_type == "text"
        assert msg.tool_use_id == "t1"
        assert msg.role == "assistant"
        assert msg.tool_name == "Read"
