from unittest.mock import patch

from bridge.claude_runner import clean_env, encode_project_path, CLAUDE_ENV_VARS_TO_STRIP


def test_encode_project_path_matches_observed_claude_code_convention():
    # Verified against a real ~/.claude/projects/ directory name: Claude Code
    # replaces every non-alphanumeric character (not just '/') with '-',
    # so underscores in a project name get collapsed too.
    with patch("os.path.realpath", return_value="/home/raul/claude/flight_vpn_tunnel"):
        assert (
            encode_project_path("/home/raul/claude/flight_vpn_tunnel")
            == "-home-raul-claude-flight-vpn-tunnel"
        )


def test_clean_env_strips_claude_session_vars(monkeypatch):
    for var in CLAUDE_ENV_VARS_TO_STRIP:
        monkeypatch.setenv(var, "some-value")
    monkeypatch.setenv("UNRELATED_VAR", "keep-me")

    env = clean_env()

    for var in CLAUDE_ENV_VARS_TO_STRIP:
        assert var not in env
    assert env.get("UNRELATED_VAR") == "keep-me"
