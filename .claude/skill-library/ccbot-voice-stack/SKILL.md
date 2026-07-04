---
name: ccbot-voice-stack
description: ccbot's voice pipeline both directions — /speak TTS-out (language detect → Haiku CLI summarize when >300 chars → OmniVoice cloned-voice server on :8838, 800-char cap) and voice-in transcription (Telegram voice note → OpenAI-compatible endpoint, locally the MLX Whisper server on :8837, confirm-first preview with Send/Cancel + 5-min TTL), the language-purity rules, voice-reference management, performance ceilings (~8s/sentence diffusion on MPS with the OOM workaround), and the REJECTED alternatives (XTTS: Spanish-accented Romanian; VoxCPM2: no Romanian). Load this when: /speak fails, sounds wrong, speaks the wrong language, or is slow; voice notes don't transcribe or inject; touching the summarizer, TTS client, or transcription path; changing the cloned voice reference; or evaluating a new TTS/STT engine. Keywords: /speak, TTS, OmniVoice, 8838, whisper, 8837, transcription, voice note, confirm-first, summarize, Haiku, cloned voice, language, OOM, MPS. Does NOT cover: the sibling LaunchAgents' lifecycle (ccbot-run-and-operate), config vars (ccbot-config-and-flags), or Telegram audio-message mechanics (ccbot-telegram-craft).
---

# ccbot-voice-stack

**Use when** voice in either direction misbehaves or evolves. **Do NOT use when** the sibling service is simply down (→ ccbot-run-and-operate triage first: probe :8838/:8837).

## /speak — TTS out (bot.py voice section, ~:707+)

Pipeline: take the topic's last answer → detect language (the pass-through improved in `ffa0e8a`) → if `should_summarize()` (long answers only, :761 — short ones go verbatim) summarize via the **Haiku CLI** (`SUMMARY_MODEL = "claude-haiku-4-5-20251001"`, :718; `_summarize_via_claude_cli`, :787 — shells out to the claude CLI, so it bills the account the CLI is logged into; OPEN QUESTION (owner): cost policy + what happens during account-switcher swaps) → POST to `TTS_SPEAK_URL = http://127.0.0.1:8838/speak` capped at `TTS_MAX_CHARS = 800` (:707–708) → OmniVoice renders in the cloned voice → voice note to the topic.

- **Language purity is enforced at two layers** (summarizer told the target language :740, and the TTS server told which language to speak) — Romanian answers must not come out English-accented or mixed.
- **Performance ceiling:** OmniVoice diffusion ≈8s/sentence on MPS, with the OOM workaround (empty_cache + HIGH_WATERMARK_RATIO=0) — long texts are SUPPOSED to be summarized first; raising TTS_MAX_CHARS without measuring is how /speak becomes unusable.
- **Voice reference:** the cloned voice comes from a reference clip on the TTS server side; the natural-speech cut beat the flat scripted one (similarity 0.881→0.912 RO) — when re-cloning, prefer natural conversational source audio.

## Voice in — transcription + confirm-first (`e12a16f`)

Voice note → transcribe via the OpenAI-compatible endpoint (`OPENAI_API_KEY`/`OPENAI_BASE_URL` — locally the MLX Whisper server on :8837; verify the live base-url value before assuming which backend is active) → **transcript PREVIEW with Send/Cancel buttons** — nothing injects into a terminal without confirmation; pending confirmations expire after 5 minutes (TTL from the `7712594` safety batch). This confirm-first gate is a safety feature, not friction — don't remove it.

## The graveyard (do not re-audition without new evidence)

| Engine | Verdict |
|---|---|
| XTTS | REJECTED — faster, but Spanish-accented Romanian; deleted (1.7GB reclaimed) |
| VoxCPM2 | REJECTED — no Romanian support |
| OmniVoice (k2-fsa) | CURRENT — native RO, cloned voice, the MPS ceiling above |

Evaluating a new engine = the bake-off pattern (`~/.claude/playbooks/ml-data-pipelines.md`): same test sentences, Romanian-native check, similarity + latency numbers, decision recorded.

## Triage

1. /speak silent → `curl -m 2 -s localhost:8838/ || echo down` (sibling LaunchAgent), then logs.
2. Wrong language → check the detect step's output in logs, then the two enforcement layers.
3. Slow → is the text >800 chars pre-cap? did summarization run? MPS memory pressure?
4. Voice-in dead → probe :8837; check OPENAI_BASE_URL actually points where you think.

## Provenance and maintenance
- Written 2026-07-04 from: bot.py (:707–708, :718, :740, :761, :787), commits e12a16f/20d810c/c808b5c/ffa0e8a/7712594, the TTS bake-off history (memory), config.py:109–110.
- Re-verify: `grep -n 'SUMMARY_MODEL\|TTS_SPEAK_URL' src/ccbot/bot.py` · `curl -m 2 -s localhost:8838/ >/dev/null && echo tts-up` · `grep OPENAI_BASE_URL ~/.ccbot/.env | cut -d= -f2` (value, this host).
