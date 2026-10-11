"""Tests for bridge/sync.py.

No pytest-asyncio in this repo yet, so async functions are driven with
plain `asyncio.run(...)` inside ordinary `def test_...` functions. The
`FakeProc`/`fake_create_subprocess_exec` pair below is this repo's first
mock of `asyncio.create_subprocess_exec` -- copy this pattern for any
future test needing to fake an async subprocess (e.g. claude_runner.run_claude).
"""

import asyncio
import os

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


def test_has_remote_live_session_finds_matching_cwd(monkeypatch):
    async def fake_exec(*args, **kwargs):
        stdout = b"111:/remote/claude/other_proj\n222:/remote/claude/proj\n"
        return FakeProc(returncode=0, stdout=stdout)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    cfg = make_cfg()
    pid = asyncio.run(sync.has_remote_live_session(cfg, "proj"))
    assert pid == "222"


def test_has_remote_live_session_no_match(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return FakeProc(returncode=0, stdout=b"111:/remote/claude/other_proj\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    cfg = make_cfg()
    pid = asyncio.run(sync.has_remote_live_session(cfg, "proj"))
    assert pid is None


def test_has_remote_live_session_fails_open_on_unreachable_host(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return FakeProc(returncode=255, stderr=b"connection failed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    cfg = make_cfg()
    pid = asyncio.run(sync.has_remote_live_session(cfg, "proj"))
    assert pid is None


def test_has_remote_live_session_disabled(monkeypatch):
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("no subprocess should run when sync is disabled")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_if_called)
    cfg = make_cfg(sync_host=None)
    pid = asyncio.run(sync.has_remote_live_session(cfg, "proj"))
    assert pid is None


def test_materialize_remote_project_creates_dir_then_pulls(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
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
    # One rsync for the project content, one for its (likely nonexistent
    # yet) Claude Code session history -- see test_sync_history_with_remote_*.
    assert len(calls) == 2


def test_claude_history_dir_matches_encode_project_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    from bridge.claude_runner import encode_project_path

    project_dir = str(tmp_path / "proj")
    expected = os.path.join(str(tmp_path / "home"), ".claude", "projects", encode_project_path(project_dir))
    assert sync.claude_history_dir(project_dir) == expected


def test_sync_history_with_remote_skips_push_when_no_local_history(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("nothing to push when no local history exists yet")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_if_called)
    cfg = make_cfg()
    result = asyncio.run(sync.sync_history_with_remote(cfg, str(tmp_path / "proj"), "push"))
    assert result.ok
    assert result.skipped


def test_sync_history_with_remote_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("no subprocess should run when sync is disabled")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_if_called)
    cfg = make_cfg(sync_host=None)
    result = asyncio.run(sync.sync_history_with_remote(cfg, str(tmp_path / "proj"), "pull"))
    assert result.ok
    assert result.skipped


def test_sync_history_with_remote_pull_creates_dir_and_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project_dir = str(tmp_path / "proj")
    history_dir = sync.claude_history_dir(project_dir)
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        assert os.path.isdir(history_dir)
        return FakeProc(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    cfg = make_cfg()
    result = asyncio.run(sync.sync_history_with_remote(cfg, project_dir, "pull"))
    assert result.ok
    assert len(calls) == 1


def test_list_remote_only_offers_git_repos_only_with_an_allowed_origin(monkeypatch):
    async def fake_exec(*args, **kwargs):
        stdout = (
            b"scratch\tnogit\n"
            b"cloneable\tgit\tgit@github.com:someone/cloneable.git\n"
            b"no_remote\tgit\t\n"
            b"other_host\tgit\tgit@example.org:someone/repo.git\n"
            b"https_remote\tgit\thttps://github.com/someone/repo.git\n"
            b"already_local\tgit\tgit@github.com:someone/already.git\n"
        )
        return FakeProc(returncode=0, stdout=stdout)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    cfg = make_cfg(sync_git_clone_hosts=["github.com"])
    plain, git = asyncio.run(sync.list_remote_only(cfg, {"already_local"}))
    assert plain == ["scratch"]
    assert git == ["cloneable"]


def test_is_allowed_clone_url_rejects_anything_but_plain_ssh_on_an_allowed_host():
    """The URL comes from the other host and is handed to `git clone` with
    this host's credentials, so it must not be able to pick another
    transport, another host, or smuggle in an option."""
    cfg = make_cfg(sync_git_clone_hosts=["github.com"])
    assert sync.is_allowed_clone_url(cfg, "git@github.com:someone/repo.git")
    assert sync.is_allowed_clone_url(cfg, "git@github.com:someone/repo")
    for bad in [
        "",
        "git@example.org:someone/repo.git",
        "https://github.com/someone/repo.git",
        "ext::sh -c 'touch /tmp/pwned'",
        "--upload-pack=touch /tmp/pwned",
        "-oProxyCommand=evil git@github.com:someone/repo.git",
        "git@github.com:someone/repo.git --upload-pack=evil",
        "git@github.com:-oProxyCommand=evil/repo",
        "/some/local/path",
        "file:///some/local/path",
        "git@github.com:someone/repo.git\nurl=evil",
    ]:
        assert not sync.is_allowed_clone_url(cfg, bad), bad
    assert not sync.is_allowed_clone_url(make_cfg(), "git@github.com:someone/repo.git")


def _git_materialize_exec(calls, state_stdout, clone_results):
    """ssh -> state_stdout; successive `git clone` calls -> clone_results;
    anything else (the history rsync) succeeds."""
    clone_results = list(clone_results)

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        if args[0] == "ssh":
            return FakeProc(returncode=0, stdout=state_stdout)
        if args[0] == "git":
            return clone_results.pop(0)
        return FakeProc(returncode=0)

    return fake_exec


def test_materialize_remote_git_project_clones_from_origin_and_reports_what_stays_behind(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    project_dir = str(tmp_path / "claude" / "repo")
    calls = []
    state = b"url=git@github.com:someone/repo.git\nbranch=feature/x\nunpushed=2\ndirty=0\nignored=15\n"
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _git_materialize_exec(calls, state, [FakeProc(returncode=0)])
    )
    cfg = make_cfg(sync_git_clone_hosts=["github.com"])

    result, notes = asyncio.run(sync.materialize_remote_git_project(cfg, "repo", project_dir))

    assert result.ok
    clone = next(call for call in calls if call[0] == "git")
    assert clone == ("git", "clone", "--branch", "feature/x", "--", "git@github.com:someone/repo.git", project_dir)
    assert any("2 commit(s)" in note for note in notes)
    assert any("15 git-ignored" in note for note in notes)
    assert not any("uncommitted" in note for note in notes)
    # Session history comes along too, like for non-git projects.
    assert calls[-1][0] == "rsync"


def test_materialize_remote_git_project_falls_back_to_default_branch(tmp_path, monkeypatch):
    # The other host is on a branch that was never pushed.
    monkeypatch.setenv("HOME", str(tmp_path))
    project_dir = str(tmp_path / "claude" / "repo")
    calls = []
    state = b"url=git@github.com:someone/repo.git\nbranch=local-only\nunpushed=1\ndirty=0\nignored=0\n"
    clones = [FakeProc(returncode=128, stderr=b"Remote branch local-only not found"), FakeProc(returncode=0)]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _git_materialize_exec(calls, state, clones))
    cfg = make_cfg(sync_git_clone_hosts=["github.com"])

    result, notes = asyncio.run(sync.materialize_remote_git_project(cfg, "repo", project_dir))

    assert result.ok
    git_calls = [call for call in calls if call[0] == "git"]
    assert git_calls[1] == ("git", "clone", "--", "git@github.com:someone/repo.git", project_dir)
    assert any("local-only" in note for note in notes)


def test_materialize_remote_git_project_refuses_a_disallowed_origin(tmp_path, monkeypatch):
    project_dir = str(tmp_path / "claude" / "repo")
    calls = []
    state = b"url=ext::sh -c evil\nbranch=main\nunpushed=0\ndirty=0\nignored=0\n"
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _git_materialize_exec(calls, state, []))
    cfg = make_cfg(sync_git_clone_hosts=["github.com"])

    result, notes = asyncio.run(sync.materialize_remote_git_project(cfg, "repo", project_dir))

    assert not result.ok
    assert not any(call[0] == "git" for call in calls)
    assert not os.path.exists(project_dir)


def test_materialize_remote_git_project_never_overwrites_or_escapes(tmp_path, monkeypatch):
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("nothing should run")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_if_called)
    cfg = make_cfg(sync_git_clone_hosts=["github.com"])
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep.txt").write_text("precious")

    result, _ = asyncio.run(sync.materialize_remote_git_project(cfg, "existing", str(existing)))
    assert not result.ok
    assert (existing / "keep.txt").read_text() == "precious"

    for name in ["../escape", "-oProxyCommand=evil", ".hidden"]:
        result, _ = asyncio.run(sync.materialize_remote_git_project(cfg, name, str(tmp_path / "x")))
        assert not result.ok, name


def test_materialize_remote_git_project_removes_partial_clone_on_failure(tmp_path, monkeypatch):
    project_dir = tmp_path / "claude" / "repo"
    state = b"url=git@github.com:someone/repo.git\nbranch=\nunpushed=0\ndirty=0\nignored=0\n"

    async def fake_exec(*args, **kwargs):
        if args[0] == "ssh":
            return FakeProc(returncode=0, stdout=state)
        project_dir.mkdir(parents=True)
        (project_dir / "half").write_text("partial")
        return FakeProc(returncode=128, stderr=b"fatal: early EOF")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    cfg = make_cfg(sync_git_clone_hosts=["github.com"])

    result, _ = asyncio.run(sync.materialize_remote_git_project(cfg, "repo", str(project_dir)))

    assert not result.ok
    assert "early EOF" in result.detail
    assert not project_dir.exists()
