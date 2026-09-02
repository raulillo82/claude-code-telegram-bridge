import asyncio
from unittest.mock import patch

from bridge import claude_runner
from bridge.claude_runner import clean_env, encode_project_path, run_claude, CLAUDE_ENV_VARS_TO_STRIP


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


class _FakeProc:
    def __init__(self, returncode=0, stdout=b'{"result": "ok"}', stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


def test_run_claude_passes_continue_when_history_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_runner, "has_prior_history", lambda project_dir: True)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(run_claude(str(tmp_path), "hi", []))
    assert "--continue" in captured["args"]


def test_run_claude_force_new_session_skips_continue_even_with_history(tmp_path, monkeypatch):
    # The "Start new session" override must never resume prior history --
    # not the local kind, not (especially) one just pulled in from another
    # host -- regardless of what has_prior_history would otherwise say.
    monkeypatch.setattr(claude_runner, "has_prior_history", lambda project_dir: True)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(run_claude(str(tmp_path), "hi", [], force_new_session=True))
    assert "--continue" not in captured["args"]
