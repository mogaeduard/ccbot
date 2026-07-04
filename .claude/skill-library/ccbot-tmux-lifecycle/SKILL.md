---
name: ccbot-tmux-lifecycle
description: The three-layer tmux lifecycle that keeps ccbot's world consistent — the zshrc grouped-session model (per-view sessions grouped on base `ccbot`, each with destroy-unattached on; cc/cc-new/cc-resume/closetab/tabs functions), the tmux.conf constraints, ccbot's app-layer reaping (mirror reconciler) and window_id re-resolution, and the FENCED-OFF wrong path: tmux client-detached kill hooks (wrong-session kills, hangs, one SEGFAULT on this build — never retry). Load this when: windows/topics orphan or vanish wrongly; a closed terminal tab leaves a zombie window; sessions multiply; touching ~/.zshrc tab functions, ~/.tmux.conf, or tmux_manager.py; window ids change after a tmux server restart; or designing ANY window-kill behavior. Keywords: tmux, grouped session, destroy-unattached, client-detached, segfault, closetab, tabs, zshrc, window_id, re-resolution, orphan window, reaping. Does NOT cover: message flow through windows (ccbot-message-flow-debugging), the drill methodology (ccbot-testing-and-qa), or the zshrc/hook integration inventory (ccbot-integration-surface).
---

# ccbot-tmux-lifecycle

**Use when** window/session lifecycle misbehaves or you're changing it. **Do NOT use when** the binding is fine but messages misflow (→ ccbot-message-flow-debugging).

## The three layers (who owns what)

1. **zshrc (this host)** — every terminal tab attaches a per-view session GROUPED on base `ccbot` (`new-session -t ccbot`), selects its window, and sets `destroy-unattached on` for THAT view session (zshrc:97/:114/:153/:161). The base `ccbot` session NEVER gets destroy-unattached (:32) — it's the anchor holding all windows. Functions: `cc` (slot attach), `cc-new`/`cc-resume` (new window running claude), `closetab [N]` (explicit kill: window + view), `tabs` (numbered listing). SSH sessions deliberately skip the cleanup trap (:30).
2. **tmux.conf** — minimal; the lifecycle intelligence deliberately does NOT live in tmux hooks (see the fence below).
3. **ccbot app layer** — the mirror reconciler (~2s tick) reaps dead windows/topics in all four directions; `tmux_manager.py` routes strictly by `window_id` (`find_window_by_id` :194) and re-resolves ids on server restart (ids are stable within one tmux server lifetime, NOT across restarts).

Death chain for a closed tab (THREE actors, in order): client dies → the view session's `destroy-unattached` reaps the VIEW → **zshrc's own detached watcher kills the WINDOW**: `_cc_tab()` (zshrc:82–100) double-forks a watcher polling for the view's disappearance, then `_cc_reap` (zshrc:60–67) issues an explicit `kill-window` (guarded by checking no other client views it) — this fires independently of, and usually before, ccbot → the app layer (mirror reconciler) is the LAST-resort reaper for whatever the watcher missed. So the first suspect for a zombie/killed window after closing a Mac tab is the **zshrc watcher layer**, not the mirror.
⚠ Know the zshrc's internal doc-drift: its top comment (:23–26) still describes a "client-side HUP trap" kill mechanism, while :77–78 says the opposite ("a HUP trap never fired — verified live") and the actual code is the double-forked watcher. Trust :77–78 + the code; the top comment is stale.

## ⚠ THE FENCE: never use tmux client-detached kill hooks

On this tmux build, every ordering/nesting/run-shell variant of a `client-detached` kill hook produced **wrong-session kills, server hangs, and one SEGFAULT** (crash report on disk; zshrc:23 documents "that path is unreliable on this tmux build"). A HUP trap variant also never fired (:77). The winning design is the current one: `destroy-unattached` + explicit `closetab` + app-layer reaping. **Do not retry hook variants** — this is a settled battle (→ ccbot-failure-archaeology). Any new kill behavior goes in the APP layer where it's observable and testable, drilled on a throwaway socket first (→ ccbot-testing-and-qa).

## Triage

| Symptom | Check |
|---|---|
| Zombie window after closing a tab | FIRST: did the zshrc watcher fire? (`_cc_tab`'s detached poller → `_cc_reap` kill-window — it's the primary reaper; another client viewing the window blocks it BY DESIGN). Only then: reconciler logs, mirror enabled (CCBOT_MIRROR_CHAT_ID)? |
| Topic bound to the WRONG window | window ids reset after a tmux server restart → the bot re-resolves on startup; a stale state.json binding from before the restart is the suspect — compare `tmux list-windows -a -F '#{window_id} #{window_name}'` against state.json |
| Sessions multiplying | views without destroy-unattached — check the creating function set the option (`tmux show-options -t <view> destroy-unattached`) |
| Whole-world weirdness after sleep/crash | one reconciler tick usually heals; else → ccbot-run-and-operate teardown ladder |
| `__main__` placeholder issues | the phantom-topic/slot-theft class was fixed in `3c94bd6` (placeholder excluded from mirroring); recurrence = regression there |

## Provenance and maintenance
- Written 2026-07-04 from: ~/.zshrc (:19–:32 design comment, :77, :97/:114/:153/:161, closetab :129–:137) — this host; tmux_manager.py (:36, :194–:205); commits fc9e2f2/c24f6a5/3c94bd6; the segfault crash report precedent.
- Re-verify: `grep -n 'destroy-unattached' ~/.zshrc` · `tmux list-sessions | head` · `grep -n 'find_window_by_id' src/ccbot/tmux_manager.py`.
