---
name: ccbot-build-and-env
description: Recreating the ccbot dev environment and understanding its env mechanics — uv sync --all-extras (Python >=3.12, NO committed lockfile), the config-singleton import trap (raises at import without TELEGRAM_BOT_TOKEN/ALLOWED_USERS — tests force-set env first), the hook-must-never-import-config rule, .env precedence (cwd .env beats ~/.ccbot/.env, first-loaded wins), sensitive-env scrubbing, and the production install path (uv tool install from the fork branch + LaunchAgent). Load this when: setting up on a new machine; ImportError/ValueError at ccbot import; tests fail at collection with missing env; deps break after a sync (no lockfile!); editing hook.py or config.py load order; or deploying a new build. Keywords: uv sync, all-extras, no lockfile, config singleton, import raises, hook.py, .env precedence, CCBOT_DIR, scrub, uv tool install. Does NOT cover: the env VARS catalog and their meanings (ccbot-config-and-flags), service lifecycle (ccbot-run-and-operate), or commit gates (ccbot-change-control).
---

# ccbot-build-and-env

**Use when** environments or imports misbehave, or installing. **Do NOT use when** you need a specific var's meaning (→ ccbot-config-and-flags).

## Dev setup

```bash
git clone git@github.com:mogaeduard/ccbot.git && cd ccbot   # or work in this checkout
uv sync --all-extras          # dev extras: pyright, pytest, pytest-asyncio, pytest-cov, ruff
uv run pytest --tb=short -q   # green (skips nothing on a machine with tmux)
```

Python >=3.12 (pyproject:6; CI matrix 3.12+3.13). Prereqs on PATH: `tmux`, `claude`. **TRAP: uv.lock is GITIGNORED** (upstream decision) — deps resolve fresh from pyproject ranges (`python-telegram-bot[rate-limiter]>=21.0`, `telegramify-markdown>=0.5.0,<1.0.0`, httpx, libtmux, Pillow, aiofiles, python-dotenv). A breakage after sync with no code change = an upstream release moved; pin it in pyproject with a dated comment.

## The three import-order laws

1. **config.py is a module-level singleton that RAISES at import** if `TELEGRAM_BOT_TOKEN` (:45) or `ALLOWED_USERS` (:49) are unset. Tests survive because `tests/conftest.py` force-sets env (token, users, tempdir CCBOT_DIR) BEFORE any ccbot import — new test files inherit this; never import ccbot modules at test-collection time outside that ordering.
2. **hook.py must NEVER import config.py** — its own docstring is the law: hooks run inside tmux panes where bot env vars are not set; config dir resolution goes through `utils.ccbot_dir()` (shared, import-safe). Adding an import to hook.py = breaking every Claude Code session start on the machine, silently.
3. **Secret scrubbing:** after load, config.py deletes `SENSITIVE_ENV_VARS` ({TELEGRAM_BOT_TOKEN, ALLOWED_USERS, OPENAI_API_KEY}) from os.environ so spawned children (claude CLIs, shells) never inherit them. Don't "fix" a child needing the token by removing the scrub — pass explicitly what's needed.

## .env precedence (config.py:5, :33–:40)

Local **cwd `.env` > `$CCBOT_DIR/.env`** (default ~/.ccbot), and `load_dotenv(override=False)` means **first-loaded wins** — a stray .env in whatever directory the process starts from silently beats the real one. The LaunchAgent's WorkingDirectory (/Users/mogaeduard) is therefore load-bearing: it determines which .env can shadow. When env values look wrong, `echo $PWD` of the running process is the first check.

## Production install path (this host)

```bash
uv tool install --force git+https://github.com/mogaeduard/ccbot@auto-topic-mirror   # → ~/.local/bin/ccbot
launchctl kickstart -k gui/$(id -u)/com.eduard.ccbot                                # restart the LaunchAgent
```
The LaunchAgent (com.eduard.ccbot: KeepAlive, RunAtLoad, WorkingDirectory=/Users/mogaeduard — that field was a root-cause fix, the daemon ran with cwd=/ for days) runs the INSTALLED tool, not the repo checkout: **a repo edit does nothing in production until re-installed + kickstarted.** Per-host bring-up is parameterized in `~/.ccbot/SETUP-HOST.md` (runtime dir, this host — includes the future systemd unit for the Ubuntu box).

## Provenance and maintenance
- Written 2026-07-04 from: pyproject (:6–:14), .gitignore:71, config.py (:5, :22, :33–:40, :45, :49, :125), hook.py docstring (:1–:9), tests/conftest.py pattern, the LaunchAgent plist (this host), SETUP-HOST.md location.
- Re-verify: `grep -n 'raise ValueError' src/ccbot/config.py` · `head -10 src/ccbot/hook.py` · `grep -n uv.lock .gitignore` · `plutil -p ~/Library/LaunchAgents/com.eduard.ccbot.plist | grep -i workingdir`.
