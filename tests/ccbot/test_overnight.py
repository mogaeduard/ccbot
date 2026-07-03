"""Tests for overnight autonomy (overnight.py): the git snapshot function
against real temporary git repos, arm/disarm state, tick wiring
(auto-disarm on quiet-until expiry, repo dedupe across windows), and
morning-report formatting.

Snapshot/repo-root tests use real `git init` tempdirs (integration marker,
mirroring test_tmux_manager.py's real-tmux tests) — fast and hermetic, no
mocking of git itself needed since the whole point is verifying real git
state (HEAD/index/worktree untouched, ref chaining).
"""

import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ccbot import overnight
from ccbot.session import SessionManager
from ccbot.tmux_manager import TmuxWindow
from ccbot.utils import ccbot_dir


def _git_available() -> bool:
    return shutil.which("git") is not None


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path) -> Path:
    """A real git repo, one commit, plus a dirty worktree: a modified
    tracked file and an untracked file."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo_dir)
    _git("config", "user.email", "a@b.com", cwd=repo_dir)
    _git("config", "user.name", "test", cwd=repo_dir)
    (repo_dir / "a.txt").write_text("hello\n")
    _git("add", "a.txt", cwd=repo_dir)
    _git("commit", "-qm", "init", cwd=repo_dir)
    (repo_dir / "a.txt").write_text("hello\nmodified\n")
    (repo_dir / "b.txt").write_text("untracked\n")
    return repo_dir


@pytest.mark.integration
@pytest.mark.skipif(not _git_available(), reason="git binary not available")
class TestSnapshotRepoRealGit:
    @pytest.mark.asyncio
    async def test_creates_checkpoint_without_touching_worktree_index_head(
        self, repo
    ) -> None:
        head_before = _git("rev-parse", "HEAD", cwd=repo).strip()
        status_before = _git("status", "--porcelain", cwd=repo)
        a_before = (repo / "a.txt").read_text()

        night: dict[str, overnight.RepoNightState] = {}
        commit = await overnight.snapshot_repo(str(repo), night)

        assert commit is not None
        # worktree/index/HEAD untouched
        assert _git("rev-parse", "HEAD", cwd=repo).strip() == head_before
        assert _git("status", "--porcelain", cwd=repo) == status_before
        assert (repo / "a.txt").read_text() == a_before

        branch = _git("branch", "--show-current", cwd=repo).strip()
        today = time.strftime("%Y%m%d")
        ref = f"refs/heads/wip/overnight-{today}-{branch}"
        assert _git("rev-parse", ref, cwd=repo).strip() == commit

        # the checkpoint actually captured the dirty state, including the
        # untracked file
        assert _git("show", f"{commit}:a.txt", cwd=repo) == "hello\nmodified\n"
        assert _git("show", f"{commit}:b.txt", cwd=repo) == "untracked\n"

    @pytest.mark.asyncio
    async def test_parent_chaining_across_two_snapshots(self, repo) -> None:
        night: dict[str, overnight.RepoNightState] = {}
        c1 = await overnight.snapshot_repo(str(repo), night)
        assert c1 is not None

        (repo / "a.txt").write_text("hello\nmodified\nmore\n")
        c2 = await overnight.snapshot_repo(str(repo), night)
        assert c2 is not None
        assert c2 != c1

        parent = _git("rev-parse", f"{c2}^", cwd=repo).strip()
        assert parent == c1
        assert night[str(repo)].checkpoint_count == 2

    @pytest.mark.asyncio
    async def test_dedupe_when_no_actual_change(self, repo) -> None:
        night: dict[str, overnight.RepoNightState] = {}
        c1 = await overnight.snapshot_repo(str(repo), night)
        assert c1 is not None

        # status is still non-empty (same dirty state) but the resulting
        # tree is identical -> no new commit
        c2 = await overnight.snapshot_repo(str(repo), night)
        assert c2 is None
        assert night[str(repo)].checkpoint_count == 1
        assert night[str(repo)].last_commit == c1

    @pytest.mark.asyncio
    async def test_clean_worktree_returns_none(self, repo) -> None:
        _git("add", "-A", cwd=repo)
        _git("commit", "-qm", "clean it up", cwd=repo)
        night: dict[str, overnight.RepoNightState] = {}
        assert await overnight.snapshot_repo(str(repo), night) is None
        assert night == {}


@pytest.mark.integration
@pytest.mark.skipif(not _git_available(), reason="git binary not available")
class TestResolveRepoRoot:
    @pytest.mark.asyncio
    async def test_non_repo_skipped(self, tmp_path) -> None:
        assert await overnight._resolve_repo_root(str(tmp_path)) is None

    @pytest.mark.asyncio
    async def test_repo_resolves_to_toplevel(self, repo) -> None:
        expected = _git("rev-parse", "--show-toplevel", cwd=repo).strip()
        assert await overnight._resolve_repo_root(str(repo)) == expected


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    """Fresh SessionManager with persistence stubbed out, swapped into
    overnight.py's namespace — mirrors test_mirror.py's `mgr` fixture."""
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    m = SessionManager()
    monkeypatch.setattr(overnight, "session_manager", m)
    return m


@pytest.fixture(autouse=True)
def _clean_night_state():
    """_night_state is a module-level dict — reset around every test so
    they can't leak into each other."""
    overnight._night_state.clear()
    yield
    overnight._night_state.clear()


