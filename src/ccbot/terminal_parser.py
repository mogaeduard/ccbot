"""Terminal output parser — detects Claude Code UI elements in pane text.

Parses captured tmux pane content to detect:
  - Interactive UIs (AskUserQuestion, ExitPlanMode, Permission Prompt,
    RestoreCheckpoint) via regex-based UIPattern matching with top/bottom
    delimiters.
  - Unrecognized dialogs: a conservative fallback for full-screen dialogs
    with no specific parser (claude --resume picker, /login, trust
    prompts, ...) — see is_unrecognized_dialog().
  - Status line (spinner characters + working text) by scanning from bottom up.

All Claude Code text patterns live here. To support a new UI type or
a changed Claude Code version, edit UI_PATTERNS / STATUS_SPINNERS.

Key functions: is_interactive_ui(), extract_interactive_content(),
is_unrecognized_dialog(), parse_status_line(), strip_pane_chrome(),
extract_bash_output(), format_pane_text_block().
"""

import re
from dataclasses import dataclass


@dataclass
class InteractiveUIContent:
    """Content extracted from an interactive UI."""

    content: str  # The extracted display content
    name: str = ""  # Pattern name that matched (e.g. "AskUserQuestion")


@dataclass(frozen=True)
class UIPattern:
    """A text-marker pair that delimits an interactive UI region.

    Extraction scans lines top-down: the first line matching any `top` pattern
    marks the start, the first subsequent line matching any `bottom` pattern
    marks the end.  Both boundary lines are included in the extracted content.

    ``top`` and ``bottom`` are tuples of compiled regexes — any single match
    is sufficient.  This accommodates wording changes across Claude Code
    versions (e.g. a reworded confirmation prompt).
    """

    name: str  # Descriptive label (not used programmatically)
    top: tuple[re.Pattern[str], ...]
    bottom: tuple[re.Pattern[str], ...]
    min_gap: int = 2  # minimum lines between top and bottom (inclusive)


# ── UI pattern definitions (order matters — first match wins) ────────────

UI_PATTERNS: list[UIPattern] = [
    UIPattern(
        name="ExitPlanMode",
        top=(
            re.compile(r"^\s*Would you like to proceed\?"),
            # v2.1.29+: longer prefix that may wrap across lines
            re.compile(r"^\s*Claude has written up a plan"),
        ),
        bottom=(
            re.compile(r"^\s*ctrl-g to edit in "),
            re.compile(r"^\s*Esc to (cancel|exit)"),
        ),
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*←\s+[☐✔☒]"),),  # Multi-tab: no bottom needed
        bottom=(),
        min_gap=1,
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*[☐✔☒]"),),  # Single-tab: bottom required
        bottom=(re.compile(r"^\s*Enter to select"),),
        min_gap=1,
    ),
    UIPattern(
        # Clipped AskUserQuestion: in a short pane (80x24 + pinned task list)
        # the "☐ <header>" line and the question text scroll out of the
        # viewport, leaving only numbered options + the "Enter to select"
        # footer (observed live 2026-07-05, twice). Anchor on the first
        # numbered option instead; the footer requirement keeps ordinary
        # numbered lists in transcript text from matching.
        name="AskUserQuestion",
        top=(re.compile(r"^\s*(?:❯\s*)?\d{1,2}\.\s+\S"),),
        bottom=(re.compile(r"^\s*Enter to select"),),
        min_gap=1,
    ),
    UIPattern(
        name="PermissionPrompt",
        top=(
            re.compile(r"^\s*Do you want to proceed\?"),
            re.compile(r"^\s*Do you want to make this edit"),
            re.compile(r"^\s*Do you want to create \S"),
            re.compile(r"^\s*Do you want to delete \S"),
        ),
        bottom=(re.compile(r"^\s*Esc to cancel"),),
    ),
    UIPattern(
        # Permission menu with numbered choices (no "Esc to cancel" line)
        name="PermissionPrompt",
        top=(re.compile(r"^\s*❯\s*1\.\s*Yes"),),
        bottom=(),
        min_gap=2,
    ),
    UIPattern(
        # Bash command approval
        name="BashApproval",
        top=(
            re.compile(r"^\s*Bash command\s*$"),
            re.compile(r"^\s*This command requires approval"),
        ),
        bottom=(re.compile(r"^\s*Esc to cancel"),),
    ),
    UIPattern(
        name="RestoreCheckpoint",
        top=(re.compile(r"^\s*Restore the code"),),
        bottom=(re.compile(r"^\s*Enter to continue"),),
    ),
    UIPattern(
        name="Settings",
        top=(
            re.compile(r"^\s*Settings:.*tab to cycle"),
            re.compile(r"^\s*Select model"),
        ),
        bottom=(
            re.compile(r"Esc to cancel"),
            re.compile(r"Esc to exit"),
            re.compile(r"Enter to confirm"),
            re.compile(r"^\s*Type to filter"),
        ),
    ),
]


