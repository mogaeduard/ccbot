---
name: ccbot-overnight-autonomy
description: ccbot's overnight-autonomy subsystem — /sleep and /wake semantics (quiet-until file, self-expiring), the armed checkpoint loop (every 600s, for each live tmux window's dirty git repo: temp-index snapshot via GIT_INDEX_FILE → commit-tree → a wip/overnight-<date>-<branch> ref, NEVER touching the working tree, index, or HEAD), the fixed committer identity, the morning report, promote/discard discipline for the wip refs, and the safety envelope (10s git timeout, what repos should be excluded — an OPEN question). Load this when: arming or debugging overnight mode; wip/overnight-* refs appear in a repo and you must decide their fate; the checkpoint loop errors on a repo; changing the poll/timeout; or designing the exclusion policy. Keywords: overnight, /sleep, /wake, quiet-until, checkpoint, wip/overnight, commit-tree, GIT_INDEX_FILE, snapshot, morning report, armed, dirty repo. Does NOT cover: the quiet-hours notification muting (ccbot-integration-surface owns the pager), general watchdog design (~/.claude/playbooks/ops-and-automation.md), or /lock (ccbot-config-and-flags).
---

# ccbot-overnight-autonomy

**Use when** working the overnight subsystem or judging wip/overnight refs. **Do NOT use when** you need the pager/notification layer (→ ccbot-integration-surface).

## Semantics (overnight.py, all line-verified)

- **/sleep** writes `~/.ccbot/quiet-until` (epoch) and arms the loop; **/wake** disarms; quiet-until is **self-expiring** — the loop checks "in case /wake was never sent" (:11). The same file mutes the external notify-pager (shared toggle — → ccbot-integration-surface).
- While armed, every `OVERNIGHT_POLL_INTERVAL_S = 600` (:40): for each live tmux window's git repo that is DIRTY, `snapshot_repo` checkpoints it (:122).
- **The snapshot mechanism (the part that must never regress):** temp-index add → commit-tree → ref update, with `GIT_INDEX_FILE` pointed at a tmpdir index (:138) so the REAL index, working tree, and HEAD are never touched (:16 docstring: "temp-index add -> commit-tree -> ..."). Ref pattern: `refs/heads/wip/overnight-<YYYYMMDD>-<branch>` (:193). A fixed committer identity is passed explicitly because "commit-tree requires a committer identity; don't depend on the machine's" (:43).
- Every git call bounded by `GIT_TIMEOUT_S = 10.0` (:41) — a hung repo (lock contention, huge repo) skips rather than stalls the loop. Morning: the report summarizes what got checkpointed.

## Promote / discard discipline for wip/overnight-* refs

These refs are CRASH INSURANCE, not branches to build on:
1. Morning after: `git log --oneline wip/overnight-<date>-<branch>` and diff against the live branch.
2. Work survived normally? → `git branch -D wip/overnight-...` (discard — the live tree already has it).
3. Work was LOST (crash/limit death)? → cherry-pick or `git restore --source=wip/... -- <paths>` selectively; then delete the ref.
4. Never merge a wip ref wholesale — it's an unreviewed dirty-tree snapshot (the UNREVIEWED doctrine applies).
5. Refs older than a few days with nothing to recover = delete; they're noise in `git branch` otherwise.

## Safety envelope (honest state, 2026-07-04)

- **No per-repo exclusion mechanism exists, BY OWNER DECISION (2026-07-05):** ALL dirty repos behind live windows get checkpointed; no repo is excluded unless the owner names one explicitly. Do not build or propose an exclusion list unprompted.
- Untested-in-anger cases: locked index (skips via timeout), submodules, repos mid-rebase. If the loop errors on a specific repo, capture the log line — that's the next fence.
- The live-test protocol: armed overnight, verified next morning against real repos (the Batch-drill convention — → ccbot-testing-and-qa phone/live discipline).

## Provenance and maintenance
- Written 2026-07-04 from overnight.py (:6, :8, :11, :16, :40–:41, :43, :122, :138, :193) and its tests; commit 104533c; the shared quiet-until reader at :211.
- Re-verify: `grep -n 'wip/overnight\|GIT_INDEX_FILE\|OVERNIGHT_POLL' src/ccbot/overnight.py` · `git branch --list 'wip/overnight-*'` in any repo behind a live window.