@pytest.fixture(autouse=True)
def _clean_quiet_until():
    """quiet-until lives under the shared test CCBOT_DIR — remove it
    before/after so tests don't see a stale value from another test."""
    f = ccbot_dir() / "quiet-until"
    f.unlink(missing_ok=True)
    yield
    f.unlink(missing_ok=True)


class TestArmDisarmState:
    def test_arm_sets_flag_and_starts_fresh_chain(self, mgr) -> None:
        overnight._night_state["/some/repo"] = overnight.RepoNightState(
            checkpoint_count=2
        )
        overnight.arm()
        assert mgr.is_overnight_armed() is True
        assert overnight._night_state == {}

    def test_arm_is_idempotent_preserves_in_progress_chain(self, mgr) -> None:
        overnight.arm()
        overnight._night_state["/some/repo"] = overnight.RepoNightState(
            checkpoint_count=2
        )
        overnight.arm()  # re-arm while already armed (e.g. a second /sleep)
        assert overnight._night_state != {}

    @pytest.mark.asyncio
    async def test_disarm_and_report_clears_state_and_dms(
        self, mgr, monkeypatch
    ) -> None:
        overnight.arm()
        overnight._night_state["/tmp/proj"] = overnight.RepoNightState(
            wip_ref="refs/heads/wip/overnight-20260703-main", checkpoint_count=2
        )
        monkeypatch.setattr(
            overnight,
            "_diffstat_summary",
            AsyncMock(return_value="1 file changed, 2 insertions(+)"),
        )
        mock_send = AsyncMock()
        monkeypatch.setattr(overnight, "safe_send", mock_send)
        bot = AsyncMock()

        await overnight.disarm_and_report(bot)

        assert mgr.is_overnight_armed() is False
        assert overnight._night_state == {}
        mock_send.assert_awaited_once()
        sent_bot, chat_id, text = mock_send.call_args.args
        assert sent_bot is bot
        assert chat_id == overnight._report_chat_id()
        assert "proj" in text


