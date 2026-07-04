---
name: ccbot-failure-archaeology
description: The ccbot battle chronicle — rejected approaches nobody should retry and hard-won fixes, as symptom → root cause → evidence → status: the tmux client-detached segfault saga, the HTML-formatter full revert, the window_name→window_id re-keying, the parse-layer ANSI first-attempt revert, the LaunchAgent cwd=/ bug, the token-leak-to-subprocess fix, the XTTS/VoxCPM2 voice rejections, the TYPING-probe-validated-nothing deletion saga, the phantom __main__ topic, and the silent overnight Pro-limit death that created the redispatch policy. Load this when: a symptom feels familiar; before retrying ANYTHING in this list; when a fix keeps not working; or to understand why an invariant exists. Keywords: history, revert, rejected, segfault, HTML formatter, window_name, ANSI, cwd, token leak, XTTS, TYPING probe, phantom topic, silent failure, settled. Does NOT cover: fix procedures (each entry names its owning skill); cross-project lesson classes (~/.claude/playbooks/failure-archaeology.md).
---

# ccbot-failure-archaeology

**Use when** a battle looks familiar. Statuses: SETTLED / FENCED (never retry) / ACCEPTED.

### 1. tmux client-detached kill hooks — FENCED
Every ordering/nesting/run-shell variant on this tmux build: wrong-session kills, server hangs, one SEGFAULT (crash report on disk); a HUP-trap variant never fired. Winner: destroy-unattached + explicit closetab + app-layer reaping. **Never retry hook variants.** Owner: ccbot-tmux-lifecycle (the fence section).

### 2. The formatter war — HTML era fully reverted (upstream)
MarkdownV2/telegramify → chatgpt-md-converter HTML (1d37a2d, ef79072) → FULL REVERT to telegramify (20e3794 + 9d4627f). The README trio still documents the dead era. Status: SETTLED — MarkdownV2-only via safe_* is law (ccbot-telegram-craft). Don't "modernize" to HTML again.

### 3. window_name → window_id re-keying (upstream, 7786646/8395ef7)
Names drift and collide; ids don't (within a server lifetime). Status: SETTLED as architecture invariant 1. Any new lookup keyed by name is a regression.

### 4. ANSI strip: wrong layer first (upstream)
First attempt (9587189) REVERTED (101824e), redone correctly at the parse layer (70183a0). Status: SETTLED — strip at parse, nowhere else (ccbot-terminal-parsing).

### 5. Bot token leaked into subprocess env (upstream, 52c09d8/17745f4)
Children inherited the token. Fix: SENSITIVE_ENV_VARS scrub after config load. Status: SETTLED — the scrub is a security boundary (ccbot-build-and-env law 3); never pass secrets by env inheritance.

### 6. LaunchAgent cwd=/ since day one
The daemon ran with working directory `/` for days — the directory browser silently browsed filesystem root. Fix: WorkingDirectory in the plist; and cwd determines which .env can shadow (build-and-env). Status: SETTLED; the lesson (always set WorkingDirectory) generalized into the ops playbook.

### 7. Voice engine rejections — XTTS, VoxCPM2
XTTS: Spanish-accented Romanian, deleted (1.7GB back). VoxCPM2: no Romanian. OmniVoice won; the reference-clip swap (natural speech beats scripted, 0.881→0.912 similarity) is part of the verdict. Status: SETTLED bake-off — re-audition only with new evidence (ccbot-voice-stack graveyard).

### 8. Topic-deletion detection: the probe that validated nothing (Jul 4)
Idle topic deletion from the phone went unnoticed → active TYPING probe added (`752bf1f`) → **the probe validated nothing** (sending typing to a deleted thread didn't error usefully) → replaced by an editForumTopic probe whose error IS the signal (`aa64a6d`, + /mute /unmute landed alongside). Status: SETTLED at the second design. Lesson: a probe must be validated to FAIL on the condition it detects — a probe that can't fail detects nothing (see the vacuous-test rule in the playbooks).

### 9. Phantom __main__ topic stealing slot 1 (Jul 4)
The zshrc placeholder window leaked into the mirror as a topic AND displaced terminal 1's numbering. Fix: `3c94bd6` excludes the placeholder. Status: SETTLED; recurrence = regression there (ccbot-tmux-lifecycle triage row).

### 10. Echo fix that over-consumed (Jul 3)
`74da8c7` deduped injected-message echo; `7712594` had to partially reverse it (one-shot consumption) because the dedup ate LEGITIMATE repeats. Status: SETTLED at one-shot + 90s window, ceilings documented (ccbot-message-flow-debugging). Both directions have been tried — a third flip needs a reproduced mechanism, not vibes.

### 11. Silent overnight Pro-limit death (Jul 4, session-level)
An overnight agent batch died on a usage limit without surfacing; the night produced nothing; discovered by redispatch on Max. Status: SETTLED as policy (limit errors are terminal alarms; redispatch-on-stronger-tier; heartbeats) — lives in `~/.claude/playbooks/ops-and-automation.md` §6 and this repo's overnight design (morning report = the heartbeat).

## Provenance and maintenance
- Written 2026-07-04 (HEAD aa64a6d) from: fork+upstream git log (all shas cited verifiable via `git show -s`), the crash-report precedent, zshrc design comments, BACKLOG/memory records.
- Re-verify any sha: `git show -s --format='%h %ad %s' <sha>` (upstream shas need `git fetch origin` first if pruned).
