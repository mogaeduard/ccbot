"""Tests for SessionManager pure dict operations."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import ccbot.session as session_module
from ccbot.session import SessionManager


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
