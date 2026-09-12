"""Tests for bridge/git_sync.py. Same FakeProc/monkeypatch pattern as
tests/test_sync.py -- see that file's module docstring for the rationale."""

import asyncio

from bridge import git_sync


class FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        pass


def test_is_git_project(tmp_path):
    assert not git_sync.is_git_project(str(tmp_path))
    (tmp_path / ".git").mkdir()
    assert git_sync.is_git_project(str(tmp_path))


def test_pull_skips_non_git_project(tmp_path, monkeypatch):
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("git should not run against a non-git directory")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_if_called)
    result = asyncio.run(git_sync.pull(str(tmp_path)))
    assert result.ok
    assert result.skipped


def test_pull_runs_ff_only(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return FakeProc(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = asyncio.run(git_sync.pull(str(tmp_path)))
    assert result.ok
    assert not result.skipped
    assert captured["args"] == ("git", "-C", str(tmp_path), "pull", "--ff-only")


def test_pull_reports_failure_without_raising(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()

    async def fake_exec(*args, **kwargs):
        return FakeProc(returncode=1, stderr=b"fatal: Not possible to fast-forward, aborting.")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = asyncio.run(git_sync.pull(str(tmp_path)))
    assert not result.ok
    assert not result.skipped
    assert "fast-forward" in result.detail


def test_push_if_ahead_skips_non_git_project(tmp_path, monkeypatch):
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("git should not run against a non-git directory")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_if_called)
    result = asyncio.run(git_sync.push_if_ahead(str(tmp_path)))
    assert result.ok
    assert result.skipped


def test_push_if_ahead_skips_when_nothing_new(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return FakeProc(returncode=0, stdout=b"0\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = asyncio.run(git_sync.push_if_ahead(str(tmp_path)))
    assert result.ok
    assert result.skipped
    # Only the rev-list check should run, never an actual push.
    assert len(calls) == 1
    assert calls[0][-3:] == ("rev-list", "--count", "@{u}..HEAD")


def test_push_if_ahead_skips_when_no_upstream(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()

    async def fake_exec(*args, **kwargs):
        return FakeProc(returncode=128, stderr=b"fatal: no upstream configured for branch 'main'")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = asyncio.run(git_sync.push_if_ahead(str(tmp_path)))
    assert result.ok
    assert result.skipped


def test_push_if_ahead_pushes_when_commits_pending(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        if "rev-list" in args:
            return FakeProc(returncode=0, stdout=b"2\n")
        return FakeProc(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = asyncio.run(git_sync.push_if_ahead(str(tmp_path)))
    assert result.ok
    assert not result.skipped
    assert calls[-1][-1] == "push"


def test_push_if_ahead_reports_push_failure(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()

    async def fake_exec(*args, **kwargs):
        if "rev-list" in args:
            return FakeProc(returncode=0, stdout=b"1\n")
        return FakeProc(returncode=1, stderr=b"! [rejected] main -> main (fetch first)")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = asyncio.run(git_sync.push_if_ahead(str(tmp_path)))
    assert not result.ok
    assert not result.skipped
    assert "rejected" in result.detail
