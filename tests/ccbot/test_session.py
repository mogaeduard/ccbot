"""Tests for SessionManager pure dict operations."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import ccbot.session as session_module
from ccbot.session import SessionManager, WindowState
from ccbot.tmux_manager import TmuxWindow


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    return SessionManager()


class TestThreadBindings:
    def test_bind_and_get(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        assert mgr.get_window_for_thread(100, 1) == "@1"

    def test_bind_unbind_get_returns_none(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        mgr.unbind_thread(100, 1)
        assert mgr.get_window_for_thread(100, 1) is None

    def test_unbind_nonexistent_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.unbind_thread(100, 999) is None

    def test_iter_thread_bindings(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        mgr.bind_thread(100, 2, "@2")
        mgr.bind_thread(200, 3, "@3")
        result = set(mgr.iter_thread_bindings())
        assert result == {(100, 1, "@1"), (100, 2, "@2"), (200, 3, "@3")}


class TestGroupChatId:
    """Tests for group chat_id routing (supergroup forum topic support).

    IMPORTANT: These tests protect against regression. The group_chat_ids
    mapping is required for Telegram supergroup forum topics — without it,
    all outbound messages fail with "Message thread not found". This was
    erroneously removed once (26cb81f) and restored in PR #23. Do NOT
    delete these tests or the underlying functionality.
    """

    def test_resolve_with_stored_group_id(self, mgr: SessionManager) -> None:
        """resolve_chat_id returns stored group chat_id for known thread."""
        mgr.set_group_chat_id(100, 1, -1001234567890)
        assert mgr.resolve_chat_id(100, 1) == -1001234567890

    def test_resolve_without_group_id_falls_back_to_user_id(
        self, mgr: SessionManager
    ) -> None:
        """resolve_chat_id falls back to user_id when no group_id stored."""
        assert mgr.resolve_chat_id(100, 1) == 100

    def test_resolve_none_thread_id_falls_back_to_user_id(
        self, mgr: SessionManager
    ) -> None:
        """resolve_chat_id returns user_id when thread_id is None (private chat)."""
        mgr.set_group_chat_id(100, 1, -1001234567890)
        assert mgr.resolve_chat_id(100) == 100

    def test_set_group_chat_id_overwrites(self, mgr: SessionManager) -> None:
        """set_group_chat_id updates the stored value on change."""
        mgr.set_group_chat_id(100, 1, -999)
        mgr.set_group_chat_id(100, 1, -888)
        assert mgr.resolve_chat_id(100, 1) == -888

    def test_multiple_threads_independent(self, mgr: SessionManager) -> None:
        """Different threads for the same user store independent group chat_ids."""
        mgr.set_group_chat_id(100, 1, -111)
        mgr.set_group_chat_id(100, 2, -222)
        assert mgr.resolve_chat_id(100, 1) == -111
        assert mgr.resolve_chat_id(100, 2) == -222

    def test_multiple_users_independent(self, mgr: SessionManager) -> None:
        """Different users store independent group chat_ids."""
        mgr.set_group_chat_id(100, 1, -111)
        mgr.set_group_chat_id(200, 1, -222)
        assert mgr.resolve_chat_id(100, 1) == -111
        assert mgr.resolve_chat_id(200, 1) == -222

    def test_set_group_chat_id_with_none_thread(self, mgr: SessionManager) -> None:
        """set_group_chat_id handles None thread_id (mapped to 0)."""
        mgr.set_group_chat_id(100, None, -999)
        # thread_id=None in resolve falls back to user_id (by design)
        assert mgr.resolve_chat_id(100, None) == 100
        # The stored key is "100:0", only accessible with explicit thread_id=0
        assert mgr.group_chat_ids.get("100:0") == -999


class TestWindowState:
    def test_get_creates_new(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@0")
        assert state.session_id == ""
        assert state.cwd == ""

    def test_get_returns_existing(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "abc"
        assert mgr.get_window_state("@1").session_id == "abc"

    def test_clear_window_session(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "abc"
        mgr.clear_window_session("@1")
        assert mgr.get_window_state("@1").session_id == ""


class TestResolveWindowForThread:
    def test_none_thread_id_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.resolve_window_for_thread(100, None) is None

    def test_unbound_thread_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.resolve_window_for_thread(100, 42) is None

    def test_bound_thread_returns_window(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 42, "@3")
        assert mgr.resolve_window_for_thread(100, 42) == "@3"


class TestDisplayNames:
    def test_get_display_name_fallback(self, mgr: SessionManager) -> None:
        """get_display_name returns window_id when no display name is set."""
        assert mgr.get_display_name("@99") == "@99"

    def test_set_and_get_display_name(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="myproject")
        assert mgr.get_display_name("@1") == "myproject"

    def test_set_display_name_update(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="old-name")
        mgr.window_display_names["@1"] = "new-name"
        assert mgr.get_display_name("@1") == "new-name"

    def test_bind_thread_sets_display_name(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="proj")
        assert mgr.get_display_name("@1") == "proj"

    def test_bind_thread_without_name_no_display(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        # No display name set, fallback to window_id
        assert mgr.get_display_name("@1") == "@1"


class TestIsWindowId:
    def test_valid_ids(self, mgr: SessionManager) -> None:
        assert mgr._is_window_id("@0") is True
        assert mgr._is_window_id("@12") is True
        assert mgr._is_window_id("@999") is True

    def test_invalid_ids(self, mgr: SessionManager) -> None:
        assert mgr._is_window_id("myproject") is False
        assert mgr._is_window_id("@") is False
        assert mgr._is_window_id("") is False
        assert mgr._is_window_id("@abc") is False


class TestInjectionEcho:
    """Echo suppression tracking (fix: ccbot's own tmux injections shouldn't
    be mirrored back to the user as if they were a second copy of their
    message — see was_recently_injected / send_to_window)."""

    def test_matches_recent_injection(self, mgr: SessionManager) -> None:
        mgr._record_injected_text("@1", "hello world")
        assert mgr.was_recently_injected("@1", "hello world") is True

    def test_no_match_for_different_text(self, mgr: SessionManager) -> None:
        mgr._record_injected_text("@1", "hello world")
        assert mgr.was_recently_injected("@1", "something else") is False

    def test_normalizes_whitespace(self, mgr: SessionManager) -> None:
        """Multi-space injected text still matches after normalization
        (tmux keystroke injection can perturb whitespace)."""
        mgr._record_injected_text("@1", "hello   world\n\nfoo")
        assert mgr.was_recently_injected("@1", "hello world foo") is True

    def test_normalizes_whitespace_exact_form(self, mgr: SessionManager) -> None:
        """Same normalization applies when queried with the original
        (unnormalized) form — a separate injection so one-shot consumption
        (see TestEchoOneShotConsumption) doesn't make this a re-test of the
        same match."""
        mgr._record_injected_text("@1", "hello   world\n\nfoo")
        assert mgr.was_recently_injected("@1", "hello   world\n\nfoo") is True

    def test_per_window_isolation(self, mgr: SessionManager) -> None:
        mgr._record_injected_text("@1", "hello")
        assert mgr.was_recently_injected("@2", "hello") is False

    def test_no_injections_returns_false(self, mgr: SessionManager) -> None:
        assert mgr.was_recently_injected("@99", "anything") is False

    def test_empty_text_never_matches(self, mgr: SessionManager) -> None:
        mgr._record_injected_text("@1", "   ")
        assert mgr.was_recently_injected("@1", "") is False
        assert mgr.was_recently_injected("@1", "   ") is False

    def test_expires_after_echo_window(self, mgr: SessionManager) -> None:
        mgr._record_injected_text("@1", "hello world")
        # Simulate the injection having happened just past the echo window.
        ts, text = mgr._recent_injections["@1"][-1]
        mgr._recent_injections["@1"][-1] = (
            ts - mgr._INJECTION_ECHO_WINDOW_SECONDS - 1,
            text,
        )
        assert mgr.was_recently_injected("@1", "hello world") is False

    def test_bounded_history_per_window(self, mgr: SessionManager) -> None:
        for i in range(mgr._INJECTION_HISTORY_MAXLEN + 5):
            mgr._record_injected_text("@1", f"msg {i}")
        assert len(mgr._recent_injections["@1"]) == mgr._INJECTION_HISTORY_MAXLEN
        # Only the most recent MAXLEN survive.
        assert mgr.was_recently_injected("@1", "msg 0") is False
        assert mgr.was_recently_injected("@1", "msg 24") is True


class TestEchoOneShotConsumption:
    """was_recently_injected consumes the matched entry — one injection can
    only suppress one transcript echo. Regression coverage for the bug where
    a user typing the same short word ("yes", "ok") on the Mac within the
    90s window of sending it from the phone had their Mac message silently
    swallowed as if it were ccbot's own echo."""

    def test_second_call_with_same_text_is_not_suppressed(
        self, mgr: SessionManager
    ) -> None:
        mgr._record_injected_text("@1", "yes")
        assert mgr.was_recently_injected("@1", "yes") is True
        # The matched entry was consumed — a second, genuinely-typed "yes"
        # (e.g. typed directly on the Mac) must mirror normally, not vanish.
        assert mgr.was_recently_injected("@1", "yes") is False

    def test_only_the_matching_entry_is_consumed(self, mgr: SessionManager) -> None:
        mgr._record_injected_text("@1", "yes")
        mgr._record_injected_text("@1", "no")
        assert mgr.was_recently_injected("@1", "yes") is True
        # "no" is untouched — still matches
        assert mgr.was_recently_injected("@1", "no") is True

    def test_two_injections_of_same_text_each_suppress_once(
        self, mgr: SessionManager
    ) -> None:
        """Two separate injections of the same text (e.g. two Sends of "ok"
        from the phone) each get their own echo suppressed."""
        mgr._record_injected_text("@1", "ok")
        mgr._record_injected_text("@1", "ok")
        assert mgr.was_recently_injected("@1", "ok") is True
        assert mgr.was_recently_injected("@1", "ok") is True
        assert mgr.was_recently_injected("@1", "ok") is False


