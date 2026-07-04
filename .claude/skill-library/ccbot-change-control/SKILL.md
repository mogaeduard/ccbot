---
name: ccbot-change-control
description: What "done" means in ccbot — the pre-commit gates (uv run ruff check + format, pyright with ZERO errors, pytest ~515 tests), the module-docstring rule, commit-message conventions (test counts + verification evidence in the body, the fork's established style), secrets discipline (token only in ~/.ccbot/.env chmod 600, sensitive vars scrubbed), the restart-after-change requirement, and branch rules on the fork. Load this when: preparing any commit here; reviewing a diff; a PR/commit claims done; adding a module (docstring rule); or wondering whether to restart the bot after a change. Keywords: gates, ruff, pyright, pytest, commit message, docstring, done, restart, secrets, .env. Does NOT cover: running the suites' content (ccbot-testing-and-qa), deployment/restart mechanics detail (ccbot-run-and-operate), fork/upstream strategy (ccbot-fork-vs-upstream).
---

# ccbot-change-control

**Use when** shipping any change. **Do NOT use when** you need test-suite details (→ ccbot-testing-and-qa) or run/restart mechanics (→ ccbot-run-and-operate).

## The gates (CLAUDE.md:10–12, verbatim requirements)

```bash
uv run ruff check src/ tests/     # MUST pass before committing
uv run ruff format src/ tests/    # auto-fix, then verify with --check
uv run pyright src/ccbot/         # MUST be 0 errors
uv run pytest --tb=short -q       # ~515 tests as of 2026-07-04 (grows with every batch)
```

Red gate = not done. After code changes, **restart the service** (production = `launchctl kickstart -k gui/$(id -u)/com.eduard.ccbot`; the repo's scripts/restart.sh is the DEV-mode path expecting a `ccbot:__main__` tmux window — → ccbot-run-and-operate for which world you're in).

## Conventions (all enforced by precedent in the fork's 19 commits)

- **Module docstrings** (CLAUDE.md:29): every .py starts with a module-level docstring — purpose clear within 10 lines, one-sentence summary first line, then responsibilities/components.
- **Commit messages carry evidence:** the fork's established style records test counts and verification method in bodies (BACKLOG tracked 313→378→430→482→530+ across batches; commits cite "verified against a real throwaway tmux server"). A behavior change without its verification story is incomplete.
- Subjects: `feat:`/`fix:` imperative, the symptom named plainly (see `752bf1f` "idle topic deletion from phone goes unnoticed — active TYPING probe" — symptom AND mechanism in one line).
- Config-gated features default OFF (upstream-friendliness — → ccbot-architecture-contract rule 7).
- `ponytail:` comments mark deliberate shortcuts with their ceiling; removing one = doing the upgrade it names.

## Secrets and files

- Token/ALLOWED_USERS live ONLY in `~/.ccbot/.env` (chmod 600) — never in the repo; config.py scrubs `SENSITIVE_ENV_VARS` from os.environ after load so children never inherit (config.py:22,:125).
- **uv.lock is gitignored** (.gitignore:71, upstream decision) — there is NO committed lockfile; deps resolve from pyproject ranges. A mysterious breakage after `uv sync` = check for an upstream dep release (→ ccbot-build-and-env).
- Branch rules: work lands on `auto-topic-mirror`, pushed to `fork`; local `main` stays a clean upstream mirror for diffing (→ ccbot-fork-vs-upstream).

## Live-verification duty (this bot's specialty)

Code green ≠ done here: phone-visible behavior (topics, dashboard, voice) needs a **live phone drill** — the BACKLOG convention tracks "PENDING LIVE VERIFICATION" items explicitly. A change to mirror/dashboard/voice ships with its drill result noted (which command, what appeared on the phone). tmux-touching changes drill on a THROWAWAY socket first (→ ccbot-tmux-lifecycle).

## Provenance and maintenance
- Written 2026-07-04 (HEAD aa64a6d) from: CLAUDE.md (:10–12, :29), .gitignore:71, config.py (:22,:125), commit-log style, ~/.ccbot conventions ("this host": BACKLOG.md lives in the runtime dir, not the repo).
- Re-verify: `grep -n 'MUST' CLAUDE.md` · `uv run pytest --co -q 2>/dev/null | tail -1` (current count) · `grep -n 'uv.lock' .gitignore`.
