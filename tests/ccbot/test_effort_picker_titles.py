"""Tests for the 2026-07-05 UX batch: numbered-option buttons on interactive
UIs, the redesigned session picker (full titles + All view), ai-title
preference for conversation titles, and the /effort level keyboard."""

import json
from unittest.mock import patch

import pytest

from ccbot import session as session_module
from ccbot.handlers.callback_data import (
    CB_ASK_NUM,
    CB_EFFORT_CANCEL,
    CB_EFFORT_SET,
    CB_SESSION_ALL,
    CB_SESSION_CANCEL,
    CB_SESSION_NEW,
    CB_SESSION_SELECT,
)
from ccbot.handlers.directory_browser import build_session_picker
from ccbot.handlers.interactive_ui import _build_interactive_keyboard
from ccbot.session import ClaudeSession, SessionManager
from ccbot.terminal_parser import parse_numbered_options

DIALOG = """\
 ☐ ccbot version

Is origin/auto-topic-mirror @ bff95db your current ccbot version?

❯ 1. bff95db is current - proceed
     GitHub auto-topic-mirror is up to date; build the /effort command,
     picker UI redesign, and ai-title fix on top of it.
  2. Mac has unpushed work
     You will push the latest from the Mac first.
  3. Different repo/branch
     Your current version lives in another repo or branch.
  4. Type something.
─────
  5. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel"""


class TestParseNumberedOptions:
    def test_extracts_options_in_order(self) -> None:
        options = parse_numbered_options(DIALOG)
        assert [n for n, _ in options] == [1, 2, 3, 4, 5]
        assert options[0][1] == "bff95db is current - proceed"
        assert options[3][1] == "Type something."
        assert options[4][1] == "Chat about this"

    def test_description_lines_ignored(self) -> None:
        labels = [label for _, label in parse_numbered_options(DIALOG)]
        assert not any("auto-topic-mirror is up to date" in label for label in labels)

    def test_empty_content(self) -> None:
        assert parse_numbered_options("") == []
        assert parse_numbered_options("no options here") == []


# 80x24 pane + pinned task list: the "☐ <header>" line and question text are
# clipped above the viewport (captured live 2026-07-05 14:10). Must still be
# detected as AskUserQuestion — NOT PermissionPrompt (option 1 starting with
# "Yes" used to trip that pattern) and NOT nothing (this morning's 13-minute
# undelivered question).
CLIPPED_DIALOG = """\
topic with tappable numbered option buttons?
❯ 1. Yes - buttons in Telegram
     The dialog showed up in the topic with one button per option and you are
     answering by tapping one.
  2. No - answering from terminal
     Nothing appeared in Telegram (again); you are answering in the terminal
     like before.
  3. Appeared but broken
     Something showed up in Telegram but buttons are missing, mislabeled, or
     don't work.
  4. Type something.
────────────────────────────────────────────────────────────────────────────────
  5. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel
  5 tasks (4 done, 1 open)
  ✔ Add interactive /effort command to ccbot"""


class TestClippedDialogDetection:
    def test_detected_as_ask_user_question(self) -> None:
        from ccbot.terminal_parser import extract_interactive_content

        content = extract_interactive_content(CLIPPED_DIALOG)
        assert content is not None
        assert content.name == "AskUserQuestion"
        options = parse_numbered_options(content.content)
        assert [n for n, _ in options] == [1, 2, 3, 4, 5]

    def test_plain_numbered_list_without_footer_not_detected(self) -> None:
        from ccbot.terminal_parser import extract_interactive_content

        prose = "Here is my plan:\n1. First step\n2. Second step\n3. Third step\ndone."
        assert extract_interactive_content(prose) is None


class TestInteractiveKeyboardOptions:
    def test_option_rows_come_first_one_per_row(self) -> None:
        options = parse_numbered_options(DIALOG)
        kb = _build_interactive_keyboard("@4", "AskUserQuestion", options)
        rows = kb.inline_keyboard
        # 5 option rows + nav row + action row
        assert len(rows) == 7
        for i in range(5):
            assert len(rows[i]) == 1
            assert rows[i][0].callback_data == f"{CB_ASK_NUM}{i + 1}:@4"

    def test_text_input_options_marked(self) -> None:
        options = parse_numbered_options(DIALOG)
        kb = _build_interactive_keyboard("@4", "AskUserQuestion", options)
        labels = [row[0].text for row in kb.inline_keyboard[:5]]
        assert labels[3].startswith("✏️ 4.")
        assert labels[4].startswith("✏️ 5.")
        assert labels[0].startswith("1.")

    def test_no_options_still_has_nav_and_actions(self) -> None:
        kb = _build_interactive_keyboard("@4", "PermissionPrompt", [])
        all_data = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert not any(d.startswith(CB_ASK_NUM) for d in all_data)
        assert any(d.startswith("aq:enter:") for d in all_data)


def _mk_session(i: int, summary: str, path: str = "/tmp/nonexistent") -> ClaudeSession:
    return ClaudeSession(
        session_id=f"sid-{i}", summary=summary, message_count=i + 1, file_path=path
    )


