"""Overnight autonomy — checkpoint dirty git repos while the user sleeps.

Armed by /sleep, disarmed by /wake (bot.py), which also triggers the
morning report DM. A background task (overnight_poll_loop, started
unconditionally from bot.py's post_init like mirror_poll_loop) ticks every
OVERNIGHT_POLL_INTERVAL_S while armed: for each live tmux window's git repo
with uncommitted changes (deduped across windows sharing a repo), it writes
a checkpoint commit onto a parallel `wip/overnight-<date>-<branch>` ref
WITHOUT touching the real worktree, index, or HEAD — the live session never
notices. Also auto-disarms (+reports) the first tick after the /sleep
quiet-until time has passed, in case /wake was never sent.

Key functions:
  - arm / disarm_and_report: called from bot.py's sleep_command/wake_command.
  - overnight_tick / overnight_poll_loop: the background snapshot loop.
  - snapshot_repo: one repo's checkpoint (temp-index add -> commit-tree ->
    update-ref).
  - format_morning_report / build_morning_report: the wake-up DM.
"""

import asyncio
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from telegram import Bot

from .config import config
from .handlers.message_sender import safe_send
from .session import session_manager
from .tmux_manager import tmux_manager
from .utils import ccbot_dir

logger = logging.getLogger(__name__)

OVERNIGHT_POLL_INTERVAL_S = 600  # 10 minutes
GIT_TIMEOUT_S = 10.0

# commit-tree requires a committer identity; don't depend on the machine's
# global git config being set (e.g. CI runners).
_GIT_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "ccbot-overnight",
    "GIT_AUTHOR_EMAIL": "ccbot-overnight@localhost",
    "GIT_COMMITTER_NAME": "ccbot-overnight",
    "GIT_COMMITTER_EMAIL": "ccbot-overnight@localhost",
}


@dataclass
class RepoNightState:
    """In-memory checkpoint chain for one repo across the current armed
    night. Not persisted — a daemon restart mid-night just starts a fresh
    chain off HEAD on the next tick, which is harmless since every
    checkpoint commit is a complete, self-contained snapshot."""

    wip_ref: str = ""
    last_tree: str | None = None
    last_commit: str | None = None
    checkpoint_count: int = 0


# repo root -> RepoNightState, for the current armed night. Cleared on arm().
_night_state: dict[str, RepoNightState] = {}


async def _run_git(
    *args: str, cwd: str, extra_env: dict[str, str] | None = None
) -> tuple[int, bytes, bytes]:
    """Run `git <args>` in cwd with a timeout. Never raises — returns
    (-1, b"", <error>) on a timeout or exec failure so callers can treat it
    identically to a failed git command."""
    env = {**os.environ, **_GIT_IDENTITY_ENV, **(extra_env or {})}
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        return -1, b"", str(e).encode()
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=GIT_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return -1, b"", b"git command timed out"
    return proc.returncode if proc.returncode is not None else -1, stdout, stderr


async def _resolve_repo_root(cwd: str) -> str | None:
    """Git repo root for cwd, or None if cwd isn't inside a repo."""
    code, out, _ = await _run_git("rev-parse", "--show-toplevel", cwd=cwd)
    if code != 0:
        return None
    return out.decode().strip()


async def _current_branch(repo: str) -> str | None:
    """Current branch name, or None on detached HEAD / failure."""
    code, out, _ = await _run_git("branch", "--show-current", cwd=repo)
    if code != 0:
        return None
    name = out.decode().strip()
    return name or None


async def snapshot_repo(
    repo: str, night_state: dict[str, RepoNightState]
) -> str | None:
    """Checkpoint one repo's dirty worktree onto its wip/overnight-* branch,
    without touching the real worktree, index, or HEAD (a temporary
    GIT_INDEX_FILE is used to build the tree). Returns the new checkpoint
    commit sha, or None if there was nothing to snapshot: clean worktree,
    detached HEAD, an unchanged tree since the last checkpoint this night,
    or any failed git call along the way (logged, never raised)."""
    code, status_out, _ = await _run_git("status", "--porcelain", cwd=repo)
    if code != 0 or not status_out.strip():
        return None

    branch = await _current_branch(repo)
    if not branch:
        logger.warning("Overnight: %s has no current branch, skipping", repo)
        return None

    with tempfile.TemporaryDirectory() as tmpdir:
        index_env = {"GIT_INDEX_FILE": str(Path(tmpdir) / "index")}
        code, _, err = await _run_git(
            "read-tree", "HEAD", cwd=repo, extra_env=index_env
        )
        if code != 0:
            logger.warning(
                "Overnight: read-tree failed for %s: %s",
                repo,
                err.decode(errors="replace"),
            )
            return None
        code, _, err = await _run_git("add", "-A", cwd=repo, extra_env=index_env)
        if code != 0:
            logger.warning(
                "Overnight: add -A failed for %s: %s",
                repo,
                err.decode(errors="replace"),
            )
            return None
        code, tree_out, err = await _run_git(
            "write-tree", cwd=repo, extra_env=index_env
        )
        if code != 0:
            logger.warning(
                "Overnight: write-tree failed for %s: %s",
                repo,
                err.decode(errors="replace"),
            )
            return None
    tree = tree_out.decode().strip()

    state = night_state.setdefault(repo, RepoNightState())
    if tree == state.last_tree:
        return None  # dedupe: worktree churned but landed on the same tree

    parent = state.last_commit
    if parent is None:
        code, head_out, _ = await _run_git("rev-parse", "HEAD", cwd=repo)
        if code != 0:
            return None
        parent = head_out.decode().strip()

    message = f"overnight checkpoint {datetime.now().strftime('%H:%M')}"
    code, commit_out, err = await _run_git(
        "commit-tree", tree, "-p", parent, "-m", message, cwd=repo
    )
    if code != 0:
        logger.warning(
            "Overnight: commit-tree failed for %s: %s",
            repo,
            err.decode(errors="replace"),
        )
        return None
    commit = commit_out.decode().strip()

    ref = f"refs/heads/wip/overnight-{datetime.now().strftime('%Y%m%d')}-{branch}"
    code, _, err = await _run_git("update-ref", ref, commit, cwd=repo)
    if code != 0:
        logger.warning(
            "Overnight: update-ref failed for %s: %s",
            repo,
            err.decode(errors="replace"),
        )
        return None

    state.wip_ref = ref
    state.last_tree = tree
    state.last_commit = commit
    state.checkpoint_count += 1
    logger.info("Overnight: checkpointed %s -> %s (%s)", repo, ref, commit[:8])
    return commit


