---
name: ccbot-integration-surface
description: Everything OUTSIDE the ccbot repo that the bot depends on (this host) — the ~/.claude/settings.json hooks (SessionStart → `ccbot hook` maintaining session_map.json; Notification → notify-pager.sh waiting with Submarine sound; Stop → notify-pager.sh done with Glass), the notify-pager's quiet logic (respects ~/.ccbot/quiet-until AND ~/.claude/sleep-mode.flag, DMs the owner), the zshrc tab/window functions, ~/.tmux.conf, and the docs of record living in ~/.ccbot (BACKLOG.md, SETUP-HOST.md). Load this when: hooks stop firing after a settings.json edit; pages/sounds misbehave or fire during quiet hours; session_map.json goes stale; moving the bot to a new machine (the full external checklist); or auditing what a repo clone does NOT carry. Keywords: hooks, settings.json, SessionStart, notify-pager, Submarine, Glass, DM, quiet, session_map, zshrc, BACKLOG, SETUP-HOST, external, integration. Does NOT cover: the zshrc lifecycle design itself (ccbot-tmux-lifecycle), quiet-until's writer (ccbot-overnight-autonomy), or in-repo config (ccbot-config-and-flags).
---

# ccbot-integration-surface

**Use when** the out-of-repo wiring misbehaves or you're relocating the bot. All facts here are **this host** — a fresh clone carries NONE of them; SETUP-HOST.md is the portable recipe.

## The ~/.claude/settings.json hooks (verified 2026-07-04)

| Hook | Command | Does |
|---|---|---|
| SessionStart | `/Users/mogaeduard/.local/bin/ccbot hook` | maintains `~/.ccbot/session_map.json` (window↔Claude-session). NOTE: points at the INSTALLED tool — a repo-only change to hook.py does nothing until reinstall (→ ccbot-run-and-operate deploy loop). `ccbot hook --install` re-wires this block (hook.py:117) |
| Notification | `bash ~/.claude/notify-pager.sh waiting` | needs-input pager: Submarine.aiff + ⏳ + Telegram DM |
| Stop | `bash ~/.claude/notify-pager.sh done` | Glass.aiff + ✅ (quiet channel) |

hook.py must never import config.py (→ ccbot-build-and-env law 2) — that's what keeps the SessionStart hook safe in env-less panes.

## notify-pager.sh (~/.claude/, this host)

- Sounds: Submarine (waiting) / Glass (done); DM goes to the owner's hardcoded chat id (in-script; the loud channel).
- **Quiet logic — FIVE independent, additive mute gates** (a page or its absence is explained by any of them; check ALL when confused): (1) `~/.ccbot/quiet-until` (epoch; written by /sleep, deleted by /wake, self-expiring); (2) `~/.claude/sleep-mode.flag` (the overnight-watchdog world's toggle); (3) **a static nightly fallback window 23:30–08:00** (`QUIET_FROM=2330 QUIET_TO=0800`, notify-pager.sh:26–33) that applies whenever quiet-until does NOT exist — i.e. every night by default even without /sleep; (4) `~/.ccbot/mute-mac` and (5) `~/.ccbot/mute-phone` (written by the bot's global /mute command, checked at notify-pager.sh:41–44 — silence the sound and the DM independently).
- The pager is the "needs input" vs "done" split — keep the loud/quiet channel distinction when editing (→ `~/.claude/playbooks/ops-and-automation.md` §5).

## The rest of the surface

| Piece | Where | Role |
|---|---|---|
| zshrc functions (cc/cc-new/cc-resume/closetab/tabs, digit slots) | ~/.zshrc | create the windows the bot mirrors (design → ccbot-tmux-lifecycle) |
| ~/.tmux.conf | minimal by design | no kill hooks (fenced) |
| BACKLOG.md | ~/.ccbot/ | THE live product log: decisions, batches, pending live verifications |
| SETUP-HOST.md | ~/.ccbot/ | parameterized per-host bring-up incl. the future Ubuntu systemd unit — the ONLY portable copy of this whole page's knowledge; consider committing it into the repo (flagged in the ops playbook) |
| Sibling LaunchAgents | com.eduard.ccbot + claude-tts (:8838) + whisper-stt (:8837) | → ccbot-run-and-operate |
| Telegram-side manual config | bot admin rights (Manage Topics), forum/Threaded mode on the group, the DM chat id | OPEN QUESTION (owner): no single source of truth doc exists — SETUP-HOST is the closest; record the exact settings there when next touched |

## New-machine checklist (the short form; SETUP-HOST.md is authoritative)

- [ ] uv tool install from the fork branch; write ~/.ccbot/.env (token, users, mirror chat id)
- [ ] LaunchAgent/systemd unit with WorkingDirectory set
- [ ] `ccbot hook --install` (wires SessionStart)
- [ ] notify-pager.sh + its two hook entries (or skip paging on a server host)
- [ ] zshrc functions if humans will open tabs there; skip for headless
- [ ] ONE bot per machine per token (getUpdates conflict otherwise)

## Provenance and maintenance
- Written 2026-07-04 from: ~/.claude/settings.json (:9/:20/:32), ~/.claude/notify-pager.sh (:13/:15/:22–:25), hook.py (:65/:114), ~/.ccbot contents, ~/.zshrc — all this-host.
- Re-verify: `grep -n 'ccbot hook\|notify-pager' ~/.claude/settings.json` · `ls ~/.ccbot/{BACKLOG.md,SETUP-HOST.md}`.
