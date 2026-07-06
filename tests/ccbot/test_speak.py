"""Tests for /speak — picking the last answer, summarizing long ones for
TTS (should_summarize / extractive_fallback / _is_valid_summary /
_summarize_via_claude_cli / summarize_for_speech), and the end-to-end
command flow."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ccbot import bot
from ccbot.bot import last_assistant_text


def _m(role: str, ctype: str, text: str) -> dict:
    return {"role": role, "content_type": ctype, "text": text, "timestamp": ""}


class TestDetectLanguageRoEn:
    def test_plain_english_is_en(self) -> None:
        assert bot.detect_language_ro_en("This is a short English answer.") == "en"

    def test_diacritics_are_ro(self) -> None:
        assert bot.detect_language_ro_en("Așa funcționează.") == "ro"

    def test_legacy_cedilla_diacritics_are_ro(self) -> None:
        assert bot.detect_language_ro_en("Asa functioneaza cu şi ţ") == "ro"

    def test_stopword_without_diacritics_is_ro(self) -> None:
        assert (
            bot.detect_language_ro_en("Acesta este un raspuns fara diacritice") == "ro"
        )

    def test_stopword_is_whole_word_only(self) -> None:
        # "un" and "o" appear only as substrings here, never as whole words
        assert bot.detect_language_ro_en("Unusual output observation") == "en"

    def test_stopword_case_insensitive(self) -> None:
        assert bot.detect_language_ro_en("NU this is not it") == "ro"

    def test_empty_text_is_en(self) -> None:
        assert bot.detect_language_ro_en("") == "en"


class TestLastAssistantText:
    def test_picks_most_recent_assistant_text(self) -> None:
        msgs = [
            _m("assistant", "text", "old answer"),
            _m("user", "text", "question"),
            _m("assistant", "text", "new answer"),
        ]
        assert last_assistant_text(msgs) == "new answer"

    def test_skips_thinking_and_tool_content(self) -> None:
        msgs = [
            _m("assistant", "text", "real answer"),
            _m("assistant", "thinking", "hmm"),
            _m("assistant", "tool_use", "Bash(...)"),
        ]
        assert last_assistant_text(msgs) == "real answer"

    def test_skips_user_and_blank(self) -> None:
        msgs = [
            _m("user", "text", "hello"),
            _m("assistant", "text", "   "),
        ]
        assert last_assistant_text(msgs) is None

    def test_empty_history(self) -> None:
        assert last_assistant_text([]) is None


class TestShouldSummarize:
    def test_at_threshold_is_false(self) -> None:
        assert bot.should_summarize("x" * bot.SUMMARIZE_THRESHOLD_CHARS) is False

    def test_above_threshold_is_true(self) -> None:
        assert bot.should_summarize("x" * (bot.SUMMARIZE_THRESHOLD_CHARS + 1)) is True

    def test_short_text_is_false(self) -> None:
        assert bot.should_summarize("hi") is False


class TestExtractiveFallback:
    def test_two_sentences_no_truncation(self) -> None:
        text = "First sentence here. Second sentence here."
        assert bot.extractive_fallback(text) == text

    def test_more_than_two_sentences_appends_ellipsis(self) -> None:
        result = bot.extractive_fallback("One. Two. Three.")
        assert result == "One. Two.…"

    def test_single_long_sentence_is_char_capped(self) -> None:
        text = "word " * 100  # no terminal punctuation, > 250 chars
        result = bot.extractive_fallback(text, max_chars=250)
        assert result.endswith("…")
        assert len(result) <= 251

    def test_short_single_sentence_untouched(self) -> None:
        text = "Just one short sentence"
        assert bot.extractive_fallback(text) == text


class TestIsValidSummary:
    def test_empty_is_invalid(self) -> None:
        assert bot._is_valid_summary("") is False

    def test_whitespace_only_is_invalid(self) -> None:
        assert bot._is_valid_summary("   \n  ") is False

    def test_too_long_is_invalid(self) -> None:
        assert bot._is_valid_summary("x" * (bot.SUMMARY_MAX_CHARS + 1)) is False

    def test_at_max_is_valid(self) -> None:
        assert bot._is_valid_summary("x" * bot.SUMMARY_MAX_CHARS) is True

    def test_normal_summary_is_valid(self) -> None:
        assert bot._is_valid_summary("Scurt rezumat, gata.") is True


class TestSummarizeViaClaudeCli:
    @pytest.mark.asyncio
    async def test_success_returns_stdout(self) -> None:
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"Scurt rezumat.\n", b""))
        proc.returncode = 0
        with patch(
            "ccbot.bot.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ) as mock_exec:
            result = await bot._summarize_via_claude_cli("some long answer")

        assert result == "Scurt rezumat."
        args = mock_exec.call_args.args
        assert args[0] == bot.SUMMARY_CLI_BIN
        assert "-p" in args
        assert "--model" in args
        assert bot.SUMMARY_MODEL in args
        assert "--settings" in args
        assert '{"disableAllHooks":true}' in args
        assert "some long answer" in args[-1]

    @pytest.mark.asyncio
    async def test_missing_binary_returns_none(self) -> None:
        with patch(
            "ccbot.bot.asyncio.create_subprocess_exec",
            side_effect=OSError("no such file"),
        ):
            result = await bot._summarize_via_claude_cli("text")
        assert result is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none_and_kills_process(self) -> None:
        proc = MagicMock()
        proc.communicate = AsyncMock(side_effect=TimeoutError())
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        with patch(
            "ccbot.bot.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            result = await bot._summarize_via_claude_cli("text")

        assert result is None
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_nonzero_exit_returns_none(self) -> None:
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b"boom"))
        proc.returncode = 1
        with patch(
            "ccbot.bot.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            result = await bot._summarize_via_claude_cli("text")
        assert result is None


class TestSummarizeForSpeech:
    @pytest.mark.asyncio
    async def test_uses_valid_cli_summary(self) -> None:
        with patch(
            "ccbot.bot._summarize_via_claude_cli",
            new_callable=AsyncMock,
            return_value="  Rezumat bun.  ",
        ):
            result = await bot.summarize_for_speech("long text " * 50)
        assert result == "Rezumat bun."

    @pytest.mark.asyncio
    async def test_falls_back_when_cli_returns_none(self) -> None:
        text = "One. Two. Three. Four."
        with patch(
            "ccbot.bot._summarize_via_claude_cli",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await bot.summarize_for_speech(text)
        assert result == bot.extractive_fallback(text)

    @pytest.mark.asyncio
    async def test_falls_back_when_cli_returns_too_long(self) -> None:
        text = "One. Two. Three. Four."
        with patch(
            "ccbot.bot._summarize_via_claude_cli",
            new_callable=AsyncMock,
            return_value="x" * (bot.SUMMARY_MAX_CHARS + 1),
        ):
            result = await bot.summarize_for_speech(text)
        assert result == bot.extractive_fallback(text)

    @pytest.mark.asyncio
    async def test_falls_back_when_cli_returns_empty(self) -> None:
        text = "One. Two. Three. Four."
        with patch(
            "ccbot.bot._summarize_via_claude_cli",
            new_callable=AsyncMock,
            return_value="   ",
        ):
            result = await bot.summarize_for_speech(text)
        assert result == bot.extractive_fallback(text)


def _make_speak_update(user_id: int = 1) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.message.reply_voice = AsyncMock()
    update.message.chat = MagicMock()
    update.message.chat.send_action = AsyncMock()
    update.message.chat_id = 123
    update.message.message_thread_id = 42
    return update


def _make_context(args: list[str] | None = None) -> MagicMock:
    context = MagicMock()
    context.bot = AsyncMock()
    # Real PTB always provides a list here (possibly empty) — a bare MagicMock
    # would be truthy and wrongly enter the optional-speed-argument branch.
    context.args = args or []
    return context


def _mock_tts_response() -> httpx.Response:
    request = httpx.Request("POST", bot.TTS_SPEAK_URL)
    return httpx.Response(status_code=200, content=b"fake-ogg-bytes", request=request)


class TestSpeakCommandFlow:
    @pytest.mark.asyncio
    async def test_short_answer_spoken_as_is(self) -> None:
        update = _make_speak_update()
        messages = [_m("assistant", "text", "Short answer.")]
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
            ) as mock_exec,
            patch.object(
                httpx.AsyncClient,
                "post",
                new_callable=AsyncMock,
                return_value=_mock_tts_response(),
            ) as mock_post,
        ):
            mock_sm.get_window_for_thread.return_value = "@1"
            mock_sm.get_recent_messages = AsyncMock(return_value=(messages, None))
            await bot.speak_command(update, _make_context())

        mock_exec.assert_not_called()
        sent_text = mock_post.call_args.kwargs["json"]["text"]
        assert sent_text == "Short answer."
        update.message.reply_voice.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_speed_argument_omits_speed_from_payload(self) -> None:
        update = _make_speak_update()
        messages = [_m("assistant", "text", "Short answer.")]
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch.object(
                httpx.AsyncClient,
                "post",
                new_callable=AsyncMock,
                return_value=_mock_tts_response(),
            ) as mock_post,
        ):
            mock_sm.get_window_for_thread.return_value = "@1"
            mock_sm.get_recent_messages = AsyncMock(return_value=(messages, None))
            await bot.speak_command(update, _make_context())

        # The server's TTS_DEFAULT_SPEED must govern when no argument is given.
        assert "speed" not in mock_post.call_args.kwargs["json"]

    @pytest.mark.asyncio
    async def test_speed_argument_is_parsed_clamped_and_sent(self) -> None:
        update = _make_speak_update()
        messages = [_m("assistant", "text", "Short answer.")]
        for raw, expected in (("0.8", 0.8), ("1.5x", 1.5), ("0,7", 0.7), ("9", 2.0), ("0.1", 0.5)):
            with (
                patch("ccbot.bot.is_user_allowed", return_value=True),
                patch("ccbot.bot._get_thread_id", return_value=42),
                patch("ccbot.bot.session_manager") as mock_sm,
                patch.object(
                    httpx.AsyncClient,
                    "post",
                    new_callable=AsyncMock,
                    return_value=_mock_tts_response(),
                ) as mock_post,
            ):
                mock_sm.get_window_for_thread.return_value = "@1"
                mock_sm.get_recent_messages = AsyncMock(return_value=(messages, None))
                await bot.speak_command(update, _make_context([raw]))

            assert mock_post.call_args.kwargs["json"]["speed"] == expected, raw

    @pytest.mark.asyncio
    async def test_invalid_speed_argument_replies_usage_and_skips_tts(self) -> None:
        update = _make_speak_update()
        messages = [_m("assistant", "text", "Short answer.")]
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
            patch.object(
                httpx.AsyncClient,
                "post",
                new_callable=AsyncMock,
                return_value=_mock_tts_response(),
            ) as mock_post,
        ):
            mock_sm.get_window_for_thread.return_value = "@1"
            mock_sm.get_recent_messages = AsyncMock(return_value=(messages, None))
            await bot.speak_command(update, _make_context(["fast"]))

        mock_post.assert_not_called()
        assert "Usage" in mock_reply.call_args.args[1]

    @pytest.mark.asyncio
    async def test_long_answer_uses_cli_summary(self) -> None:
        update = _make_speak_update()
        long_text = "Sentence one is here. " * 30  # > 300 chars
        messages = [_m("assistant", "text", long_text)]
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"Rezumat scurt.", b""))
        proc.returncode = 0
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ) as mock_exec,
            patch.object(
                httpx.AsyncClient,
                "post",
                new_callable=AsyncMock,
                return_value=_mock_tts_response(),
            ) as mock_post,
        ):
            mock_sm.get_window_for_thread.return_value = "@1"
            mock_sm.get_recent_messages = AsyncMock(return_value=(messages, None))
            await bot.speak_command(update, _make_context())

        mock_exec.assert_called_once()
        sent_text = mock_post.call_args.kwargs["json"]["text"]
        assert sent_text == "Rezumat scurt."
        update.message.reply_voice.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_long_answer_falls_back_when_cli_unavailable(self) -> None:
        update = _make_speak_update()
        long_text = (
            "Sentence one is here. Sentence two is here. Sentence three trails off. "
            * 5
        )
        messages = [_m("assistant", "text", long_text)]
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot.asyncio.create_subprocess_exec",
                side_effect=OSError("not found"),
            ) as mock_exec,
            patch.object(
                httpx.AsyncClient,
                "post",
                new_callable=AsyncMock,
                return_value=_mock_tts_response(),
            ) as mock_post,
        ):
            mock_sm.get_window_for_thread.return_value = "@1"
            mock_sm.get_recent_messages = AsyncMock(return_value=(messages, None))
            await bot.speak_command(update, _make_context())

        mock_exec.assert_called_once()
        sent_text = mock_post.call_args.kwargs["json"]["text"]
        assert sent_text == bot.extractive_fallback(long_text)
        update.message.reply_voice.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_text_replies_nothing_to_speak(self) -> None:
        update = _make_speak_update()
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
        ):
            mock_sm.get_window_for_thread.return_value = "@1"
            mock_sm.get_recent_messages = AsyncMock(return_value=([], None))
            await bot.speak_command(update, _make_context())

        reply_text = update.message.reply_text.call_args.args[0]
        assert "Nothing to speak" in reply_text
        update.message.reply_voice.assert_not_awaited()