class TestSessionPickerRedesign:
    def test_one_button_per_session_full_row(self) -> None:
        sessions = [_mk_session(i, f"Conversation about topic {i}") for i in range(4)]
        text, kb = build_session_picker(sessions, total_count=4)
        rows = kb.inline_keyboard
        assert len(rows) == 5  # 4 sessions + action row
        for i in range(4):
            assert len(rows[i]) == 1
            assert rows[i][0].callback_data == f"{CB_SESSION_SELECT}{i}"
            assert f"Conversation about topic {i}" in rows[i][0].text
        assert f"Conversation about topic {i}" in text

    def test_all_button_when_more_exist(self) -> None:
        sessions = [_mk_session(i, f"Chat {i}") for i in range(10)]
        _, kb = build_session_picker(sessions, total_count=25)
        action_row = kb.inline_keyboard[-1]
        data = [b.callback_data for b in action_row]
        assert CB_SESSION_ALL in data
        assert CB_SESSION_NEW in data
        assert CB_SESSION_CANCEL in data
        all_btn = next(b for b in action_row if b.callback_data == CB_SESSION_ALL)
        assert "25" in all_btn.text

    def test_no_all_button_when_all_shown(self) -> None:
        sessions = [_mk_session(i, f"Chat {i}") for i in range(3)]
        _, kb = build_session_picker(sessions, total_count=3)
        data = [b.callback_data for b in kb.inline_keyboard[-1]]
        assert CB_SESSION_ALL not in data

    def test_no_all_button_in_all_view(self) -> None:
        sessions = [_mk_session(i, f"Chat {i}") for i in range(25)]
        text, kb = build_session_picker(sessions, showing_all=True)
        data = [b.callback_data for b in kb.inline_keyboard[-1]]
        assert CB_SESSION_ALL not in data
        assert "All conversations" in text

    def test_long_title_truncated_on_button_not_in_text(self) -> None:
        long_title = "A very long conversation title that goes on and on " * 3
        sessions = [_mk_session(0, long_title.strip())]
        text, kb = build_session_picker(sessions, total_count=1)
        assert len(kb.inline_keyboard[0][0].text) <= 50
        assert long_title.strip() in text


@pytest.fixture
def mgr(tmp_path, monkeypatch: pytest.MonkeyPatch) -> SessionManager:
    monkeypatch.setattr(session_module.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(
        session_module.config, "claude_projects_path", tmp_path / "projects"
    )
    return SessionManager()


def _write_jsonl(path, entries) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


class TestAiTitlePreference:
    @pytest.mark.asyncio
    async def test_ai_title_wins_over_summary_and_user_msg(
        self, mgr: SessionManager, tmp_path
    ) -> None:
        cwd = "/home/u/proj"
        project_dir = tmp_path / "projects" / "-home-u-proj"
        _write_jsonl(
            project_dir / "abc.jsonl",
            [
                {"type": "summary", "summary": "Old generic summary"},
                {"type": "user", "message": {"role": "user", "content": "hey"}},
                {"type": "ai-title", "aiTitle": "First title", "sessionId": "abc"},
                {
                    "type": "ai-title",
                    "aiTitle": "Deploy GPU STT service",
                    "sessionId": "abc",
                },
            ],
        )
        s = await mgr._get_session_direct("abc", cwd)
        assert s is not None
        assert s.summary == "Deploy GPU STT service"

    @pytest.mark.asyncio
    async def test_fallback_order_without_ai_title(
        self, mgr: SessionManager, tmp_path
    ) -> None:
        cwd = "/home/u/proj"
        project_dir = tmp_path / "projects" / "-home-u-proj"
        _write_jsonl(
            project_dir / "abc.jsonl",
            [{"type": "summary", "summary": "Summary title"}],
        )
        s = await mgr._get_session_direct("abc", cwd)
        assert s is not None
        assert s.summary == "Summary title"

    @pytest.mark.asyncio
    async def test_list_limit_and_all(self, mgr: SessionManager, tmp_path) -> None:
        cwd = "/home/u/proj"
        project_dir = tmp_path / "projects" / "-home-u-proj"
        for i in range(14):
            _write_jsonl(
                project_dir / f"s{i:02d}.jsonl",
                [
                    {
                        "type": "ai-title",
                        "aiTitle": f"Chat {i}",
                        "sessionId": f"s{i:02d}",
                    }
                ],
            )
        default = await mgr.list_sessions_for_directory(cwd)
        assert len(default) == 10
        everything = await mgr.list_sessions_for_directory(cwd, limit=None)
        assert len(everything) == 14
        assert mgr.count_session_files_for_directory(cwd) == 14


class TestEffortKeyboard:
    def test_levels_and_current_marker(self) -> None:
        from ccbot import bot as bot_module

        with patch.object(bot_module, "_read_default_effort", return_value="xhigh"):
            kb = bot_module._build_effort_keyboard()
        buttons = [b for row in kb.inline_keyboard for b in row]
        data = [b.callback_data for b in buttons]
        for level in bot_module.EFFORT_LEVELS:
            assert f"{CB_EFFORT_SET}{level}" in data
        assert CB_EFFORT_CANCEL in data
        marked = [b.text for b in buttons if b.text.startswith("✓")]
        assert marked == ["✓ xhigh"]
