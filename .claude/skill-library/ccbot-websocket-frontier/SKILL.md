---
name: ccbot-websocket-frontier
description: The research frontier for ccbot — the reverse-engineered Claude Code `--sdk-url` NDJSON-over-WebSocket protocol (doc/WEBSOCKET_PROTOCOL_REVERSED.md, 1,355 lines, from CLI v2.1.37): what it would enable (replacing the entire tmux-scrape + terminal-parser stack with a structured stream), the migration cost/benefit, evaluation criteria for ever switching, and its current status (pure research, zero implementation). Load this when: considering replacing/reducing the terminal-parser layer; a Claude Code update breaks parsing badly enough to re-raise the question; evaluating the SDK/WebSocket route; or someone proposes "just use the API instead of scraping". Keywords: websocket, sdk-url, NDJSON, protocol, frontier, replace tmux scraping, structured stream, research. Does NOT cover: today's parsing reality (ccbot-terminal-parsing), or the Agent SDK as a product question (that's an owner decision).
---

# ccbot-websocket-frontier

**Use when** the scrape-vs-protocol question comes up. Status: **SHELVED RESEARCH** — documented, unimplemented, no roadmap commitment (OPEN QUESTION (owner): ever pursue it?).

## What exists

`doc/WEBSOCKET_PROTOCOL_REVERSED.md` (1,355 lines, upstream commit 71cc989): a reference-grade reverse-engineering of Claude Code's hidden `--sdk-url` mode from CLI v2.1.37 — the CLI connects OUT to a WebSocket server and speaks NDJSON both ways (session lifecycle, messages, tool events, permissions). Read it before any evaluation; do not re-reverse-engineer.

## What switching would buy / cost

| Buy | Cost |
|---|---|
| Kills the entire heuristic layer: terminal_parser TUI detection, screenshot fallback, echo heuristics — structured events instead of scraped panes | The protocol is UNDOCUMENTED-official: it can change without notice at any CLI release (same fragility class as the TUI, different surface) |
| Exact message boundaries, tool events, permission prompts as data | ccbot becomes a WebSocket SERVER per session; the tmux world (windows as the unit of terminal presence, zshrc integration, /term views) still has to exist for HUMAN terminal use — you'd run BOTH stacks |
| No more TUI-churn breakage class | Fork divergence grows enormously; upstream shows no movement here |

## Evaluation criteria (decide by numbers, not frustration)

Re-open this question only when at least one holds:
1. TUI churn breaks parsing >2× in a month (track via failure-archaeology entries).
2. Anthropic DOCUMENTS the protocol or ships an official SDK transport that matches this use (recheck the Agent SDK docs at evaluation time).
3. A feature is impossible by scraping (true structured tool-event streams, e.g.).

Then: prototype ONE window bridged via --sdk-url against the doc'd protocol, run it in parallel with the scrape for a week (same session mirrored both ways), diff fidelity. Adopt only if the diff is clean AND the protocol survived a CLI update during the trial. Otherwise re-shelve with a dated note here.

## Provenance and maintenance
- Written 2026-07-04 from: doc/WEBSOCKET_PROTOCOL_REVERSED.md (existence, scope, origin commit 71cc989), the zero-implementation grep (no sdk-url references in src/).
- Re-verify: `grep -rn 'sdk-url\|sdk_url' src/ | wc -l` (0 = still shelved) · `wc -l doc/WEBSOCKET_PROTOCOL_REVERSED.md` · protocol validity: only testable against the CURRENT CLI version at evaluation time.