class TestSendToWindowRecordsInjection:
    """send_to_window is the single choke point every text-injection path
    (typed messages, photo captions, voice transcripts, pending-thread
    forwards) routes through — recording the echo there covers all of them."""

    @pytest.mark.asyncio
    async def test_send_to_window_records_text(
        self, mgr: SessionManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_window = MagicMock()
        mock_window.window_id = "@1"
        mock_tmux = MagicMock()
        mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
        mock_tmux.send_keys = AsyncMock(return_value=True)
        monkeypatch.setattr(session_module, "tmux_manager", mock_tmux)

        success, _ = await mgr.send_to_window("@1", "hello there")

        assert success is True
        assert mgr.was_recently_injected("@1", "hello there") is True

    @pytest.mark.asyncio
    async def test_missing_window_does_not_record(
        self, mgr: SessionManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_tmux = MagicMock()
        mock_tmux.find_window_by_id = AsyncMock(return_value=None)
        monkeypatch.setattr(session_module, "tmux_manager", mock_tmux)

        success, _ = await mgr.send_to_window("@1", "hello there")

        assert success is False
        assert mgr.was_recently_injected("@1", "hello there") is False


class TestLockedState:
    """/lock kill switch: defaults unlocked, toggles idempotently, and only
    writes state.json on an actual change (matches set_group_chat_id's
    write-on-change convention elsewhere in this class)."""

    def test_defaults_unlocked(self, mgr: SessionManager) -> None:
        assert mgr.is_locked() is False

    def test_set_locked_true(self, mgr: SessionManager) -> None:
        mgr.set_locked(True)
        assert mgr.is_locked() is True

    def test_set_locked_false_after_true(self, mgr: SessionManager) -> None:
        mgr.set_locked(True)
        mgr.set_locked(False)
        assert mgr.is_locked() is False

    def test_set_locked_only_saves_on_change(
        self, mgr: SessionManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_calls = []
        monkeypatch.setattr(mgr, "_save_state", lambda: save_calls.append(1))

        mgr.set_locked(False)  # already False — no-op
        assert save_calls == []

        mgr.set_locked(True)
        assert save_calls == [1]

        mgr.set_locked(True)  # already True — no-op
        assert save_calls == [1]


class TestLockedStatePersistence:
    """Real filesystem round trip: locked survives a fresh SessionManager
    instance reading the same state.json — i.e. survives a daemon restart."""

    def test_locked_true_survives_reload(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            session_module.config, "state_file", tmp_path / "state.json"
        )

        first = SessionManager()
        first.set_locked(True)

        second = SessionManager()
        assert second.is_locked() is True

    def test_locked_false_survives_reload(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            session_module.config, "state_file", tmp_path / "state.json"
        )

        first = SessionManager()
        first.set_locked(True)
        first.set_locked(False)

        second = SessionManager()
        assert second.is_locked() is False

    def test_no_state_file_defaults_unlocked(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            session_module.config, "state_file", tmp_path / "state.json"
        )
        mgr = SessionManager()
        assert mgr.is_locked() is False


class TestResolveStaleIds:
    """resolve_stale_ids re-maps topics across a tmux restart (window IDs
    reset) by the stable Terminal-N number, and never orphans a topic: an
    unmatched binding is retained so the mirror's dead-topic reconciler
    deletes it rather than the binding being dropped and the topic stranded."""

    def _live(
        self,
        monkeypatch,
        windows: list[TmuxWindow],
        server_start: str = "srv-1",
    ) -> None:
        monkeypatch.setattr(
            session_module.tmux_manager,
            "list_windows",
            AsyncMock(return_value=windows),
        )
        monkeypatch.setattr(
            session_module.tmux_manager,
            "get_server_start_time",
            AsyncMock(return_value=server_start),
        )

    @pytest.mark.asyncio
    async def test_rematch_by_index_when_ai_title_drifts(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        # Topic bound to old window @1, titled "1 — old title".
        mgr.bind_thread(100, 10, "@1", window_name="1 — old title")
        # After restart: same Terminal 1, new tmux id @9, drifted ai-title.
        self._live(
            monkeypatch,
            [TmuxWindow("@9", "1 — new title", "/x", window_index="1")],
        )
        await mgr.resolve_stale_ids()
        # Re-mapped to the live window by its number — reused, not orphaned.
        assert mgr.get_window_for_thread(100, 10) == "@9"

    @pytest.mark.asyncio
    async def test_parks_binding_when_no_live_window(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        mgr.bind_thread(100, 20, "@2", window_name="2 — foo")
        # No live window matches the name OR the Terminal-2 number.
        self._live(
            monkeypatch,
            [TmuxWindow("@9", "1 — bar", "/x", window_index="1")],
        )
        await mgr.resolve_stale_ids()
        # PARKED: the value becomes the topic name (never a dead id, which
        # fed the mirror's dead-topic deletion and destroyed real topic
        # history; never droppable, which orphaned the topic forever).
        assert mgr.get_window_for_thread(100, 20) == "2 — foo"

    @pytest.mark.asyncio
    async def test_parked_binding_reattaches_by_slot_on_later_restart(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        mgr.bind_thread(100, 20, "2 — foo", window_name="")
        self._live(
            monkeypatch,
            [TmuxWindow("@4", "zsh", "/x", window_index="2")],
        )
        await mgr.resolve_stale_ids()
        assert mgr.get_window_for_thread(100, 20) == "@4"
        assert mgr.get_display_name("@4") == "2 — foo"

    @pytest.mark.asyncio
    async def test_slot_fallback_rejected_on_cwd_mismatch(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        """A different project reopened in the same slot must NOT inherit
        the topic — that misroutes the user's keystrokes into it."""
        mgr.bind_thread(100, 20, "@2", window_name="2 — kolab")
        mgr.window_states["@2"] = WindowState(session_id="s", cwd="/home/kolab")
        self._live(
            monkeypatch,
            [TmuxWindow("@4", "zsh", "/home/eterni", window_index="2")],
        )
        await mgr.resolve_stale_ids()
        assert mgr.get_window_for_thread(100, 20) == "2 — kolab"  # parked

    @pytest.mark.asyncio
    async def test_slot_fallback_never_claims_a_window_twice(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        """1 topic = 1 window: two bindings must not resolve to one window."""
        mgr.bind_thread(100, 20, "@2", window_name="1 — foo")
        mgr.bind_thread(100, 30, "@3", window_name="1 — bar")
        self._live(
            monkeypatch,
            [TmuxWindow("@7", "zsh", "/x", window_index="1")],
        )
        await mgr.resolve_stale_ids()
        bound = [wid for _, _, wid in mgr.iter_thread_bindings() if wid == "@7"]
        assert len(bound) == 1

    @pytest.mark.asyncio
    async def test_recycled_window_id_is_not_trusted_after_server_restart(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        """tmux restarts recycle ids from @0: a live @2 belonging to a
        DIFFERENT terminal must not keep the old binding — the topic's
        keystrokes would land in an unrelated session."""
        mgr.tmux_server_start = "srv-old"  # state saved under a dead server
        mgr.bind_thread(100, 20, "@2", window_name="2 — kolab")
        # @2 exists but is now slot 5 named differently; slot 2 is @6.
        self._live(
            monkeypatch,
            [
                TmuxWindow("@2", "5 — eterni", "/e", window_index="5"),
                TmuxWindow("@6", "zsh", "/k", window_index="2"),
            ],
            server_start="srv-new",
        )
        await mgr.resolve_stale_ids()
        assert mgr.get_window_for_thread(100, 20) == "@6"
        assert mgr.tmux_server_start == "srv-new"

    @pytest.mark.asyncio
    async def test_live_id_trusted_on_same_server_despite_display_drift(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        """Ids are never recycled while one tmux server lives: same
        start_time → a live id keeps its binding even when the recorded
        display name is garbage (real drifted state observed live)."""
        mgr.tmux_server_start = "srv-1"
        mgr.bind_thread(100, 20, "@2", window_name="2_1_214")  # junk display
        self._live(
            monkeypatch,
            [TmuxWindow("@2", "2 — MacBook freezing issues", "/x", window_index="2")],
            server_start="srv-1",
        )
        await mgr.resolve_stale_ids()
        assert mgr.get_window_for_thread(100, 20) == "@2"

    @pytest.mark.asyncio
    async def test_purely_numeric_window_name_is_not_a_slot(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        """A window literally named "3" has no Terminal-N prefix; it must
        never index-match an arbitrary terminal after a restart."""
        mgr.bind_thread(100, 20, "@2", window_name="3")
        self._live(
            monkeypatch,
            [TmuxWindow("@7", "zsh", "/x", window_index="3")],
        )
        await mgr.resolve_stale_ids()
        assert mgr.get_window_for_thread(100, 20) == "3"  # parked, not @7

    @pytest.mark.asyncio
    async def test_slot_rematch_carries_states_and_offsets(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        """window_states and read offsets must follow the SAME re-mapping as
        the binding — diverging silently unroutes the terminal's output and
        re-ingests its whole transcript."""
        mgr.bind_thread(100, 10, "@1", window_name="1 — old title")
        mgr.window_states["@1"] = WindowState(session_id="sess-1", cwd="/x")
        mgr.user_window_offsets[100] = {"@1": 12345}
        self._live(
            monkeypatch,
            [TmuxWindow("@9", "1 — new title", "/x", window_index="1")],
        )
        await mgr.resolve_stale_ids()
        assert mgr.get_window_for_thread(100, 10) == "@9"
        assert mgr.window_states["@9"].session_id == "sess-1"
        assert mgr.user_window_offsets[100]["@9"] == 12345
        assert "@1" not in mgr.window_states

    @pytest.mark.asyncio
    async def test_ghost_group_chat_ids_drained(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        mgr.bind_thread(100, 10, "@1", window_name="1 — live")
        mgr.group_chat_ids["100:10"] = -100999  # live topic — kept
        mgr.group_chat_ids["100:777"] = -100999  # no binding — ghost
        self._live(
            monkeypatch,
            [TmuxWindow("@1", "1 — live", "/x", window_index="1")],
        )
        await mgr.resolve_stale_ids()
        assert "100:10" in mgr.group_chat_ids
        assert "100:777" not in mgr.group_chat_ids

    @pytest.mark.asyncio
    async def test_live_binding_kept(self, mgr: SessionManager, monkeypatch) -> None:
        mgr.bind_thread(100, 30, "@5", window_name="3 — baz")
        self._live(
            monkeypatch,
            [TmuxWindow("@5", "3 — baz", "/x", window_index="3")],
        )
        await mgr.resolve_stale_ids()
        assert mgr.get_window_for_thread(100, 30) == "@5"

    @pytest.mark.asyncio
    async def test_no_identity_evidence_parks_after_server_restart(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        """After a REAL server restart ids are recycled: a live id with NO
        recorded display/name evidence must fail closed — park the binding,
        never route the topic's keystrokes into a stranger's terminal."""
        mgr.tmux_server_start = "srv-old"
        mgr.bind_thread(100, 20, "@2", window_name="")
        self._live(
            monkeypatch,
            [TmuxWindow("@2", "eterni", "/e", window_index="5")],
            server_start="srv-new",
        )
        await mgr.resolve_stale_ids()
        # Display fallback is the id itself (id-shaped) → inert marker.
        assert mgr.get_window_for_thread(100, 20) == "gone:@2"

    @pytest.mark.asyncio
    async def test_two_topics_parked_to_same_name_both_kept(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        """Parked values are names, not windows: the 1-topic-1-window dedup
        must not drop the second topic parking to an identical name — that
        would erase its parked identity (and drain its group routing)."""
        mgr.bind_thread(100, 20, "@2", window_name="2 — foo")
        mgr.bind_thread(100, 30, "@3", window_name="2 — foo")
        self._live(monkeypatch, [])
        await mgr.resolve_stale_ids()
        assert mgr.get_window_for_thread(100, 20) == "2 — foo"
        assert mgr.get_window_for_thread(100, 30) == "2 — foo"


class TestAdoptParkedBinding:
    """Direct adoption-guard coverage (the mirror flow is in test_mirror)."""

    def test_exact_name_match_adopts_slotless_parked_value(
        self, mgr: SessionManager
    ) -> None:
        """Parked values without a 'N — ' slot prefix (ancient name-format
        bindings) must still adopt a reopened window with exactly that
        name — slot-only matching left them parked forever."""
        mgr.bind_thread(100, 20, "myproject", window_name="")
        got = mgr.adopt_parked_binding(
            TmuxWindow("@5", "myproject", "/x", window_index="7")
        )
        assert got == (100, 20)
        assert mgr.get_window_for_thread(100, 20) == "@5"

    def test_empty_name_and_index_never_match(self, mgr: SessionManager) -> None:
        """No evidence, no adoption: a window with neither index nor name
        must not vacuum up a parked binding."""
        mgr.bind_thread(100, 20, "myproject", window_name="")
        assert (
            mgr.adopt_parked_binding(TmuxWindow("@5", "", "/x", window_index=""))
            is None
        )
