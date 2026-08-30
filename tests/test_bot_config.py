import json

from bridge import bot


def write_config(tmp_path, allowed_user_id):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "bot_token": "dummy",
                "allowed_user_id": allowed_user_id,
                "projects_dir": "~/claude",
                "flight_mode_idle_minutes": 30,
            }
        )
    )
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
