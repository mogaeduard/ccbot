---
name: ccbot-run-and-operate
description: Operating ccbot as the live daily-driver service — the LaunchAgent lifecycle (kickstart restart, logs in ~/.ccbot/logs), the deploy loop (edit → uv tool install --force from the fork branch → kickstart), state files in ~/.ccbot (state.json, session_map.json, .env), dev-mode vs production worlds (scripts/restart.sh expects a tmux __main__ window that production doesn't use), the teardown/fresh-start procedure, and the two sibling voice LaunchAgents (claude-tts :8838, whisper-stt :8837). Load this when: the bot is down/unresponsive; deploying a change to production; logs are needed; state looks corrupted; performing a world teardown/rebuild; or setting up a new host. Keywords: LaunchAgent, kickstart, restart, deploy, logs, state.json, session_map, ~/.ccbot, teardown, dev mode, restart.sh, 8838, 8837. Does NOT cover: install/env mechanics (ccbot-build-and-env), config meanings (ccbot-config-and-flags), or the tmux window world itself (ccbot-tmux-lifecycle).
---

# ccbot-run-and-operate

**Use when** operating the live service. **Do NOT use when** setting up from scratch (→ ccbot-build-and-env).

## The two worlds — know which one you're in

| World | Runs | Restart |
|---|---|---|
| **PRODUCTION (this host)** | the INSTALLED tool (`~/.local/bin/ccbot`) under LaunchAgent `com.eduard.ccbot` (KeepAlive, RunAtLoad, WorkingDirectory=/Users/mogaeduard) | `launchctl kickstart -k gui/$(id -u)/com.eduard.ccbot` |
| Dev | `uv run ccbot` in the tmux window `ccbot:__main__` | `scripts/restart.sh` — fully functional on this host: zshrc's `_cc_ensure_base()` creates the base session's placeholder window ATOMICALLY named `__main__` (zshrc:56–58, deliberately atomic so a concurrent mirror tick never sees an unnamed window), and restart.sh targets exactly that window, injecting `uv run ccbot` if not running |

**Deploy loop (production):** edit → gates green (→ ccbot-change-control) → `uv tool install --force git+https://github.com/mogaeduard/ccbot@auto-topic-mirror` → kickstart → live phone drill. A repo edit without the install+kickstart does NOTHING in production.

## Where everything lives (~/.ccbot, the runtime dir — this host)

| File | Holds |
|---|---|
| `.env` (600) | token, users, mirror chat id, OpenAI key/base-url, etc. (→ ccbot-config-and-flags) |
| `state.json` | thread bindings, /lock state, overnight armed, dashboard message id |
| `session_map.json` | window↔Claude-session mapping (written by the SessionStart hook) |
| `quiet-until` | epoch for quiet hours |
| `logs/` | service stdout/err — FIRST stop when the bot misbehaves |
| `BACKLOG.md`, `SETUP-HOST.md` | product log + per-host bring-up (docs of record, runtime dir not repo) |

## Triage when the bot is down/unresponsive

1. `launchctl list | grep ccbot` — running? exit code?
2. `tail -50 ~/.ccbot/logs/*.log` — crash reason (config raise? Telegram conflict?).
3. **"Conflict: terminated by other getUpdates request"** = TWO consumers on one token (a dev `uv run ccbot` fighting the LaunchAgent) — one bot per token, kill the extra.
4. Locked? `/unlock` from Telegram, or check `is_locked` in state.json.
5. Windows exist but topics dead (or vice versa) → the reconciler heals within ~2s ticks; if not, → ccbot-message-flow-debugging.

## Teardown / fresh world (the executed precedent, 2026-07-04)

Close all terminals → the reconciler deletes dead topics → fresh windows get fresh numbered topics from Telegram. State that persists a teardown: state.json bindings get reconciled away; session_map entries for dead sessions are reaped. For a FULL reset: stop the agent, move state.json aside, kickstart — topics rebuild from live tmux truth (the mirror treats tmux as the source of truth).

## The sibling services (voice — this host)

`/speak` depends on **claude-tts** (:8838, OmniVoice cloned voice) and voice-in on **whisper-stt** (:8837, local MLX Whisper) — separate LaunchAgents NOT managed by this repo. If /speak fails, probe :8838 first (`curl -s localhost:8838 -m 2`; → ccbot-voice-stack).

## Provenance and maintenance
- Written 2026-07-04 from: the LaunchAgent plist (this host), SETUP-HOST.md conventions, state files in ~/.ccbot, scripts/restart.sh, the executed teardown precedent (2026-07-04), bot.py TTS constants.
- Re-verify: `launchctl list | grep ccbot` · `ls ~/.ccbot` · `plutil -p ~/Library/LaunchAgents/com.eduard.ccbot.plist`.
