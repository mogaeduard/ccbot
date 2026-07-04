---
name: ccbot-fork-vs-upstream
description: The git relationship of this ccbot checkout — fork of six-ddc/ccbot (remote origin = upstream, remote fork = mogaeduard/ccbot, working branch auto-topic-mirror, 19 fork-only commits as of 2026-07-04), what the fork owns vs upstream, the upstream branch graveyard, the README staleness map (EN/CN/RU document a reverted formatter era and omit all fork features), and how to evaluate pulling upstream changes. Load this when: considering a rebase/merge from upstream; deciding whether to PR something upstream; confused by README vs code disagreement; touching git remotes/branches here; or attributing a behavior to fork vs upstream code. Keywords: fork, upstream, six-ddc, rebase, PR, origin, auto-topic-mirror, README stale, ccmux, divergence. Does NOT cover: the fork modules' content (ccbot-architecture-contract), commit gates (ccbot-change-control), or historical reverts' technical content (ccbot-failure-archaeology).
---

# ccbot-fork-vs-upstream

**Use when** navigating the fork/upstream boundary. **Do NOT use when** you need module internals (→ ccbot-architecture-contract).

## The topology (as of 2026-07-04)

- Remotes: `origin` = six-ddc/ccbot (UPSTREAM — note the inverted naming!), `fork` = mogaeduard/ccbot. Working branch: `auto-topic-mirror` (= fork/auto-topic-mirror), HEAD `aa64a6d`, **19 commits ahead of origin/main** (`7c2e15c`, 2026-06-03 = merge-base). Local `main` == origin/main (clean upstream mirror — keep it that way for diffing).
- Deployment installs FROM THE FORK BRANCH: `uv tool install git+https://github.com/mogaeduard/ccbot@auto-topic-mirror` (→ ccbot-run-and-operate).
- Upstream calls the project "ccmux" in its README install URLs — same project, renamed upstream.

## What the fork owns (the 19 commits, thematically)

Mirror world (`4e01299`→`fc9e2f2`: auto-topic mirror, all-windows Termius design, reconciler, shell echo, delete-not-close), message-flow fixes (`74da8c7`, echo/thinking/final-answer), voice (`e12a16f` confirm-first, `20d810c`/`c808b5c` /speak+summarize), safety batch (`7712594`: screenshot fallback, bracketed-paste multiline, echo one-shot, /lock, voice TTL), Batch B/C (`cfcf33f` titles+dashboard+/new; `097fb9b` owner label+/sleep/wake+/killall+/grab), overnight autonomy (`104533c`), command menu (`ffa0e8a`), and the Jul-4 overnight batch (`3c94bd6` phantom-__main__, `752bf1f`+`aa64a6d` deletion probes+/mute, `d6f6a34` /term). Fork-only modules: mirror, overnight, dashboard, topic_titles, screenshot, dialog_fallback.

## Evaluating an upstream pull (the procedure)

1. `git log origin/main..main` — wait, local main IS origin/main; fetch first: `git fetch origin && git log main..origin/main --oneline` to see what upstream added.
2. High-collision zones: bot.py (+1,100 fork lines), terminal_parser.py, message_queue.py. Low-collision: fork-only modules.
3. Upstream already moved once while forking (Codex remote-agent support landed upstream) — expect drift; rebase in a WORKTREE, run the full gate (515 tests — → ccbot-testing-and-qa), live phone drill before adopting.
4. OPEN QUESTION (owner): PR the mirror upstream (it's config-gated OFF, upstream-friendly by design) or let the fork drift permanently? No rebase policy is written anywhere — until decided, don't rebase casually.

## Staleness map (don't trust these against code)

| Doc | Status |
|---|---|
| README.md / README_CN / README_RU | UPSTREAM-era: document the chatgpt-md-converter/HTML formatter (fully REVERTED — code is telegramify/MarkdownV2), list a nonexistent html_converter.py, omit all fork modules and ~12 fork commands, install URLs point at six-ddc/ccmux |
| CLAUDE.md + .claude/rules/*.md | ACCURATE for the core; silent on fork modules (that gap is ccbot-architecture-contract's job) |
| Upstream branch graveyard | copilot/* (3), add-claude-github-actions-* (2), codex/codex-remote-agents — dead, ignore |

## Provenance and maintenance
- Written 2026-07-04 from: git remote -v, git log origin/main..HEAD (19), merge-base 7c2e15c, README greps, module listing.
- Re-verify: `git remote -v` · `git fetch origin --dry-run` then `git log main..origin/main --oneline | head` · `git log origin/main..HEAD --oneline | wc -l`.