def _read_quiet_until() -> int | None:
    """UNIX epoch from ~/.ccbot/quiet-until (written by /sleep), or None."""
    try:
        return int((ccbot_dir() / "quiet-until").read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _report_chat_id() -> int:
    """DM target for the morning report — same convention as mirror.py's
    _mirror_user_id (single-user bot; lowest allowed_users id is stable)."""
    return min(config.allowed_users)


def arm() -> None:
    """Called from /sleep. Idempotent: re-arming (e.g. pushing the wake
    time back with a second /sleep) does not reset an in-progress chain —
    only a fresh disarmed->armed transition starts a new one."""
    if not session_manager.is_overnight_armed():
        _night_state.clear()
        session_manager.set_overnight_armed(True)


async def disarm_and_report(bot: Bot) -> None:
    """Called from /wake (when armed) and from the poll loop's own
    quiet-until expiry check. Disarms, DMs the morning report, and clears
    the night's in-memory checkpoint chain."""
    report = await build_morning_report(_night_state)
    _night_state.clear()
    session_manager.set_overnight_armed(False)
    await safe_send(bot, _report_chat_id(), report)


@dataclass
class RepoSummary:
    name: str
    wip_branch: str
    checkpoint_count: int
    diffstat: str


def format_morning_report(summaries: list[RepoSummary]) -> str:
    """Pure formatting, no I/O — the wake-up DM text."""
    if not summaries:
        return "Quiet night — no changes to checkpoint."
    lines = [
        f"{s.name} ({s.wip_branch}): {s.checkpoint_count} checkpoint"
        f"{'' if s.checkpoint_count == 1 else 's'} — {s.diffstat}"
        for s in summaries
    ]
    lines.append("")
    lines.append(
        "Promote with: git merge <wip-branch> (or cherry-pick), "
        "discard with: git branch -D <wip-branch>."
    )
    return "\n".join(lines)


async def _diffstat_summary(repo: str, wip_ref: str) -> str:
    code, out, _ = await _run_git("diff", "--stat", f"HEAD..{wip_ref}", cwd=repo)
    if code != 0:
        return "(diff unavailable)"
    lines = out.decode(errors="replace").strip().splitlines()
    return lines[-1].strip() if lines else "(no changes)"


async def build_morning_report(night_state: dict[str, RepoNightState]) -> str:
    """Format the night's checkpoints into the wake-up DM text."""
    summaries = [
        RepoSummary(
            name=Path(repo).name,
            wip_branch=state.wip_ref.removeprefix("refs/heads/"),
            checkpoint_count=state.checkpoint_count,
            diffstat=await _diffstat_summary(repo, state.wip_ref),
        )
        for repo, state in night_state.items()
        if state.checkpoint_count > 0
    ]
    return format_morning_report(summaries)


async def overnight_tick(bot: Bot) -> None:
    """One armed-night pass: auto-disarm+report if quiet-until has passed,
    otherwise snapshot every dirty repo behind a live tmux window (deduped
    by repo root — several windows can share one working directory)."""
    if not session_manager.is_overnight_armed():
        return

    quiet_until = _read_quiet_until()
    if quiet_until is not None and time.time() >= quiet_until:
        (ccbot_dir() / "quiet-until").unlink(missing_ok=True)
        await disarm_and_report(bot)
        return

    windows = await tmux_manager.list_windows()
    seen_repos: set[str] = set()
    for w in windows:
        if not w.cwd:
            continue
        repo = await _resolve_repo_root(w.cwd)
        if not repo or repo in seen_repos:
            continue
        seen_repos.add(repo)
        try:
            await snapshot_repo(repo, _night_state)
        except Exception as e:
            logger.error("Overnight: snapshot crashed for %s: %s", repo, e)


async def overnight_poll_loop(bot: Bot) -> None:
    """Background task started unconditionally in post_init (mirrors
    mirror_poll_loop). Armed state is a runtime toggle (/sleep, /wake), so
    this loop must always be alive to pick it up without a daemon restart."""
    while True:
        try:
            await overnight_tick(bot)
        except Exception as e:
            logger.error("Overnight poll loop error: %s", e)
        await asyncio.sleep(OVERNIGHT_POLL_INTERVAL_S)
