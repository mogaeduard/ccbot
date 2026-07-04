---
name: ccbot-telegram-craft
description: Telegram Bot API craft as practiced in ccbot — MarkdownV2-only via the safe_reply/safe_edit/safe_send helpers with plain-text fallback (and WHY: the HTML-converter era was fully reverted), the 4096 send-split and 3800 merge budget, forum-topic API quirks (create/edit/delete semantics, 1-128 char names, no read-back of topic state), callback data under 64 bytes, inline keyboards over reply keyboards, edit-in-place preference, answer_callback_query for instant feedback, and AIORateLimiter tuning. Load this when: sending/formatting any Telegram message; entity-parse errors ("can't parse entities"); building buttons/callbacks; creating/renaming/deleting forum topics; hitting rate limits; or choosing between edit and new-message. Keywords: MarkdownV2, safe_send, escape, entities, 4096, forum topic, createForumTopic, editForumTopic, callback data 64, inline keyboard, AIORateLimiter, flood control. Does NOT cover: the queue/merge pipeline order (ccbot-message-flow-debugging + .claude/rules/message-handling.md), topic↔window binding (ccbot-architecture-contract), or voice messages (ccbot-voice-stack).
---

# ccbot-telegram-craft

**Use when** touching the Telegram surface. **Do NOT use when** the issue is upstream in the queue (→ ccbot-message-flow-debugging).

## Formatting law: MarkdownV2 only, through the safe_* helpers

ALL sends go through `telegram_sender.py`'s `safe_reply`/`safe_edit`/`safe_send` — telegramify-markdown conversion with a **plain-text fallback** when entity parsing fails. Never call raw `bot.send_message(parse_mode=...)` directly. WHY this is law: upstream tried chatgpt-md-converter/HTML, lived with it, and FULLY REVERTED to telegramify/MarkdownV2 (the README trio still documents the dead HTML era — trust CLAUDE.md and the code, → ccbot-fork-vs-upstream staleness map). MarkdownV2's escaping rules are unforgiving; the fallback exists because SOME content will always break entities — a plain-text message beats a lost one.

## Size discipline

- **4096** = Telegram's hard message cap — splitting happens at the SEND layer only.
- **3800** = the queue's merge budget (headroom for formatting expansion) — merging happens at the QUEUE layer (tool_use/tool_result pairing breaks a merge chain).
- No truncation anywhere else (architecture invariant 5).

## Forum-topic API quirks (all learned the hard way)

| Quirk | Consequence in ccbot |
|---|---|
| Topic names: 1–128 chars | `TOPIC_NAME_LIMIT = 128` (topic_titles.py:31); AI titles clamp |
| No reliable read-back of topic existence/state | you cannot ask "does topic X still exist?" cheaply — deletion detection went through TWO probe designs: an active TYPING probe (`752bf1f`) which "validated nothing", replaced by an **editForumTopic probe** (`aa64a6d`) whose error response is the actual signal |
| "message thread not found" on send | the authoritative deleted-topic signal — triggers kill-window + unbind (reconciler policy) |
| DELETE vs close | dead windows' topics are DELETED not closed (`c24f6a5`) — closed topics clutter the group |
| One forum group | all topics in CCBOT_MIRROR_CHAT_ID; bot needs Manage Topics admin right (OPEN QUESTION (owner): the full manual Telegram-side config has no single in-repo doc) |

## Interaction craft (upstream conventions, CLAUDE.md)

- Inline keyboards over reply keyboards; `callback_data` **< 64 bytes** (pack ids, not payloads — handlers/callback_data.py owns the packing).
- `edit_message_text` for in-place updates (dashboard, thinking consolidation) over message spam; `answer_callback_query` immediately for perceived responsiveness.
- Rate limiting: `AIORateLimiter(max_retries=5)` (bot.py:3027) — the comment at :2919 notes it has no per-private-chat limiter, hence retries as the correction. Burst-after-quiet = the limiter working (→ ccbot-message-flow-debugging).
- Commands registered in the command menu (`ffa0e8a`); sustained typing indicator during long operations.

## Provenance and maintenance
- Written 2026-07-04 from: telegram_sender.py safe_* (:77 import, helpers), bot.py (:2919 comment, :3027), topic_titles.py:31, commits 752bf1f/aa64a6d/c24f6a5/ffa0e8a, .claude/rules/message-handling.md, doc/telegram-bot-features.md.
- Re-verify: `grep -n 'AIORateLimiter' src/ccbot/bot.py` · `grep -n 'safe_send\|safe_reply' src/ccbot/telegram_sender.py | head -3` · `git log --oneline -2 -- src/ccbot/mirror.py` (probe design current?).