# ── Post-processing ──────────────────────────────────────────────────────

_RE_LONG_DASH = re.compile(r"^─{5,}$")


def _shorten_separators(text: str) -> str:
    """Replace lines of 5+ ─ characters with exactly ─────."""
    return "\n".join(
        "─────" if _RE_LONG_DASH.match(line) else line for line in text.split("\n")
    )


# ── Core extraction ──────────────────────────────────────────────────────


def _try_extract(lines: list[str], pattern: UIPattern) -> InteractiveUIContent | None:
    """Try to extract content matching a single UI pattern.

    When ``pattern.bottom`` is empty, the region extends from the top marker
    to the last non-empty line (used for multi-tab AskUserQuestion where the
    bottom delimiter varies by tab).
    """
    top_idx: int | None = None
    bottom_idx: int | None = None

    for i, line in enumerate(lines):
        if top_idx is None:
            if any(p.search(line) for p in pattern.top):
                top_idx = i
        elif pattern.bottom and any(p.search(line) for p in pattern.bottom):
            bottom_idx = i
            break

    if top_idx is None:
        return None

    # No bottom patterns → use last non-empty line as boundary
    if not pattern.bottom:
        for i in range(len(lines) - 1, top_idx, -1):
            if lines[i].strip():
                bottom_idx = i
                break

    if bottom_idx is None or bottom_idx - top_idx < pattern.min_gap:
        return None

    content = "\n".join(lines[top_idx : bottom_idx + 1]).rstrip()
    return InteractiveUIContent(content=_shorten_separators(content), name=pattern.name)


# ── Public API ───────────────────────────────────────────────────────────


def extract_interactive_content(pane_text: str) -> InteractiveUIContent | None:
    """Extract content from an interactive UI in terminal output.

    Tries each UI pattern in declaration order; first match wins.
    Returns None if no recognizable interactive UI is found.
    """
    if not pane_text:
        return None

    lines = pane_text.strip().split("\n")
    for pattern in UI_PATTERNS:
        result = _try_extract(lines, pattern)
        if result:
            return result
    return None


def is_interactive_ui(pane_text: str) -> bool:
    """Check if terminal currently shows an interactive UI."""
    return extract_interactive_content(pane_text) is not None


# Numbered option line: "❯ 1. Yes", "  2. Type something." — the cursor
# marker is optional, the number is single/double digit, label follows.
# Indented continuation lines (option descriptions) don't match because
# they never start with "<digit>."
_RE_NUMBERED_OPTION = re.compile(r"^\s*(?:❯\s*)?(\d{1,2})\.\s+(\S.*)$")

# multiSelect checkbox glued onto an option label: "[ ] Apple", "[✔] Cherry".
# The character class covers every checked glyph Claude Code renders plus a
# blank (unchecked); tolerant of stray spacing on either side of the glyph.
# A bracket holding anything else (e.g. an ordinary "[Beta] Feature" label)
# won't match, so plain single-select labels pass through untouched.
_RE_CHECKBOX = re.compile(r"^\[\s*([ xX✔✓☑]?)\s*\]\s*(.*)$")
_CHECKED_GLYPHS = frozenset({"x", "X", "✔", "✓", "☑"})


def _parse_option_lines(content: str) -> list[tuple[int, str, bool | None]]:
    """Numbered option lines as (number, clean label, checked-or-None).

    Shared scan behind parse_numbered_options() and parse_option_states().
    ``checked`` is None when the line has no checkbox (single-select),
    else True/False for a checked/unchecked multiSelect box.
    """
    results: list[tuple[int, str, bool | None]] = []
    seen: set[int] = set()
    for line in content.split("\n"):
        m = _RE_NUMBERED_OPTION.match(line)
        if not m:
            continue
        num = int(m.group(1))
        if num in seen:
            continue
        seen.add(num)
        raw_label = m.group(2).strip()
        box = _RE_CHECKBOX.match(raw_label)
        if box:
            results.append((num, box.group(2).strip(), box.group(1) in _CHECKED_GLYPHS))
        else:
            results.append((num, raw_label, None))
    return results


def parse_numbered_options(content: str) -> list[tuple[int, str]]:
    """Extract numbered options from interactive-UI content.

    Claude Code's choice dialogs (AskUserQuestion, permission prompts,
    ExitPlanMode) list options as "N. label" lines, and pressing the digit
    key selects that option directly. Returns (number, first-line label)
    pairs in on-screen order; wrapped label continuations and description
    lines are ignored. Duplicate numbers (shouldn't happen) keep the first.
    multiSelect checkboxes ("[ ] Apple") are stripped from the label.
    """
    return [(num, label) for num, label, _checked in _parse_option_lines(content)]


