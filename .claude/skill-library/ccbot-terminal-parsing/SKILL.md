---
name: ccbot-terminal-parsing
description: The terminal-parser layer of ccbot and how to survive Claude Code TUI churn — interactive-UI detection heuristics (AskUserQuestion tabs, Edit-permission patterns, status-line chrome anchoring), the conservative closed-box-border is_unrecognized_dialog heuristic, the screenshot dialog-fallback flow (render the pane to PNG and send it when parsing fails), ANSI handling at the parse layer, and the adaptation procedure when Claude Code's TUI changes and detection silently breaks. Load this when: the bot stops detecting prompts/dialogs/questions after a Claude Code update; interactive UIs stop rendering as buttons; dialogs are missed or false-positive; touching terminal_parser.py or screenshot.py/dialog_fallback.py; or adding detection for a new TUI element. Keywords: terminal parser, TUI, dialog detection, box border, unrecognized dialog, screenshot fallback, AskUserQuestion, chrome anchor, ANSI, Claude Code update broke. Does NOT cover: what happens to parsed messages downstream (ccbot-message-flow-debugging), tmux pane mechanics (ccbot-tmux-lifecycle), or fonts/rendering internals of the screenshot module beyond operational use.
---

# ccbot-terminal-parsing

**Use when** TUI detection breaks or you're extending it. **Do NOT use when** messages parse fine but flow wrong (→ ccbot-message-flow-debugging).

## The problem this layer fights

ccbot reads Claude Code's TUI by scraping tmux panes. Claude Code's TUI changes without notice; every detection here is a heuristic that **breaks silently** — history shows repeated patch-on-patch (upstream commits 2cdbaad, ab564c4, fb63bc3, 8452440, 49bf869). Treat this layer as adversarial territory: every heuristic needs its near-miss negative test.

## The detection stack (terminal_parser.py)

| Detector | Mechanism | Fragility |
|---|---|---|
| Interactive UIs (AskUserQuestion, permission prompts) | pattern anchors: tab characters in option rows, Edit-permission phrasing, **status-line chrome anchoring** (locate the bottom chrome, parse relative to it — 49bf869) | breaks when Claude Code moves/renames chrome |
| Unrecognized dialog | **closed box-drawn border** heuristic (comment :200+, functions :213–:226): a paired top corner line (`_is_box_top`) + bottom (`_is_box_bottom`) — the comment documents why: Claude Code's own output uses plain `─` rules WITHOUT corner glyphs, so paired corners reliably mean some OTHER full-screen TUI element | deliberately conservative: misses borderless dialogs by design (fallback catches UX, not detection) |
| Shell echo (mirror windows) | line-ends-with-injected-text heuristic (:346, ponytail-marked known-fragile) | → ccbot-message-flow-debugging owns its symptoms |
| ANSI | stripped at the parse layer the CORRECT way (upstream re-did this after a bad first attempt was reverted — 101824e reverting 9587189, redone in 70183a0) | don't strip earlier in the pipeline |

## The safety net: screenshot dialog fallback (fork)

When a dialog is detected but not parseable (`is_unrecognized_dialog`), the bot renders the pane to a PNG (screenshot.py, bundled fonts) and sends the IMAGE with reply options (handlers/dialog_fallback.py) — the user sees exactly what the terminal shows even when parsing can't. Also user-invokable as text-first via `/term` (`d6f6a34`). **This is why detection can afford to be conservative** — prefer a fallback screenshot over a wrong parse.

## When Claude Code updates and detection breaks (the procedure)

1. Reproduce: open the affected TUI state in a real window; `tmux capture-pane -p -t <window>` and SAVE that capture — it's your new test fixture.
2. Diff the capture against the old fixture — what chrome/glyphs moved?
3. Adjust the anchor pattern minimally; add the capture as a table-driven test case (positive + the old shape as regression).
4. Drill on a throwaway socket (→ ccbot-testing-and-qa), then live phone drill on the real dialog.
5. If unparseable → confirm the box-border fallback fires (that's a pass state, not a failure).

## Provenance and maintenance
- Written 2026-07-04 from: terminal_parser.py (:207–:224 box heuristic + its comment, :346), screenshot.py + dialog_fallback.py, commits 49bf869/7712594/d6f6a34, upstream ANSI history (101824e/9587189/70183a0).
- Re-verify: `grep -n '_is_box_top\|_is_box_bottom' src/ccbot/terminal_parser.py` · `ls src/ccbot/fonts/` · after any Claude Code update: run the phone drill on one AskUserQuestion prompt.
