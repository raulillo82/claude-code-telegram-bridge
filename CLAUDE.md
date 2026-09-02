# Project notes for Claude Code

> The user prefers to communicate in Spanish. This file (like the rest of
> the repo) is in English for consistency, but reply to the user in Spanish
> regardless of what language the docs/code/commits are written in.

## Credentials

- Telegram's `bot_token` is resolved in `bridge/bot.py::_load_bot_token`: if
  `config.json` provides `bot_token`, that value wins; otherwise it's
  decrypted from `~/.credentials/telegram-bot-token.gpg` (the global
  `CLAUDE.md`'s centralized credentials scheme). That encrypted file must
  stay in mode `600` — `gpg --encrypt -o` recreates it with the default
  umask on every overwrite, so after rotating the token `chmod 600` needs
  to be reapplied by hand.
- Decrypting it on startup requires the user's GPG key to be unlocked in
  `gpg-agent` at that moment (see the "Firma de commits"/`gpg-unlock`
  section of the global `CLAUDE.md`). The systemd service uses
  `Restart=always`: if the process dies after the key's cache expires (8h
  after `gpg-unlock`), it will keep failing to start until someone unlocks
  it again.
- The `httpx` logger is deliberately lowered to `WARNING` in `bridge/bot.py`
  — at `INFO` it logs the full URL of every request to Telegram's API,
  which includes the token in plaintext (`.../bot<TOKEN>/...`). Don't raise
  that level again without first replacing the logging with something that
  doesn't dump the URL.
- If the token is rotated (BotFather → "Revoke current token"), the new
  value should never pass through a Claude session in plaintext — encrypt
  it directly from `systemd-ask-password` without showing it to the
  assistant, and verify the service starts instead of asking it to confirm
  the value.