def parse_option_states(content: str) -> dict[int, bool]:
    """Extract multiSelect checkbox states from interactive-UI content.

    Returns {option_number: checked} for every numbered option that carries
    a checkbox. An empty dict means the screen has no checkboxes at all —
    a single-select dialog, or the multiSelect "review your answers" /
    Submit tab, which re-lists options as plain "N. label" lines.
    """
    return {
        num: checked
        for num, _label, checked in _parse_option_lines(content)
        if checked is not None
    }


# The ❯ cursor marks the focused numbered line — unlike _RE_NUMBERED_OPTION
# (which treats ❯ as optional filler), this only matches when it's present.
_RE_FOCUSED_OPTION = re.compile(r"^\s*❯\s*(\d{1,2})\.\s+\S")


def parse_focused_option(content: str) -> int | None:
    """Number of the ❯-focused option line, or None if nothing is focused.

    Used to steer keyboard focus onto a specific option (e.g. multiSelect's
    inline-editable "Type something" row) by computing how many Up/Down
    presses separate the current focus from the target.
    """
    for line in content.split("\n"):
        m = _RE_FOCUSED_OPTION.match(line)
        if m:
            return int(m.group(1))
    return None


# ── Unrecognized dialog fallback ─────────────────────────────────────────
#
# Detects a full-screen dialog ccbot has no specific parser for (the
# `claude --resume` picker, `/login`, a trust-this-directory prompt, ...).
# Deliberately conservative — a false positive means posting a needless
# screenshot for an ordinary Claude Code screen, which is more disruptive
# than occasionally missing an exotic prompt, so this only fires on a
# strong, low-noise signal: a *closed* box-drawn border (a top corner line
# followed by a matching bottom corner line further down). Claude Code's
# own chrome (see strip_pane_chrome / _RE_LONG_DASH) only ever uses flat
# '─' rules with no corner glyphs, so a paired corner border reliably means
# some other full-screen TUI element is drawing its own box.

_BOX_TOP_LEFT = "┌╭"
_BOX_TOP_RIGHT = "┐╮"
_BOX_BOTTOM_LEFT = "└╰"
_BOX_BOTTOM_RIGHT = "┘╯"


def _is_box_top(line: str) -> bool:
    s = line.strip()
    return len(s) >= 2 and s[0] in _BOX_TOP_LEFT and s[-1] in _BOX_TOP_RIGHT


def _is_box_bottom(line: str) -> bool:
    s = line.strip()
    return len(s) >= 2 and s[0] in _BOX_BOTTOM_LEFT and s[-1] in _BOX_BOTTOM_RIGHT


def is_unrecognized_dialog(pane_text: str) -> bool:
    """True if the pane shows a full-screen dialog with no specific parser.

    Only fires when no known UI_PATTERNS match (is_interactive_ui already
    covers AskUserQuestion/ExitPlanMode/Permission/etc. — those get their
    normal, specific handling) AND the pane contains a closed box-drawn
    border: a line starting with a top-left/top-right corner glyph followed,
    somewhere below, by a line starting with a bottom-left/bottom-right
    corner glyph.
    """
    if not pane_text:
        return False
    if is_interactive_ui(pane_text):
        return False  # already has a specific handler

    lines = pane_text.strip().split("\n")
    top_idx = next((i for i, ln in enumerate(lines) if _is_box_top(ln)), None)
    if top_idx is None:
        return False
    return any(_is_box_bottom(ln) for ln in lines[top_idx + 1 :])


# ── Status line parsing ─────────────────────────────────────────────────

# Spinner characters Claude Code uses in its status line
STATUS_SPINNERS = frozenset(["·", "✻", "✽", "✶", "✳", "✢"])


def parse_status_line(pane_text: str) -> str | None:
    """Extract the Claude Code status line from terminal output.

    The status line (spinner + working text) appears immediately above
    the chrome separator (a full line of ``─`` characters).  We locate
    the separator first, then check the line just above it — this avoids
    false positives from ``·`` bullets in Claude's regular output.

    Returns the text after the spinner, or None if no status line found.
    """
    if not pane_text:
        return None

    lines = pane_text.split("\n")

    # Find the chrome separator: topmost ──── line in the last 10 lines
    chrome_idx: int | None = None
    search_start = max(0, len(lines) - 10)
    for i in range(search_start, len(lines)):
        stripped = lines[i].strip()
        if len(stripped) >= 20 and all(c == "─" for c in stripped):
            chrome_idx = i
            break

    if chrome_idx is None:
        return None  # No chrome visible — can't determine status

    # Check lines just above the separator (skip blanks, up to 4 lines)
    for i in range(chrome_idx - 1, max(chrome_idx - 5, -1), -1):
        line = lines[i].strip()
        if not line:
            continue
        if line[0] in STATUS_SPINNERS:
            return line[1:].strip()
        # First non-empty line above separator isn't a spinner → no status
        return None
    return None


