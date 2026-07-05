"""Callback data constants for Telegram inline keyboards.

Defines all CB_* prefixes used for routing callback queries in the bot.
Each prefix identifies a specific action or navigation target.

Constants:
  - CB_HISTORY_*: History pagination
  - CB_DIR_*: Directory browser navigation
  - CB_WIN_*: Window picker (bind existing unbound window)
  - CB_SCREENSHOT_*: Screenshot refresh
  - CB_TERM_REFRESH: /term text view refresh
  - CB_ASK_*: Interactive UI navigation (arrows, enter, esc)
  - CB_KEYS_PREFIX: Screenshot control keys (kb:<key_id>:<window>)
  - CB_VOICE_*: Voice transcript confirm-first (Send/Cancel)
  - CB_KILLALL_*: /killall panic button confirm/cancel
"""

# History pagination
CB_HISTORY_PREV = "hp:"  # history page older
CB_HISTORY_NEXT = "hn:"  # history page newer

# Directory browser
CB_DIR_SELECT = "db:sel:"
CB_DIR_UP = "db:up"
CB_DIR_CONFIRM = "db:confirm"
CB_DIR_CANCEL = "db:cancel"
CB_DIR_PAGE = "db:page:"

# Window picker (bind existing unbound window)
CB_WIN_BIND = "wb:sel:"  # wb:sel:<index>
CB_WIN_NEW = "wb:new"  # proceed to directory browser
CB_WIN_CANCEL = "wb:cancel"

# Screenshot
CB_SCREENSHOT_REFRESH = "ss:ref:"

# /term text view
CB_TERM_REFRESH = "tm:ref:"

# Interactive UI (aq: prefix kept for backward compatibility)
CB_ASK_NUM = "aq:num:"  # aq:num:<digit>:<window> — press option number directly
CB_ASK_UP = "aq:up:"  # aq:up:<window>
CB_ASK_DOWN = "aq:down:"  # aq:down:<window>
CB_ASK_LEFT = "aq:left:"  # aq:left:<window>
CB_ASK_RIGHT = "aq:right:"  # aq:right:<window>
CB_ASK_ESC = "aq:esc:"  # aq:esc:<window>
CB_ASK_ENTER = "aq:enter:"  # aq:enter:<window>
CB_ASK_SPACE = "aq:spc:"  # aq:spc:<window>
CB_ASK_TAB = "aq:tab:"  # aq:tab:<window>
CB_ASK_REFRESH = "aq:ref:"  # aq:ref:<window>
CB_ASK_SUBMIT = (
    "aq:submit:"  # aq:submit:<window> — multiSelect: jump to Submit tab + confirm
)

# Session picker (resume existing session)
CB_SESSION_SELECT = "rs:sel:"  # rs:sel:<index>
CB_SESSION_NEW = "rs:new"  # start a new session
CB_SESSION_CANCEL = "rs:cancel"  # cancel
CB_SESSION_ALL = "rs:all"  # expand to ALL sessions (default view caps at 10)

# /effort level picker
CB_EFFORT_SET = "ef:set:"  # ef:set:<level>
CB_EFFORT_CANCEL = "ef:cancel"

# Screenshot control keys
CB_KEYS_PREFIX = "kb:"  # kb:<key_id>:<window>

# Voice confirm-first (transcript preview before injection)
CB_VOICE_SEND = "vc:send"
CB_VOICE_CANCEL = "vc:cancel"

# /killall panic button (kill every tmux window) — confirm/cancel
CB_KILLALL_CONFIRM = "ka:confirm"
CB_KILLALL_CANCEL = "ka:cancel"
