---
name: ccbot-config-and-flags
description: The complete configuration axis catalog of ccbot — every env var with default and production value (token/users, tmux session, claude command, poll intervals, mirror chat id master switch, owner name, visibility toggles, OpenAI key/base-url for voice-in), the hardcoded tunables (TTS URL :8838 + 800-char cap, overnight 600s poll + 10s git timeout, dashboard 5s min-interval, topic 128-char limit, echo-dedup window, merge budget 3800, split 4096), the runtime toggles (/lock persisted state, overnight armed flag, quiet-until file, sleep-mode.flag), and where each lives. Load this when: adding/reading any config; a feature seems off/disabled (check its gate!); tuning an interval or limit; production .env questions; or wiring a new toggle. Keywords: env vars, CCBOT_MIRROR_CHAT_ID, CLAUDE_COMMAND, MONITOR_POLL_INTERVAL, OPENAI_BASE_URL, TTS_SPEAK_URL, quiet-until, lock, tunables, defaults. Does NOT cover: env loading order/scrubbing (ccbot-build-and-env), what the features DO (their owning skills), or the external hook/zshrc statics (ccbot-integration-surface).
---

# ccbot-config-and-flags

**Use when** touching configuration or hunting a disabled-feature gate. **Do NOT use when** the question is load order (→ ccbot-build-and-env).

## Env vars (config.py, line-verified 2026-07-04; production values noted where known on this host)

| Var (line) | Default | Notes / prod |
|---|---|---|
| TELEGRAM_BOT_TOKEN (:43) | required, raises | ~/.ccbot/.env only, chmod 600 |
| ALLOWED_USERS (:47) | required, raises | comma-sep ints; locked to the owner's id |
| TMUX_SESSION_NAME (:61) | `ccbot` | |
| CLAUDE_COMMAND (:65) | `claude` | prod runs `claude --dangerously-skip-permissions` (SETUP-HOST) |
| CCBOT_CLAUDE_PROJECTS_PATH / CLAUDE_CONFIG_DIR (:75–76) | unset → ~/.claude/projects | resolution priority: custom > CLAUDE_CONFIG_DIR/projects > default |
| MONITOR_POLL_INTERVAL (:85) | 2.0s | |
| CCBOT_SHOW_USER_MESSAGES (:90) | true | |
| CCBOT_OWNER_NAME (:95) | `Eduard` | fork-only: the bold name label |
| CCBOT_SHOW_TOOL_CALLS (:100) | true | prod true = full-verbosity mirror |
| CCBOT_SHOW_HIDDEN_DIRS (:105) | false | directory browser |
| OPENAI_API_KEY / OPENAI_BASE_URL (:109–110) | key empty; base-url has a default | voice-in transcription; prod sets BOTH (base-url points at the local MLX Whisper :8837 world — verify the live value, → ccbot-voice-stack) |
| CCBOT_MIRROR_CHAT_ID (:118) | unset = **mirror AND dashboard disabled entirely** | THE master switch; prod set |

## Hardcoded tunables (edit-in-file; each name = its grep handle)

| Constant | Value | Where |
|---|---|---|
| TTS_SPEAK_URL / TTS_MAX_CHARS | `http://127.0.0.1:8838/speak` / 800 | bot.py:707–708 (/speak; >300-char answers get summarized first — → ccbot-voice-stack) |
| OVERNIGHT_POLL_INTERVAL_S / GIT_TIMEOUT_S | 600 / 10.0 | overnight.py:40–41 |
| DASHBOARD_MIN_INTERVAL_SECONDS | 5.0 | dashboard.py:45 |
| TOPIC_NAME_LIMIT | 128 | topic_titles.py:31 (Telegram's own cap) |
| merge budget / split | 3800 / 4096 | message_queue/sender (upstream constants — see .claude/rules/message-handling.md) |
| echo-dedup window | 90s one-shot deque | mirror/queue layer (→ ccbot-message-flow-debugging) |
| voice pending TTL | 5 min | voice confirm-first flow |

## Runtime toggles (state, not env)

| Toggle | Mechanism |
|---|---|
| /lock /unlock | `is_locked` persisted in state.json — the kill switch; bot ignores everything while locked |
| overnight armed | state flag via /sleep (→ ccbot-overnight-autonomy) |
| quiet hours | `~/.ccbot/quiet-until` (epoch, written by /sleep, read by overnight.py:211 AND the external notify-pager — self-expiring per overnight.py:11) |
| external sleep mode | `~/.claude/sleep-mode.flag` also mutes the pager (owned by the watchdog world — → ccbot-integration-surface) |
| /mute /unmute | GLOBAL notification-channel mute, NOT per-topic: `/mute [mac|phone]` touches `~/.ccbot/mute-mac` / `~/.ccbot/mute-phone` flag files (no arg = both), consumed by notify-pager.sh (bot.py:573–594, fork commit aa64a6d). There is no per-thread mute state anywhere |

Adding config: prefer an env var with a safe default in config.py (document here) over a new hardcoded constant; gate new surface OFF by default (architecture rule 7).

## Provenance and maintenance
- Written 2026-07-04 from config.py (every line cited read), bot.py:707–708, overnight.py:6–41,:211, dashboard.py:45, topic_titles.py:31, SETUP-HOST/BACKLOG (this host).
- Re-verify: `grep -n 'os.getenv' src/ccbot/config.py` · `grep -n 'TTS_SPEAK_URL' src/ccbot/bot.py` · `cat ~/.ccbot/quiet-until 2>/dev/null`.
