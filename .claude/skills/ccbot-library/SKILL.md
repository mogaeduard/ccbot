---
name: ccbot-library
description: ccbot's 16-skill deep reference library (architecture invariants, fork-vs-upstream, message-flow debugging, terminal parsing, tmux lifecycle, Telegram craft, voice stack, overnight autonomy, integration surface, config catalog, build/env, run/operate, testing, change control, failure archaeology, websocket frontier). NOT auto-loaded. Use before any non-trivial change here, when messages/topics/parsing misbehave, before touching tmux or hooks, or when the user says "check the library". Loads at most 1–2 skills.
---

# ccbot-library (dispatcher)

Deep runbooks in `.claude/skill-library/` — outside the auto-load path. All 16 reviewed 2026-07-04, findings applied (incl. the /mute-is-global and zshrc-watcher corrections).

## Procedure

1. Read `.claude/skill-library/INDEX.md` (or route from below).
2. `Read .claude/skill-library/<name>/SKILL.md` — at most 1–2. Never the whole library.
3. Apply; cite; fix stale facts in place (Provenance sections carry re-verify commands).

## Quick routes

Invariants/fork modules → `ccbot-architecture-contract` (read `.claude/rules/*.md` first — base contract) · upstream/rebase/README-staleness → `ccbot-fork-vs-upstream` · duplicated/missing/echoed messages → `ccbot-message-flow-debugging` · TUI detection broke after a Claude Code update → `ccbot-terminal-parsing` · windows/topics orphan or die wrong → `ccbot-tmux-lifecycle` · MarkdownV2/topics/rate limits → `ccbot-telegram-craft` · /speak or voice-in → `ccbot-voice-stack` · /sleep and wip/overnight refs → `ccbot-overnight-autonomy` · hooks/pager/zshrc (5 mute gates!) → `ccbot-integration-surface` · any env var/tunable → `ccbot-config-and-flags` · setup/imports/.env precedence → `ccbot-build-and-env` · bot down/deploy → `ccbot-run-and-operate` · tests/drills → `ccbot-testing-and-qa` · commit gates → `ccbot-change-control` · settled battles (segfault, HTML revert…) → `ccbot-failure-archaeology` · scrape-vs-SDK question → `ccbot-websocket-frontier`.
