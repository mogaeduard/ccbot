---
name: ccbot-testing-and-qa
description: Testing conventions in ccbot — the ~515-test pytest suite (conftest force-sets env BEFORE import, asyncio auto mode, unit vs integration split), the real-tmux drill pattern on throwaway sockets (-L batchtest, NEVER the default socket), the phone live-acceptance drill convention for phone-visible features, and how to add tests for handlers/parsers/mirror logic. Load this when: writing or debugging tests here; a test fails at collection (env!); testing anything tmux-touching; deciding what "verified" means for a phone-visible feature; or reviewing test coverage claims. Keywords: pytest, conftest, batchtest, throwaway socket, integration marker, live drill, phone verification, asyncio auto, 515 tests. Does NOT cover: the gates list (ccbot-change-control), tmux lifecycle design (ccbot-tmux-lifecycle), or parser internals being tested (ccbot-terminal-parsing).
---

# ccbot-testing-and-qa

**Use when** writing/running tests or defining verified. **Do NOT use when** you need the commit gates (→ ccbot-change-control).

## The suite (~515 `def test_` as of 2026-07-04, HEAD aa64a6d)

```bash
uv run pytest --tb=short -q
```

- Layout: `tests/ccbot/` (unit: test_bot, test_config, test_dashboard, test_forward_command, handlers/, ...) + `tests/integration/` (real-filesystem/process: config, monitor_state). Marker `integration` in pyproject.
- **The collection law:** `tests/conftest.py` force-sets `TELEGRAM_BOT_TOKEN`/`ALLOWED_USERS`/`CCBOT_DIR`(tempdir) BEFORE any ccbot import — the config singleton raises otherwise (→ ccbot-build-and-env law 1). A new test file that imports ccbot at module level is fine; a new CONFTEST or collection plugin that reorders imports is where this breaks.
- asyncio_mode=auto — async tests need no decorator.
- Expected-count convention: commit messages record the test count (the fork tracked 313→378→430→482→530+ across batches). After adding tests, note the new total in the commit body.

## The real-tmux drill pattern (the house specialty)

tmux-touching logic is validated against a REAL throwaway tmux server, never mocks and **never your live server**: `test_tmux_manager.py` runs "on socket `-L batchtest` (never the default socket)" (its own docstring, :15; `TMUX_SOCKET = "batchtest"` :146), spinning a plain server, exercising bracketed-paste multiline injection etc., and killing it after. For bigger lifecycle work there's the external drill script pattern (`~/ccbot-p2-drills.sh` on socket p2drill — this host). Copy this pattern for ANY new tmux behavior: throwaway socket, real server, kill in teardown (→ ccbot-tmux-lifecycle for why hooks can't be trusted without drills).

## Phone live-acceptance (code green ≠ done)

Phone-visible features (topics, dashboard, /speak, /term) additionally get a **live drill**: perform the exact user action on the phone, record what appeared. BACKLOG.md (runtime dir) tracks "PENDING LIVE VERIFICATION" items explicitly and batches don't close until drilled. When you ship such a feature: name the drill steps in the commit/BACKLOG, run them, record pass. The overnight-autonomy live test followed exactly this protocol.

## Adding tests by area

| Area | Pattern |
|---|---|
| handlers | `tests/ccbot/handlers/` — pytest-asyncio, fake Update/Context objects per existing files |
| parser heuristics | table-driven cases in test_transcript_parser/test_terminal_parser style — add BOTH the positive and the near-miss negative (heuristics regress silently) |
| mirror/reconciler | unit-test the decision function with synthetic window/topic sets; full-loop behavior = throwaway-socket drill |
| state/monitor | tests/integration/ with real tempdir FS |

## Provenance and maintenance
- Written 2026-07-04 from: grep count (515), tests/ layout, test_tmux_manager.py (:15, :146), conftest pattern, pyproject markers, BACKLOG conventions (this host).
- Re-verify: `grep -rc 'def test_' tests/ | awk -F: '{s+=$2} END {print s}'` · `grep -n batchtest tests/ccbot/test_tmux_manager.py | head -2`.
