"""Tests for bridge/sync.py.

No pytest-asyncio in this repo yet, so async functions are driven with
plain `asyncio.run(...)` inside ordinary `def test_...` functions. The
`FakeProc`/`fake_create_subprocess_exec` pair below is this repo's first
mock of `asyncio.create_subprocess_exec` -- copy this pattern for any
future test needing to fake an async subprocess (e.g. claude_runner.run_claude).
"""

import asyncio

from bridge import sync


def make_cfg(**overrides):
    cfg = {
        "sync_host": "otherhost",
        "sync_remote_projects_dir": "/remote/claude",
        "sync_connect_timeout_seconds": 5,
        "sync_rsync_timeout_seconds": 120,
    }
    cfg.update(overrides)
    return cfg


def test_sync_enabled():
    assert sync.sync_enabled(make_cfg()) is True
    assert sync.sync_enabled(make_cfg(sync_host=None)) is False
    assert sync.sync_enabled(make_cfg(sync_host="")) is False
    assert sync.sync_enabled({}) is False


def test_build_rsync_args_includes_all_excludes_and_timeout():
    args = sync.build_rsync_args("src/", "dst/", connect_timeout=7)
    assert args[0:2] == ["rsync", "-au"]
    exclude_values = [args[i + 1] for i, a in enumerate(args) if a == "--exclude"]
    assert set(exclude_values) == set(sync.RSYNC_EXCLUDES)
    assert "-e" in args
    ssh_cmd = args[args.index("-e") + 1]
    assert "ConnectTimeout=7" in ssh_cmd
    assert "BatchMode=yes" in ssh_cmd
    assert args[-2:] == ["src/", "dst/"]


def test_remote_endpoint_has_trailing_slash():
    assert sync.remote_endpoint("host", "/remote/claude", "proj") == "host:/remote/claude/proj/"


class FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False
        self.waited = False

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        self.waited = True


def test_sync_project_with_remote_skips_when_disabled(monkeypatch):
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("no subprocess should run when sync is disabled")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_if_called)
    cfg = make_cfg(sync_host=None)
    result = asyncio.run(sync.sync_project_with_remote(cfg, "proj", "/local/proj", "pull"))
    assert result.ok
    assert result.skipped


def test_sync_project_with_remote_skips_git_managed_project(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("git-managed projects must never be rsynced")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_if_called)
    cfg = make_cfg()
    result = asyncio.run(sync.sync_project_with_remote(cfg, "proj", str(tmp_path), "pull"))
    assert result.ok
    assert result.skipped


def test_sync_project_with_remote_success(tmp_path, monkeypatch):
    async def fake_exec(*args, **kwargs):
        return FakeProc(returncode=0, stdout=b"itemized changes\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    cfg = make_cfg()
    result = asyncio.run(sync.sync_project_with_remote(cfg, "proj", str(tmp_path), "push"))
    assert result.ok
    assert not result.skipped


def test_sync_project_with_remote_failure(tmp_path, monkeypatch):
    async def fake_exec(*args, **kwargs):
        return FakeProc(returncode=1, stderr=b"rsync error: connection refused")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    cfg = make_cfg()
    result = asyncio.run(sync.sync_project_with_remote(cfg, "proj", str(tmp_path), "pull"))
    assert not result.ok
    assert not result.skipped
    assert "connection refused" in result.detail


def test_sync_project_with_remote_timeout(tmp_path, monkeypatch):
    proc = FakeProc(returncode=0)

    async def fake_exec(*args, **kwargs):
        return proc

    async def fake_wait_for(coro, timeout):
        coro.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    cfg = make_cfg()
    result = asyncio.run(sync.sync_project_with_remote(cfg, "proj", str(tmp_path), "pull"))
    assert not result.ok
    assert result.detail == "timed out"
    assert proc.killed
    assert proc.waited


def test_list_remote_only_projects_filters_git_and_local(monkeypatch):
    async def fake_exec(*args, **kwargs):
        stdout = b"scratch1\tnogit\nscratch2\tnogit\nrepo\tgit\nalready_local\tnogit\n"
        return FakeProc(returncode=0, stdout=stdout)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    cfg = make_cfg()
    result = asyncio.run(sync.list_remote_only_projects(cfg, {"already_local"}))
    assert result == ["scratch1", "scratch2"]


def test_list_remote_only_projects_returns_empty_on_failure(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return FakeProc(returncode=255, stderr=b"connection failed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    cfg = make_cfg()
    result = asyncio.run(sync.list_remote_only_projects(cfg, set()))
    assert result == []


def test_list_remote_only_projects_disabled(monkeypatch):
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("no subprocess should run when sync is disabled")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_if_called)
    cfg = make_cfg(sync_host=None)
    result = asyncio.run(sync.list_remote_only_projects(cfg, set()))
    assert result == []


def test_materialize_remote_project_creates_dir_then_pulls(tmp_path, monkeypatch):
    project_dir = tmp_path / "new_proj"
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        assert project_dir.is_dir(), "directory must exist before rsync runs"
        return FakeProc(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    cfg = make_cfg()
    result = asyncio.run(sync.materialize_remote_project(cfg, "new_proj", str(project_dir)))
    assert result.ok
    assert len(calls) == 1