# ── Pane chrome stripping & bash output extraction ─────────────────────


def strip_pane_chrome(lines: list[str]) -> list[str]:
    """Strip Claude Code's bottom chrome (prompt area + status bar).

    The bottom of the pane looks like::

        ────────────────────────  (separator)
        ❯                        (prompt)
        ────────────────────────  (separator)
          [Opus 4.6] Context: 34%
          ⏵⏵ bypass permissions…

    This function finds the topmost ``────`` separator in the last 10 lines
    and strips everything from there down.
    """
    search_start = max(0, len(lines) - 10)
    for i in range(search_start, len(lines)):
        stripped = lines[i].strip()
        if len(stripped) >= 20 and all(c == "─" for c in stripped):
            return lines[:i]
    return lines


def extract_bash_output(pane_text: str, command: str) -> str | None:
    """Extract ``!`` command output from a captured tmux pane.

    Searches from the bottom for the ``! <command>`` echo line, then
    returns that line and everything below it (including the ``⎿`` output).
    Returns *None* if the command echo wasn't found.
    """
    lines = strip_pane_chrome(pane_text.splitlines())

    # Find the last "! <command>" echo line (search from bottom).
    # Match on the first 10 chars of the command in case the line is truncated.
    cmd_idx: int | None = None
    match_prefix = command[:10]
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped.startswith(f"! {match_prefix}") or stripped.startswith(
            f"!{match_prefix}"
        ):
            cmd_idx = i
            break

    if cmd_idx is None:
        # Plain-shell fallback: no "! cmd" echo in a window that isn't
        # running Claude — match the shell's own prompt echo instead, i.e.
        # the last line that ends with the typed command (prompt prefixes
        # vary). ponytail: heuristic — an output line ending with the same
        # text can shadow the echo; worst case the capture starts low.
        wanted = command.strip()
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip().endswith(wanted):
                cmd_idx = i
                break

    if cmd_idx is None:
        return None

    # Include the command echo line and everything after it
    raw_output = lines[cmd_idx:]

    # Strip trailing empty lines
    while raw_output and not raw_output[-1].strip():
        raw_output.pop()

    if not raw_output:
        return None

    return "\n".join(raw_output).strip()


PANE_TEXT_BLOCK_LIMIT = 3500  # keeps a single send well under Telegram's 4096 cap


def format_pane_text_block(text: str, limit: int = PANE_TEXT_BLOCK_LIMIT) -> str:
    """Format captured pane text as a fenced code block for Telegram.

    Strips trailing blank lines (tmux pads a pane's height with them) and
    caps to ``limit`` characters, keeping the BOTTOM — the most recent
    output the user is looking at, not the part that already scrolled by.
    """
    body = text.rstrip()
    if len(body) > limit:
        body = body[-limit:]
    return f"```\n{body}\n```"


# ── Usage modal parsing ──────────────────────────────────────────────────────────


@dataclass
class UsageInfo:
    """Parsed output from Claude Code's /usage modal."""

    raw_text: str  # Full captured pane text
    parsed_lines: list[str]  # Cleaned content lines from the modal


def parse_usage_output(pane_text: str) -> UsageInfo | None:
    """Extract usage information from Claude Code's /usage settings tab.

    The /usage modal shows a Settings overlay with a "Usage" tab containing
    progress bars and reset times.  This parser looks for the Settings header
    line, then collects all content until "Esc to cancel".

    Returns UsageInfo with cleaned lines, or None if not detected.
    """
    if not pane_text:
        return None

    lines = pane_text.strip().split("\n")

    # Find the Settings header that indicates we're in the usage modal
    start_idx: int | None = None
    end_idx: int | None = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if start_idx is None:
            # The usage tab header line
            if "Settings:" in stripped and "Usage" in stripped:
                start_idx = i + 1  # skip the header itself
        else:
            if stripped.startswith("Esc to"):
                end_idx = i
                break

    if start_idx is None:
        return None
    if end_idx is None:
        end_idx = len(lines)

    # Collect content lines, stripping progress bar characters and whitespace
    cleaned: list[str] = []
    for line in lines[start_idx:end_idx]:
        # Strip the line but preserve meaningful content
        stripped = line.strip()
        if not stripped:
            continue
        # Remove progress bar block characters but keep the rest
        # Progress bars are like: █████▋   38% used
        # Strip leading block chars, keep the percentage
        stripped = re.sub(r"^[\u2580-\u259f\s]+", "", stripped).strip()
        if stripped:
            cleaned.append(stripped)

    if cleaned:
        return UsageInfo(raw_text=pane_text, parsed_lines=cleaned)

    return None
