"""Tests for terminal_parser — regex-based detection of Claude Code UI elements."""

import pytest

from ccbot.terminal_parser import (
    extract_bash_output,
    extract_interactive_content,
    format_pane_text_block,
    is_interactive_ui,
    is_unrecognized_dialog,
    parse_focused_option,
    parse_numbered_options,
    parse_option_states,
    parse_status_line,
    strip_pane_chrome,
)

# ── parse_status_line ────────────────────────────────────────────────────


class TestParseStatusLine:
    @pytest.mark.parametrize(
        ("spinner", "rest", "expected"),
        [
            ("·", "Working on task", "Working on task"),
            ("✻", "  Reading file  ", "Reading file"),
            ("✽", "Thinking deeply", "Thinking deeply"),
            ("✶", "Analyzing code", "Analyzing code"),
            ("✳", "Processing input", "Processing input"),
            ("✢", "Building project", "Building project"),
        ],
    )
    def test_spinner_chars(self, spinner: str, rest: str, expected: str, chrome: str):
        pane = f"some output\n{spinner}{rest}\n{chrome}"
        assert parse_status_line(pane) == expected

    @pytest.mark.parametrize(
        "pane",
        [
            pytest.param("just normal text\nno spinners here\n", id="no_spinner"),
            pytest.param("", id="empty"),
        ],
    )
    def test_returns_none(self, pane: str):
        assert parse_status_line(pane) is None

    def test_no_chrome_returns_none(self):
        """Without chrome separator, status can't be determined."""
        pane = "output\n✻ Doing work\nno chrome here\n"
        assert parse_status_line(pane) is None

    def test_blank_line_between_status_and_chrome(self, chrome: str):
        """Status line with blank lines before separator."""
        pane = f"output\n✻ Doing work\n\n{chrome}"
        assert parse_status_line(pane) == "Doing work"

    def test_idle_no_status(self, chrome: str):
        """Idle pane (no status line above chrome) returns None."""
        pane = f"some output\n● Tool result\n{chrome}"
        assert parse_status_line(pane) is None

    def test_false_positive_bullet(self, chrome: str):
        """· in regular output must NOT be detected as status."""
        pane = f"· bullet point one\n· bullet point two\nsome result\n{chrome}"
        assert parse_status_line(pane) is None

    def test_uses_fixture(self, sample_pane_status_line: str):
        assert parse_status_line(sample_pane_status_line) == "Reading file src/main.py"


# ── extract_interactive_content ──────────────────────────────────────────


