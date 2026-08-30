# claude-code-telegram-bridge

A Telegram bot that relays messages to headless [Claude Code](https://github.com/anthropics/claude-code)
sessions, for situations where you can't reach Anthropic's servers directly
but Telegram traffic gets through — the canonical case being an airline's
free "messaging-only" Wi-Fi tier (WhatsApp/iMessage/Telegram/Messenger
allowed, general internet blocked).

It is **not** a replacement for Claude Code's own Remote Control feature —
use that whenever you have real internet access. This bridge exists purely
as a fallback for the narrow case where you don't.

## How it works

- Runs on the same machine as your Claude Code projects, using long
  polling to fetch updates from Telegram. No inbound port needs to be
  opened.
- Only responds to a single, hardcoded Telegram user ID.
- Each message is relayed as a one-off `claude -p "<message>" --continue`
  call into the target project's directory — no long-running process per
  project is needed (unlike the interactive Remote Control flow, which
  keeps a persistent `claude` process alive).
- Before relaying, it checks whether a `claude` process (interactive or
  not) already has that project directory as its cwd, and refuses to send
  if so — to avoid two processes writing to the same working tree at once.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) on Telegram and
   copy the token it gives you.
2. Find your own numeric Telegram user ID (e.g. via [@Getmyid_bot](https://t.me/Getmyid_bot);
   [@userinfobot](https://t.me/userinfobot) is the more commonly recommended
   one but didn't respond in testing — try it first if you like, fall back to
   @Getmyid_bot if it stays silent). This is a plain integer Telegram assigns
   to your account internally — it
   is **not** your `@username`, and it must **not** be quoted as a string in
   `config.json` (`123456789`, not `"123456789"` — though the loader tolerates
   a quoted string too, just don't rely on it).
3. Copy `config.example.json` to `config.json` and fill in `bot_token`,
   `allowed_user_id`, and `projects_dir` (the parent directory holding your
   Claude Code project folders). `config.json` is gitignored — never commit
   it.
4. `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
5. Run it directly for a quick test: `.venv/bin/python run.py`. For actual
   use, install it as a systemd user service instead (see below) — a
   foreground process tied to a terminal (or to someone else's shell
   session) dies the moment that session closes.

### Running it as a systemd service (recommended)

```
mkdir -p ~/.config/systemd/user
ln -s ~/claude/claude-code-telegram-bridge/deploy/claude-code-telegram-bridge.service \
      ~/.config/systemd/user/claude-code-telegram-bridge.service
systemctl --user daemon-reload
systemctl --user enable --now claude-code-telegram-bridge.service
loginctl enable-linger "$USER"   # keep it running with no active login session
```

The unit assumes the repo lives at `~/claude/claude-code-telegram-bridge`;
edit `deploy/claude-code-telegram-bridge.service` first if yours doesn't.
Check on it with `systemctl --user status claude-code-telegram-bridge` or
`journalctl --user -u claude-code-telegram-bridge -f`.

## Usage

- `/projects` — pick the active project from a button list.
- Send text (optionally with a photo/document attached) to talk to the
  active project.
- Prefix a single message with `@project-name` to send just that message
  to a different project without changing the active one.
- `/mode` — show the current permission mode.
- `/mode normal` / `/mode flight` — switch permission mode (see below).

## Permission modes

Headless mode has no interactive prompt to fall back on, so every tool
call must be pre-approved or pre-denied.

- **normal** (default): a curated allowlist covering everyday work —
  reading, editing, and non-destructive git (`status`, `diff`, `log`,
  `add`, `commit`, `push`, `pull`, `branch`, `checkout`, `stash`). Clearly
  destructive or irreversible commands are explicitly denied (`rm -rf`,
  `git push --force`, `git reset --hard`, `sudo`, `chmod`, `curl`,
  `wget`, ...). See `bridge/permissions.py` for the exact lists.
- **flight**: `--dangerously-skip-permissions`. Meant to be switched on
  deliberately when normal mode blocks something legitimate, not left on.
  It reverts to normal automatically after `flight_mode_idle_minutes` of
  inactivity (configurable in `config.json`).

When normal mode blocks a tool call, the bot makes a best-effort attempt
to detect that in Claude's output and suggests switching to flight mode —
this detection is not guaranteed to catch every case.

## Security notes

- Long polling means the bot only makes outbound connections; nothing
  listens for inbound traffic.
- Only the configured `allowed_user_id` gets a response; everyone else is
  silently ignored.
- The bot token and `config.json` should be treated like any other
  credential — never commit them, and restrict file permissions on the
  host.
- Even with the `allowed_user_id` check, whoever controls that Telegram
  account can act on your projects while in flight mode. Telegram's own
  Two-Step Verification is the relevant defense on that side.

## Limitations

- No conversation state is persisted across a bot restart (active
  project / mode reset).
- Attachment support covers photos and documents only.
- The "detect a permission block and suggest flight mode" behavior is
  best-effort text matching on Claude's output, not a structured signal.
