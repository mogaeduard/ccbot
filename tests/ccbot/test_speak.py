"""Tests for /speak's answer-picking helper (last_assistant_text)."""

from ccbot.bot import last_assistant_text


def _m(role: str, ctype: str, text: str) -> dict:
    return {"role": role, "content_type": ctype, "text": text, "timestamp": ""}


class TestLastAssistantText:
    def test_picks_most_recent_assistant_text(self) -> None:
        msgs = [
            _m("assistant", "text", "old answer"),
            _m("user", "text", "question"),
            _m("assistant", "text", "new answer"),
        ]
        assert last_assistant_text(msgs) == "new answer"

    def test_skips_thinking_and_tool_content(self) -> None:
        msgs = [
            _m("assistant", "text", "real answer"),
            _m("assistant", "thinking", "hmm"),
            _m("assistant", "tool_use", "Bash(...)"),
        ]
        assert last_assistant_text(msgs) == "real answer"

    def test_skips_user_and_blank(self) -> None:
        msgs = [
            _m("user", "text", "hello"),
            _m("assistant", "text", "   "),
        ]
        assert last_assistant_text(msgs) is None

    def test_empty_history(self) -> None:
        assert last_assistant_text([]) is None
