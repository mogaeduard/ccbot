---
name: ccbot-message-flow-debugging
description: The playbook for ccbot's #1 recurring problem class — two-way mirror message-flow fidelity: duplicated messages, missing messages, echoed injections, "thinking" spam, out-of-order or swallowed final answers, rate-limit stalls. Covers the flow anatomy (queue → merge budget → status conversion → rate limiter → send), the echo-dedup one-shot mechanism and its known ceilings, thinking consolidation, final-answer buffering, and the discriminating checks for each symptom. Load this when: a message appears twice, never appears, appears as someone else's, the bot echoes what you typed, thinking blocks spam the topic, a final answer is truncated/missing, or sends stall behind the rate limiter. Keywords: duplicate, echo, missing message, thinking spam, final answer, buffer, dedupe, queue, merge, 3800, rate limit, NewMessage, silent. Does NOT cover: parsing the terminal INTO messages (ccbot-terminal-parsing), Telegram API limits themselves (ccbot-telegram-craft), or window lifecycle (ccbot-tmux-lifecycle).
---

# ccbot-message-flow-debugging

**Use when** messages duplicate/vanish/misbehave. **Do NOT use when** the problem is upstream of messages — TUI parsing (→ ccbot-terminal-parsing) or window binding (→ ccbot-tmux-lifecycle).

## Flow anatomy (where each failure class lives)

monitor (2s poll, mtime cache + byte-offset reads) → parse (NO truncation) → **per-user FIFO queue** with 3800-char merge budget (tool_use/tool_result pairing breaks the merge chain) → status-message conversion/dedup → **AIORateLimiter(max_retries=5, pre-filled bucket on restart)** → send layer (4096 split, MarkdownV2 via safe_*). The mirror adds: shell echo for non-Claude windows, injected-message **echo dedup (90s one-shot window)**, thinking consolidation (send-or-edit), final-answer buffering. Base mechanics are upstream-documented in `.claude/rules/message-handling.md` — read it; this skill owns the fork layer + triage.

## Symptom → discriminating check

| Symptom | Check | Known mechanism |
|---|---|---|
| Your own injected text comes back as a message | did it come back ONCE after >90s, or repeatedly? | echo dedup is ONE-SHOT within its window (`7712594` deliberately made consumption one-shot — a second identical line later is treated as real output; ceiling documented at terminal_parser.py:346: the heuristic is "output line ending with the same text = echo" — known-fragile with prompts that legitimately end by repeating input) |
| Same assistant message twice | was the window restarted / did monitor re-read from offset 0? | mtime-cache reset re-reads the transcript; dedup keys should absorb it — if not, capture BOTH message ids and diff their source lines |
| "Thinking…" spam | consolidation should edit-in-place (`74da8c7`) | if each thinking block is a NEW message, the send-or-edit lookup lost the message id (state.json / restart timing) |
| Final answer missing or half | buffering flushes on completion signal | bot.py:2814's ponytail admits the flow "relies on this NewMessage actually being delivered" — a dropped Telegram send = silent loss; check logs for the send result, then the rate limiter |
| Nothing sends for a while, then a burst | rate limiter | AIORateLimiter retries up to 5; pre-filled bucket after restart smooths the thundering herd — a burst-after-silence is it working, not a bug |
| Message in the WRONG topic | binding, not flow | → ccbot-tmux-lifecycle (window_id re-resolution) |
| Silent losses with no pattern | | status_polling.py:79 ponytail: fixed small poll batch — if binding count grew a lot, the batch may lag; raise it per the comment |

## Rules when fixing here

1. **Reproduce with one window, one message, logs open** (`tail -f ~/.ccbot/logs/*`) before touching code — this area's history is patch-on-patch (`74da8c7` then partially reversed by `7712594`); a fix without a reproduced mechanism joins that history.
2. Preserve the invariants: no parse-layer truncation; single cleanup owner; one-shot echo consumption (both directions of that tradeoff have already been tried — see failure archaeology).
3. Every fix ships a table-driven parser/queue test AND a live phone drill (→ ccbot-testing-and-qa).
4. The three ponytail ceilings here (echo heuristic :346, NewMessage reliance :2814, no cleanup cross-wiring status_polling.py:93) are ACCEPTED debt with documented upgrade paths — upgrading one is a deliberate task, not a drive-by.

## Provenance and maintenance
- Written 2026-07-04 from: commits 74da8c7/7712594/fc9e2f2, terminal_parser.py:346, bot.py:2814, status_polling.py:79/:93, .claude/rules/message-handling.md, config MONITOR_POLL_INTERVAL.
- Re-verify: `grep -n 'ponytail' src/ccbot/bot.py src/ccbot/terminal_parser.py src/ccbot/handlers/status_polling.py` · `git log --oneline -3 -- src/ccbot/handlers/message_queue.py`.