class TestExtractInteractiveContent:
    def test_exit_plan_mode(self, sample_pane_exit_plan: str):
        result = extract_interactive_content(sample_pane_exit_plan)
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "Would you like to proceed?" in result.content
        assert "ctrl-g to edit in" in result.content

    def test_exit_plan_mode_variant(self):
        pane = (
            "  Claude has written up a plan\n  ─────\n  Details here\n  Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "Claude has written up a plan" in result.content

    def test_ask_user_multi_tab(self, sample_pane_ask_user_multi_tab: str):
        result = extract_interactive_content(sample_pane_ask_user_multi_tab)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "←" in result.content

    def test_ask_user_single_tab(self, sample_pane_ask_user_single_tab: str):
        result = extract_interactive_content(sample_pane_ask_user_single_tab)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "Enter to select" in result.content

    def test_ask_user_multiselect(self, sample_pane_ask_user_multiselect: str):
        result = extract_interactive_content(sample_pane_ask_user_multiselect)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "[✔] Cherry" in result.content

    def test_permission_prompt(self, sample_pane_permission: str):
        result = extract_interactive_content(sample_pane_permission)
        assert result is not None
        assert result.name == "PermissionPrompt"
        assert "Do you want to proceed?" in result.content

    def test_restore_checkpoint(self):
        pane = (
            "  Restore the code to a previous state?\n"
            "  ─────\n"
            "  Some details\n"
            "  Enter to continue\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "RestoreCheckpoint"
        assert "Restore the code" in result.content

    def test_settings(self):
        pane = "  Settings: press tab to cycle\n  ─────\n  Option 1\n  Esc to cancel\n"
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Settings:" in result.content

    def test_settings_model_picker(self, sample_pane_settings: str):
        result = extract_interactive_content(sample_pane_settings)
        assert result is not None
        assert result.name == "Settings"
        assert "Select model" in result.content
        assert "Sonnet" in result.content
        assert "Enter to confirm" in result.content

    def test_settings_esc_to_cancel_bottom(self):
        pane = (
            "  Settings: press tab to cycle\n"
            "  ─────\n"
            "  Model\n"
            "  ─────\n"
            "  ● claude-sonnet-4-20250514\n"
            "  ○ claude-opus-4-20250514\n"
            "  Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Esc to cancel" in result.content

    def test_settings_esc_to_exit_bottom(self):
        pane = (
            "  Settings: press tab to cycle\n"
            "  ─────\n"
            "  Model\n"
            "  ─────\n"
            "  ● Default (Opus 4.6)\n"
            "  ○ claude-sonnet-4-20250514\n"
            "\n"
            "  Enter to confirm · Esc to exit\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Enter to confirm" in result.content

    @pytest.mark.parametrize(
        "pane",
        [
            pytest.param("$ echo hello\nhello\n$\n", id="no_ui"),
            pytest.param("", id="empty"),
        ],
    )
    def test_returns_none(self, pane: str):
        assert extract_interactive_content(pane) is None

    def test_min_gap_too_small_returns_none(self):
        pane = "  Do you want to proceed?\n  Esc to cancel\n"
        assert extract_interactive_content(pane) is None


# ── parse_numbered_options / parse_option_states / parse_focused_option ──


class TestParseNumberedOptions:
    def test_single_select_labels_unchanged(self, sample_pane_settings: str):
        """A single-select dialog has no checkboxes to strip — labels come
        out exactly as printed."""
        content = extract_interactive_content(sample_pane_settings)
        assert content is not None
        options = parse_numbered_options(content.content)
        assert (2, "Sonnet                 Sonnet 4.6 · Best for everyday tasks") in (
            options
        )

    def test_multiselect_strips_checkboxes(self, sample_pane_ask_user_multiselect: str):
        options = parse_numbered_options(sample_pane_ask_user_multiselect)
        assert options == [
            (1, "Apple"),
            (2, "Banana"),
            (3, "Cherry"),
            (4, "Type something"),
            (5, "Chat about this"),
        ]

    @pytest.mark.parametrize(
        ("box", "checked"),
        [
            ("[ ]", False),
            ("[]", False),
            ("[x]", True),
            ("[X]", True),
            ("[✔]", True),
            ("[✓]", True),
            ("[☑]", True),
        ],
    )
    def test_checkbox_variants(self, box: str, checked: bool):
        content = f"1. {box} Mango\n"
        assert parse_numbered_options(content) == [(1, "Mango")]
        assert parse_option_states(content) == {1: checked}

    def test_focused_checked_row(self):
        """`❯ 4. [✔] Mango` parses as number=4, label='Mango', state=True."""
        content = "❯ 4. [✔] Mango\n"
        assert parse_numbered_options(content) == [(4, "Mango")]
        assert parse_option_states(content) == {4: True}
        assert parse_focused_option(content) == 4


class TestParseOptionStates:
    def test_mixed_checked_and_unchecked(self, sample_pane_ask_user_multiselect: str):
        states = parse_option_states(sample_pane_ask_user_multiselect)
        assert states == {1: False, 2: False, 3: True, 4: False}
        # "5. Chat about this" carries no checkbox — a plain fallback option.
        assert 5 not in states

    def test_single_select_has_no_states(self, sample_pane_ask_user_single_tab: str):
        content = extract_interactive_content(sample_pane_ask_user_single_tab)
        assert content is not None
        assert parse_option_states(content.content) == {}

    def test_submit_review_screen_has_no_states(
        self, sample_pane_ask_user_multiselect_submit: str
    ):
        """The Submit tab re-lists options as plain 'N. label' — no
        checkboxes, so states must come back empty even though this is a
        multiSelect dialog."""
        assert parse_option_states(sample_pane_ask_user_multiselect_submit) == {}


class TestParseFocusedOption:
    def test_returns_focused_row_number(self, sample_pane_ask_user_multiselect: str):
        assert parse_focused_option(sample_pane_ask_user_multiselect) == 1

    def test_none_when_no_focus_marker(self):
        assert parse_focused_option("1. Apple\n2. Banana\n") is None

    def test_none_for_empty_content(self):
        assert parse_focused_option("") is None


# ── is_interactive_ui ────────────────────────────────────────────────────


class TestIsInteractiveUI:
    def test_true_when_ui_present(self, sample_pane_exit_plan: str):
        assert is_interactive_ui(sample_pane_exit_plan) is True

    def test_false_when_no_ui(self, sample_pane_no_ui: str):
        assert is_interactive_ui(sample_pane_no_ui) is False

    def test_settings_is_interactive(self, sample_pane_settings: str):
        assert is_interactive_ui(sample_pane_settings) is True

    def test_false_for_empty_string(self):
        assert is_interactive_ui("") is False


# ── is_unrecognized_dialog ───────────────────────────────────────────────


class TestIsUnrecognizedDialog:
    def test_square_corner_box_detected(self):
        """A closed square-corner box (e.g. `claude --resume` session
        picker) with no matching UI_PATTERNS is flagged."""
        pane = (
            "┌─ Resume Session ──────────────┐\n"
            "│ 1. feature-branch  2h ago      │\n"
            "│ 2. main            1d ago      │\n"
            "└────────────────────────────────┘\n"
        )
        assert is_unrecognized_dialog(pane) is True

    def test_rounded_corner_box_detected(self):
        """Rounded-corner box style (╭╮╰╯) is also detected."""
        pane = (
            "╭─ Login ───────────────────╮\n"
            "│ Visit https://example.com  │\n"
            "│ Enter code: ABCD-1234      │\n"
            "╰─────────────────────────────╯\n"
        )
        assert is_unrecognized_dialog(pane) is True

    def test_box_with_leading_whitespace_detected(self):
        pane = "   ┌─ Trust this folder? ─┐\n   │ Yes / No              │\n   └────────────────────────┘\n"
        assert is_unrecognized_dialog(pane) is True

    def test_no_box_returns_false(self, sample_pane_no_ui: str):
        assert is_unrecognized_dialog(sample_pane_no_ui) is False

    def test_empty_string_returns_false(self):
        assert is_unrecognized_dialog("") is False

    def test_normal_chrome_only_returns_false(self, chrome: str):
        """Claude Code's own chrome uses flat '─' rules, no corner glyphs."""
        pane = "some output\nmore output\n" + chrome
        assert is_unrecognized_dialog(pane) is False

    def test_top_border_without_bottom_returns_false(self):
        """An unpaired top corner alone is not enough — conservative by
        design (avoid flagging a stray glyph in scrollback text)."""
        pane = "┌─ Something ─┐\nsome text below, no closing border\n"
        assert is_unrecognized_dialog(pane) is False

    def test_bottom_border_without_top_returns_false(self):
        pane = "some text above, no opening border\n└─ Something ─┘\n"
        assert is_unrecognized_dialog(pane) is False

    def test_bottom_before_top_returns_false(self):
        """Corner glyphs must pair in top-then-bottom order."""
        pane = "└─ closing first ─┘\nsome text\n┌─ opening later ─┐\n"
        assert is_unrecognized_dialog(pane) is False

    @pytest.mark.parametrize(
        "fixture_name",
        [
            "sample_pane_exit_plan",
            "sample_pane_ask_user_multi_tab",
            "sample_pane_ask_user_single_tab",
            "sample_pane_permission",
            "sample_pane_settings",
            "sample_pane_status_line",
        ],
    )
    def test_no_false_positive_on_realistic_panes(
        self, fixture_name: str, request: pytest.FixtureRequest
    ):
        """No box-drawn corners in any of these real captures — either a
        known UI (defers to its own specific handler) or plain status
        output, neither should ever trip the generic fallback."""
        pane = request.getfixturevalue(fixture_name)
        assert is_unrecognized_dialog(pane) is False


# ── strip_pane_chrome ───────────────────────────────────────────────────


class TestStripPaneChrome:
    def test_strips_from_separator(self):
        lines = [
            "some output",
            "more output",
            "─" * 30,
            "❯",
            "─" * 30,
            "  [Opus 4.6] Context: 34%",
        ]
        assert strip_pane_chrome(lines) == ["some output", "more output"]

    def test_no_separator_returns_all(self):
        lines = ["line 1", "line 2", "line 3"]
        assert strip_pane_chrome(lines) == lines

    def test_short_separator_not_triggered(self):
        lines = ["output", "─" * 10, "more output"]
        assert strip_pane_chrome(lines) == lines

    def test_only_searches_last_10_lines(self):
        # Separator at line 0 with 15 lines total — outside the last-10 window
        lines = ["─" * 30] + [f"line {i}" for i in range(14)]
        assert strip_pane_chrome(lines) == lines


# ── extract_bash_output ─────────────────────────────────────────────────


class TestExtractBashOutput:
    def test_extracts_command_output(self):
        pane = "some context\n! echo hello\n⎿ hello\n"
        result = extract_bash_output(pane, "echo hello")
        assert result is not None
        assert "! echo hello" in result
        assert "hello" in result

    def test_command_not_found_returns_none(self):
        pane = "some context\njust normal output\n"
        assert extract_bash_output(pane, "echo hello") is None

    def test_chrome_stripped(self):
        pane = (
            "some context\n"
            "! ls\n"
            "⎿ file.txt\n"
            + "─" * 30
            + "\n"
            + "❯\n"
            + "─" * 30
            + "\n"
            + "  [Opus 4.6] Context: 34%\n"
        )
        result = extract_bash_output(pane, "ls")
        assert result is not None
        assert "file.txt" in result
        assert "Opus" not in result

    def test_prefix_match_long_command(self):
        pane = "! long_comma…\n⎿ output\n"
        result = extract_bash_output(pane, "long_command_that_gets_truncated")
        assert result is not None
        assert "output" in result

    def test_trailing_blank_lines_stripped(self):
        pane = "! echo hi\n⎿ hi\n\n\n"
        result = extract_bash_output(pane, "echo hi")
        assert result is not None
        assert not result.endswith("\n")

    def test_plain_shell_fallback_matches_prompt_echo(self):
        """FEATURE 2 verification: a window with no Claude session (no '!
        cmd' echo) falls back to matching the shell's own prompt-echoed
        command line — the mechanism behind plain-shell topics' echo
        capture. Keyed purely on pane text content; extract_bash_output
        takes no window_index, so BUG 1's placeholder renumbering (real
        windows shifting to start at index 1 instead of 2) cannot affect it
        — this path is window_id-only, same as capture_pane."""
        pane = "$ echo hello\nhello\n$ "
        result = extract_bash_output(pane, "echo hello")
        assert result is not None
        assert "echo hello" in result
        assert "hello" in result


# ── format_pane_text_block ───────────────────────────────────────────────


class TestFormatPaneTextBlock:
    def test_wraps_in_fenced_code_block(self):
        result = format_pane_text_block("hello\nworld")
        assert result == "```\nhello\nworld\n```"

    def test_strips_trailing_blank_lines(self):
        result = format_pane_text_block("hello\nworld\n\n   \n\n")
        assert result == "```\nhello\nworld\n```"

    def test_short_text_unchanged(self):
        text = "line1\nline2\nline3"
        result = format_pane_text_block(text, limit=3500)
        assert result == f"```\n{text}\n```"

    def test_caps_length_keeping_bottom(self):
        # 100 numbered lines; only the tail should survive a small limit.
        lines = [f"line {i}" for i in range(100)]
        text = "\n".join(lines)

        result = format_pane_text_block(text, limit=50)

        assert "line 99" in result  # most recent output kept
        assert "line 0\n" not in result  # earliest output cut
        # fence + capped body: bounded well above the raw limit, nowhere
        # near the uncapped ~700-char full text.
        assert len(result) < 100

    def test_default_limit_is_3500(self):
        text = "x" * 10_000
        result = format_pane_text_block(text)
        # 3500 chars of body + the two fence lines ("```\n" and "\n```")
        assert len(result) == 3500 + len("```\n") + len("\n```")