class TestOvernightTick:
    @pytest.mark.asyncio
    async def test_noop_when_not_armed(self, mgr, monkeypatch) -> None:
        list_windows = AsyncMock()
        monkeypatch.setattr(overnight.tmux_manager, "list_windows", list_windows)
        await overnight.overnight_tick(AsyncMock())
        list_windows.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_disarms_when_quiet_until_passed(self, mgr, monkeypatch) -> None:
        overnight.arm()
        quiet_file = ccbot_dir() / "quiet-until"
        quiet_file.write_text(str(int(time.time()) - 10))

        mock_disarm = AsyncMock()
        monkeypatch.setattr(overnight, "disarm_and_report", mock_disarm)
        list_windows = AsyncMock()
        monkeypatch.setattr(overnight.tmux_manager, "list_windows", list_windows)
        bot = AsyncMock()

        await overnight.overnight_tick(bot)

        mock_disarm.assert_awaited_once_with(bot)
        assert not quiet_file.exists()
        list_windows.assert_not_awaited()  # auto-wake short-circuits the pass

    @pytest.mark.asyncio
    async def test_dedupes_repos_across_windows(self, mgr, monkeypatch) -> None:
        overnight.arm()
        windows = [
            TmuxWindow(window_id="@0", window_name="a", cwd="/proj"),
            TmuxWindow(window_id="@1", window_name="b", cwd="/proj"),  # same repo
        ]
        monkeypatch.setattr(
            overnight.tmux_manager, "list_windows", AsyncMock(return_value=windows)
        )
        monkeypatch.setattr(
            overnight, "_resolve_repo_root", AsyncMock(return_value="/repo/root")
        )
        mock_snapshot = AsyncMock()
        monkeypatch.setattr(overnight, "snapshot_repo", mock_snapshot)

        await overnight.overnight_tick(AsyncMock())

        mock_snapshot.assert_awaited_once_with("/repo/root", overnight._night_state)

    @pytest.mark.asyncio
    async def test_skips_windows_with_no_cwd_and_non_repos(
        self, mgr, monkeypatch
    ) -> None:
        overnight.arm()
        windows = [TmuxWindow(window_id="@0", window_name="shell", cwd="")]
        monkeypatch.setattr(
            overnight.tmux_manager, "list_windows", AsyncMock(return_value=windows)
        )
        resolve = AsyncMock()
        monkeypatch.setattr(overnight, "_resolve_repo_root", resolve)
        mock_snapshot = AsyncMock()
        monkeypatch.setattr(overnight, "snapshot_repo", mock_snapshot)

        await overnight.overnight_tick(AsyncMock())

        resolve.assert_not_awaited()  # empty cwd skipped before repo resolution
        mock_snapshot.assert_not_awaited()


class TestFormatMorningReport:
    def test_no_summaries(self) -> None:
        assert (
            overnight.format_morning_report([])
            == "Quiet night — no changes to checkpoint."
        )

    def test_singular_checkpoint_wording(self) -> None:
        s = overnight.RepoSummary(
            "proj", "wip/overnight-20260703-main", 1, "1 file changed"
        )
        text = overnight.format_morning_report([s])
        assert "1 checkpoint —" in text
        assert "checkpoints" not in text

    def test_plural_multi_repo_and_footer(self) -> None:
        s1 = overnight.RepoSummary(
            "proj1",
            "wip/overnight-20260703-main",
            3,
            "3 files changed, 10 insertions(+)",
        )
        s2 = overnight.RepoSummary(
            "proj2", "wip/overnight-20260703-dev", 1, "1 file changed, 2 deletions(-)"
        )
        text = overnight.format_morning_report([s1, s2])
        assert (
            "proj1 (wip/overnight-20260703-main): 3 checkpoints — "
            "3 files changed, 10 insertions(+)" in text
        )
        assert (
            "proj2 (wip/overnight-20260703-dev): 1 checkpoint — "
            "1 file changed, 2 deletions(-)" in text
        )
        assert "Promote with: git merge <wip-branch> (or cherry-pick)" in text
        assert "discard with: git branch -D <wip-branch>." in text


class TestBuildMorningReport:
    @pytest.mark.asyncio
    async def test_skips_repos_with_zero_checkpoints(self, monkeypatch) -> None:
        monkeypatch.setattr(
            overnight, "_diffstat_summary", AsyncMock(return_value="1 file changed")
        )
        night = {
            "/repo/a": overnight.RepoNightState(
                wip_ref="refs/heads/wip/overnight-20260703-main", checkpoint_count=2
            ),
            "/repo/b": overnight.RepoNightState(checkpoint_count=0),
        }
        text = await overnight.build_morning_report(night)
        assert "a (" in text
        assert "b (" not in text
