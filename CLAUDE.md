# Project notes for Claude Code

> The user prefers to communicate in Spanish. This file (like the rest of
> the repo) is in English for consistency, but reply to the user in Spanish
> regardless of what language the docs/code/commits are written in.

## Deployment

- The bridge runs on a dedicated always-on Raspberry Pi (not on the
  primary laptop, where it originally lived). Reason: the laptop gets
  carried around or shut down ("almost always" in its docking station
  isn't "always"), while the Pi already runs other Claude automations and
  is never expected to go offline. Compute needs are negligible either
  way — the actual work happens via the Anthropic API, the bridge itself
  just relays. (See the user's own notes for which physical host is
  currently which role — deliberately not named here, this repo is
  public.)
- This has no TPM dependency to worry about: the credential scheme (see
  below) is plain GPG + `gpg-agent` passphrase caching, not
  `systemd-creds`/TPM-sealed secrets. That TPM-based approach was tried
  and abandoned already (see the global `CLAUDE.md`'s credentials
  section) for an unrelated reason (polkit forcing an interactive
  approval), so a host lacking a TPM is not a blocker for running this
  anywhere.
- Moving it to another host requires that host to have: the user's GPG
  secret key imported (`gpg --list-secret-keys`), `allow-preset-passphrase`
  in its `~/.gnupg/gpg-agent.conf` (plus the same
  `default-cache-ttl`/`max-cache-ttl` as the previous host's, 28800/86400
  — a fresh `gpg-agent.conf` won't have these), and the `gpg-unlock` shell
  function from `~/.alias` copied over.
- The systemd unit sets `Environment=PATH=...` explicitly including
  `%h/.local/bin` — a systemd `--user` service's default PATH does not
  include it, and that's exactly where `claude` is typically installed.
  Without this, the service starts and polls fine (so `systemctl status`
  looks healthy) but every actual message fails with `FileNotFoundError:
  'claude'` the moment `run_claude` tries to spawn it — this went
  unnoticed past an initial "does the service start" check during a host
  migration, only surfacing on the first real end-to-end message test.

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
- **`gpg-unlock` must preset both the signing key and the encryption
  subkey.** Decrypting this token uses the `ssb [E]` encryption subkey,
  which has its own keygrip and its own cache entry in `gpg-agent` —
  separate from the `sec [SC]` master key that `gpg-unlock` was originally
  written to preset (that one's only needed for signing commits). A
  version of `gpg-unlock` that only presets the master key will fail to
  decrypt this file with "Operación cancelada" the first time nothing else
  has incidentally warmed the subkey's cache entry. Both keygrips need
  `gpg --with-keygrip -K` to identify if the key is ever rotated.
- The `httpx` logger is deliberately lowered to `WARNING` in `bridge/bot.py`
  — at `INFO` it logs the full URL of every request to Telegram's API,
  which includes the token in plaintext (`.../bot<TOKEN>/...`). Don't raise
  that level again without first replacing the logging with something that
  doesn't dump the URL.
- If the token is rotated (BotFather → "Revoke current token"), the new
  value should never pass through a Claude session in plaintext — encrypt
  it directly from `systemd-ask-password` without showing it to the
  assistant, and verify the service starts instead of asking it to confirm
  the value. The current token was rotated this way during the move to
  the rpi4 (BotFather shown it only on the phone, piped straight into gpg
  over an SSH session with input echo disabled) — it was never seen by a
  Claude session.
