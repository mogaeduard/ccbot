"""Tests for interactive_ui — handle_interactive_ui and keyboard layout."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

from ccbot.handlers.interactive_ui import (
    _build_interactive_keyboard,
    clear_interactive_msg,
    handle_interactive_ui,
    pop_pending_inline_edit,
    set_interactive_mode,
    set_pending_inline_edit,
)
from ccbot.handlers.callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_NUM,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_SUBMIT,
    CB_ASK_TAB,
    CB_ASK_UP,
)


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 999
    bot.send_message.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_interactive_state():
    """Ensure interactive state is clean before and after each test."""
    from ccbot.handlers.interactive_ui import (
        _interactive_content,
        _interactive_mode,
        _interactive_msgs,
    )

    _interactive_mode.clear()
    _interactive_msgs.clear()
    _interactive_content.clear()
    yield
    _interactive_mode.clear()
    _interactive_msgs.clear()
    _interactive_content.clear()


@pytest.mark.usefixtures("_clear_interactive_state")
class TestHandleInteractiveUI:
    @pytest.mark.asyncio
    async def test_handle_settings_ui_sends_keyboard(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """handle_interactive_ui captures Settings pane, sends message with keyboard."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.interactive_ui.session_manager") as mock_sm,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_sm.resolve_chat_id.return_value = 100

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is True
        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args
        assert call_kwargs.kwargs["chat_id"] == 100
        assert call_kwargs.kwargs["message_thread_id"] == 42
        assert call_kwargs.kwargs["reply_markup"] is not None

    @pytest.mark.asyncio
    async def test_handle_no_ui_returns_false(self, mock_bot: AsyncMock):
        """Returns False when no interactive UI detected in pane."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.interactive_ui.session_manager"),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value="$ echo hello\nhello\n$\n")

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is False
        mock_bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_deleted_thread_triggers_cleanup_not_error_log(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """A deleted-topic send failure hands off to cleanup_deleted_thread
        (change 2) instead of just logging — this is the one send call site
        that bypasses message_sender.py's safe_* helpers (plain-text UI)."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        mock_bot.send_message = AsyncMock(
            side_effect=BadRequest("Bad Request: message thread not found")
        )

        with (
            patch("ccbot.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.interactive_ui.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.interactive_ui.cleanup_deleted_thread",
                new_callable=AsyncMock,
            ) as mock_cleanup,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_sm.resolve_chat_id.return_value = -100999

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is False
        mock_cleanup.assert_awaited_once_with(-100999, 42)


class TestKeyboardLayoutForSettings:
    def test_settings_keyboard_includes_all_nav_keys(self):
        """Settings keyboard includes Tab, arrows (not vertical_only), Space, Esc, Enter."""
        keyboard = _build_interactive_keyboard("@5", ui_name="Settings")
        # Flatten all callback data values
        all_cb_data = [
            btn.callback_data for row in keyboard.inline_keyboard for btn in row
        ]
        assert any(CB_ASK_TAB in d for d in all_cb_data if d)
        assert any(CB_ASK_SPACE in d for d in all_cb_data if d)
        assert any(CB_ASK_UP in d for d in all_cb_data if d)
        assert any(CB_ASK_DOWN in d for d in all_cb_data if d)
        assert any(CB_ASK_LEFT in d for d in all_cb_data if d)
        assert any(CB_ASK_RIGHT in d for d in all_cb_data if d)
        assert any(CB_ASK_ESC in d for d in all_cb_data if d)
        assert any(CB_ASK_ENTER in d for d in all_cb_data if d)


class TestKeyboardLayoutForMultiSelect:
    """multiSelect options carry a states dict — checkbox prefixes + a
    Submit row on top of the plain per-option buttons."""

    _OPTIONS = [
        (1, "Apple"),
        (2, "Banana"),
        (3, "Cherry"),
        (4, "Type something"),
    ]
    _STATES = {1: False, 2: False, 3: True, 4: False}

    def test_checkbox_prefixes_reflect_state(self):
        keyboard = _build_interactive_keyboard(
            "@5", ui_name="AskUserQuestion", options=self._OPTIONS, states=self._STATES
        )
        labels = [
            btn.text
            for row in keyboard.inline_keyboard
            for btn in row
            if btn.callback_data and btn.callback_data.startswith(CB_ASK_NUM)
        ]
        assert labels[0].startswith("☐ ")  # Apple, unchecked
        assert labels[2].startswith("☑ ")  # Cherry, checked

    def test_free_text_row_combines_checkbox_and_pencil(self):
        """Free-text row shows the checkbox prefix before the existing ✏️
        marker: '☐ ✏️ 4. Type something'."""
        keyboard = _build_interactive_keyboard(
            "@5", ui_name="AskUserQuestion", options=self._OPTIONS, states=self._STATES
        )
        labels = [
            btn.text
            for row in keyboard.inline_keyboard
            for btn in row
            if btn.callback_data and btn.callback_data.startswith(CB_ASK_NUM)
        ]
        assert labels[3].startswith("☐ ✏️")

    def test_submit_row_present_when_states_nonempty(self):
        keyboard = _build_interactive_keyboard(
            "@5", ui_name="AskUserQuestion", options=self._OPTIONS, states=self._STATES
        )
        all_cb_data = [
            btn.callback_data for row in keyboard.inline_keyboard for btn in row
        ]
        assert any(d and d.startswith(CB_ASK_SUBMIT) for d in all_cb_data)

    def test_submit_row_absent_for_single_select(self):
        """No states dict at all (single-select) → no Submit row."""
        keyboard = _build_interactive_keyboard(
            "@5", ui_name="AskUserQuestion", options=self._OPTIONS
        )
        all_cb_data = [
            btn.callback_data for row in keyboard.inline_keyboard for btn in row
        ]
        assert not any(d and d.startswith(CB_ASK_SUBMIT) for d in all_cb_data)

    def test_submit_row_absent_for_empty_states(self):
        """Empty (falsy) states dict behaves the same as None."""
        keyboard = _build_interactive_keyboard(
            "@5", ui_name="AskUserQuestion", options=self._OPTIONS, states={}
        )
        all_cb_data = [
            btn.callback_data for row in keyboard.inline_keyboard for btn in row
        ]
        assert not any(d and d.startswith(CB_ASK_SUBMIT) for d in all_cb_data)

    def test_callback_data_under_64_bytes(self):
        """Every button's callback_data — including CB_ASK_SUBMIT with a
        long window_id — stays under Telegram's 64 byte limit."""
        keyboard = _build_interactive_keyboard(
            "@some-long-window-identifier-12345",
            ui_name="AskUserQuestion",
            options=self._OPTIONS,
            states=self._STATES,
        )
        for row in keyboard.inline_keyboard:
            for btn in row:
                assert btn.callback_data is not None
                assert len(btn.callback_data.encode()) < 64


@pytest.mark.usefixtures("_clear_interactive_state")
class TestPendingInlineEdit:
    """set_pending_inline_edit / pop_pending_inline_edit — the one-shot
    flag that routes a user's next plain message into a focused multiSelect
    free-text row instead of Claude's prompt."""

    def test_pop_without_set_returns_false(self):
        assert pop_pending_inline_edit(1, 42) is False

    def test_set_then_pop_returns_true_once(self):
        set_pending_inline_edit(1, 42)
        assert pop_pending_inline_edit(1, 42) is True
        assert pop_pending_inline_edit(1, 42) is False  # consumed, one-shot

    def test_scoped_by_user_and_thread(self):
        set_pending_inline_edit(1, 42)
        assert pop_pending_inline_edit(2, 42) is False  # different user
        assert pop_pending_inline_edit(1, 99) is False  # different thread
        assert pop_pending_inline_edit(1, 42) is True  # original still armed

    def test_none_thread_id_normalizes_like_other_state_dicts(self):
        set_pending_inline_edit(1, None)
        assert pop_pending_inline_edit(1, 0) is True

    @pytest.mark.asyncio
    async def test_cleared_by_clear_interactive_msg(self):
        set_interactive_mode(1, "@5", 42)
        set_pending_inline_edit(1, 42)
        await clear_interactive_msg(1, bot=None, thread_id=42)
        assert pop_pending_inline_edit(1, 42) is False
