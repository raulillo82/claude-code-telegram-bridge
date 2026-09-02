import json
import subprocess

import pytest

from bridge import bot


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
