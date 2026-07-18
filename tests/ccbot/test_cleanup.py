"""Cleanup-ordering regression (2026-07-17 review, finding #3): the
group_chat_ids routing entry must survive unbind_thread — clear_topic_state's
supergroup message deletes resolve the chat through it — and be pruned only
at the END of clear_topic_state."""

from unittest.mock import patch

import pytest

from ccbot.handlers.cleanup import clear_topic_state
from ccbot.session import SessionManager


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    return SessionManager()


def test_unbind_keeps_group_routing(mgr: SessionManager) -> None:
    mgr.bind_thread(100, 42, "@1", window_name="proj")
    mgr.set_group_chat_id(100, 42, -100999)

    mgr.unbind_thread(100, 42)

    # Still resolvable: cleanup after unbind needs the supergroup chat id.
    assert mgr.resolve_chat_id(100, 42) == -100999


@pytest.mark.asyncio
async def test_clear_topic_state_prunes_group_routing_last(
    mgr: SessionManager,
) -> None:
    mgr.set_group_chat_id(100, 42, -100999)
    seen_during_cleanup: list[int] = []

    async def fake_clear_interactive_msg(user_id, bot, thread_id):
        # Runs INSIDE clear_topic_state — routing must still be intact here.
        seen_during_cleanup.append(mgr.resolve_chat_id(user_id, thread_id))

    with (
        patch("ccbot.handlers.cleanup.session_manager", mgr),
        patch(
            "ccbot.handlers.cleanup.clear_interactive_msg",
            fake_clear_interactive_msg,
        ),
    ):
        await clear_topic_state(100, 42, None)

    assert seen_during_cleanup == [-100999]
    assert "100:42" not in mgr.group_chat_ids  # pruned at the end
