---
name: ccbot-architecture-contract
description: The invariants of ccbot that must never break — 1 Topic = 1 Window = 1 Session with ALL routing keyed by tmux window_id (@N, never window name), topic-only mode, the single-forum-group deployment invariant, single-owner dead-binding cleanup (mirror owns it when enabled, status_polling steps aside), no-truncation-at-parse-layer, MarkdownV2-only via safe_* helpers — PLUS the map of the 6 fork-only modules the pre-existing .claude/rules/*.md docs do not cover (mirror, overnight, dashboard, topic_titles, screenshot, handlers/dialog_fallback). Load this when: changing routing, topic/window lifecycle, message flow ownership, or any module boundary; onboarding to the codebase; or checking whether a change violates an invariant. Keywords: invariants, window_id, topic-only, mirror, reconciler, single owner, forum group, architecture, fork modules. Does NOT cover: upstream architecture detail — READ .claude/rules/architecture.md + topic-architecture.md + message-handling.md FIRST (they are the base contract; this skill only adds what they miss); fork/upstream git relationship (ccbot-fork-vs-upstream); message-flow debugging (ccbot-message-flow-debugging).
---

# ccbot-architecture-contract

**Read `.claude/rules/architecture.md`, `topic-architecture.md`, `message-handling.md` FIRST** — they are the standing base contract (referenced from CLAUDE.md) and are accurate for the upstream core. This skill adds ONLY the invariant summary and the fork-module layer those docs don't cover. **Do NOT use when** you need git fork mechanics (→ ccbot-fork-vs-upstream).

## The invariants (violating any of these = architectural regression)

1. **1 Topic = 1 Window = 1 Session; ALL routing keyed by tmux `window_id` (`@N`), never window name** (CLAUDE.md:19). Names are display-only; upstream's biggest historical refactor was exactly this re-keying — never key on names again.
2. **Topic-only mode:** no active_sessions, no General-topic routing, no backward compat.
3. **Single-forum-group deployment:** all topics live in ONE forum group (`CCBOT_MIRROR_CHAT_ID`); one bot per machine (Telegram allows one getUpdates consumer per token). A second group = a separate bot + separate state, never shared.
4. **Single-owner cleanup:** when the mirror is enabled, mirror.py owns dead-binding cleanup and status_polling deliberately steps aside (its ponytail note documents the non-wiring). Two cleanup owners = the wrong-window-kill class of bug.
5. **No truncation at parse layer** — splitting only at send layer (4096); parse layer preserves everything.
6. **MarkdownV2 only** via safe_reply/safe_edit/safe_send with plain-text fallback (the HTML-converter era was fully reverted upstream — → ccbot-failure-archaeology).
7. Config-gated features default OFF (upstream-friendly): unset `CCBOT_MIRROR_CHAT_ID` disables mirror AND dashboard entirely.

## The fork-module layer (what the rules docs don't cover; 19 fork commits as of 2026-07-04)

| Module | Owns |
|---|---|
| `mirror.py` | the auto-topic mirror: every tmux window ⇄ a numbered forum topic ("Termius-style standing chats" — ALL windows incl. plain shells, a deliberate design change from the original Claude-only plan); the ~2s bidirectional reconciler (`fc9e2f2`) maintaining windows == bound topics in all four sync directions; dead-window reaping; topic deletion (DELETE not close, `c24f6a5`; deletion detection via editForumTopic probe after the TYPING probe proved insufficient — `752bf1f`, `aa64a6d`) |
| `overnight.py` | /sleep quiet-hours + overnight checkpointing of dirty repos to `wip/overnight-*` refs (→ ccbot-overnight-autonomy) |
| `dashboard.py` | the pinned live dashboard message (min-interval 5s) |
| `topic_titles.py` | AI topic auto-rename (128-char limit) |
| `screenshot.py` + `handlers/dialog_fallback.py` | unrecognized-dialog screenshot fallback (→ ccbot-terminal-parsing) |
| bot.py additions | /term text-first terminal view (`d6f6a34`), /speak, /lock,/unlock, /sleep,/wake, /killall, /grab, /mute,/unmute (`aa64a6d`), command menu (`ffa0e8a`) |

Recent invariant-relevant fix to know: the `__main__` placeholder window used to leak a phantom topic and steal terminal 1's slot — fixed `3c94bd6`; the placeholder is now excluded from mirroring.

## Provenance and maintenance
- Written 2026-07-04 (HEAD `aa64a6d`, 19 commits ahead of origin/main) from: CLAUDE.md, .claude/rules/*.md, src module listing, fork commit log (4e01299..aa64a6d), config.py:118.
- Re-verify: `git log origin/main..HEAD --oneline | wc -l` · `ls src/ccbot/` · the rules docs' own accuracy: they predate the fork modules — if upstream rebases ever land, re-check rule 4's ownership split.
