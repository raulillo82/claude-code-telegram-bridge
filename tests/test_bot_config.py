import json
import subprocess

import pytest

from bridge import bot, sync


def write_config(tmp_path, allowed_user_id, bot_token="dummy"):
    path = tmp_path / "config.json"
    cfg = {
        "allowed_user_id": allowed_user_id,
        "projects_dir": "~/claude",
        "flight_mode_idle_minutes": 30,
    }
    if bot_token is not None:
        cfg["bot_token"] = bot_token
    path.write_text(json.dumps(cfg))
    return path


def test_load_config_accepts_numeric_user_id(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "CONFIG_PATH", write_config(tmp_path, 7626037))
    cfg = bot.load_config()
    assert cfg["allowed_user_id"] == 7626037
    assert isinstance(cfg["allowed_user_id"], int)


def test_load_config_tolerates_quoted_string_user_id(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "CONFIG_PATH", write_config(tmp_path, "7626037"))
    cfg = bot.load_config()
    assert cfg["allowed_user_id"] == 7626037
    assert isinstance(cfg["allowed_user_id"], int)


def test_load_config_prefers_inline_bot_token_over_gpg(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "CONFIG_PATH", write_config(tmp_path, 7626037, bot_token="inline-token"))
    monkeypatch.setattr(bot, "BOT_TOKEN_GPG_PATH", tmp_path / "unused.gpg")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("gpg should not be invoked when config.json has bot_token")

    monkeypatch.setattr(subprocess, "run", fail_if_called)
    cfg = bot.load_config()
    assert cfg["bot_token"] == "inline-token"


def test_load_config_decrypts_bot_token_from_gpg_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "CONFIG_PATH", write_config(tmp_path, 7626037, bot_token=None))
    gpg_path = tmp_path / "telegram-bot-token.gpg"
    gpg_path.write_bytes(b"not-really-encrypted")
    monkeypatch.setattr(bot, "BOT_TOKEN_GPG_PATH", gpg_path)

    def fake_run(cmd, **kwargs):
        assert cmd == ["gpg", "--decrypt", str(gpg_path)]
        return subprocess.CompletedProcess(cmd, 0, stdout="decrypted-token\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cfg = bot.load_config()
    assert cfg["bot_token"] == "decrypted-token"


def test_load_config_raises_when_no_token_available(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "CONFIG_PATH", write_config(tmp_path, 7626037, bot_token=None))
    monkeypatch.setattr(bot, "BOT_TOKEN_GPG_PATH", tmp_path / "missing.gpg")
    with pytest.raises(RuntimeError):
        bot.load_config()


def test_load_config_sync_disabled_when_absent(tmp_path, monkeypatch):
    """A config predating the sync feature (or one that just never opted
    in) must leave it fully off -- no forced changes to existing configs."""
    monkeypatch.setattr(bot, "CONFIG_PATH", write_config(tmp_path, 7626037))
    cfg = bot.load_config()
    assert cfg["sync_host"] is None
    assert sync.sync_enabled(cfg) is False


def test_load_config_waits_for_gpg_key_instead_of_failing(tmp_path, monkeypatch):
    """A locked GPG key is the normal state after a reboot: the bridge must
    keep retrying (with backoff) and come up on its own once the key is
    unlocked, not exit into systemd's restart loop."""
    monkeypatch.setattr(bot, "CONFIG_PATH", write_config(tmp_path, 7626037, bot_token=None))
    gpg_path = tmp_path / "telegram-bot-token.gpg"
    gpg_path.write_bytes(b"not-really-encrypted")
    monkeypatch.setattr(bot, "BOT_TOKEN_GPG_PATH", gpg_path)

    outcomes = [
        subprocess.CompletedProcess([], 2, stdout="", stderr="gpg: decryption failed: No secret key\n"),
        subprocess.CompletedProcess([], 2, stdout="", stderr=""),
        subprocess.CompletedProcess([], 2, stdout="", stderr="gpg: Operation cancelled\n"),
        subprocess.CompletedProcess([], 0, stdout="decrypted-token\n", stderr=""),
    ]
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kwargs: outcomes.pop(0))
    sleeps = []
    monkeypatch.setattr(bot.time, "sleep", sleeps.append)

    cfg = bot.load_config()

    assert cfg["bot_token"] == "decrypted-token"
    assert sleeps == [5, 10, 20]


def test_token_retry_backoff_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "BOT_TOKEN_GPG_PATH", tmp_path / "token.gpg")
    (tmp_path / "token.gpg").write_bytes(b"x")
    failures = [subprocess.CompletedProcess([], 2, stdout="", stderr="locked")] * 10
    outcomes = failures + [subprocess.CompletedProcess([], 0, stdout="t", stderr="")]
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kwargs: outcomes.pop(0))
    sleeps = []
    monkeypatch.setattr(bot.time, "sleep", sleeps.append)

    assert bot._load_bot_token({}) == "t"
    assert max(sleeps) == bot.TOKEN_RETRY_MAX_SECONDS
    assert sleeps[-3:] == [bot.TOKEN_RETRY_MAX_SECONDS] * 3


def test_load_bot_token_rejects_empty_decryption(tmp_path, monkeypatch):
    """gpg exiting 0 with nothing on stdout is a broken credential file,
    not a locked key -- retrying forever would hide it."""
    monkeypatch.setattr(bot, "BOT_TOKEN_GPG_PATH", tmp_path / "token.gpg")
    (tmp_path / "token.gpg").write_bytes(b"x")
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout="\n", stderr="")
    )
    with pytest.raises(RuntimeError):
        bot._load_bot_token({})
