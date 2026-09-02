"""Two-way sync of non-git project directories with a second host.

Most projects under `projects_dir` are ad-hoc scratch directories with no
git remote, so unlike git-backed projects (which already self-sync via
`git push`/`pull`) they only ever exist on whichever host last touched
them. This module bridges that gap with `rsync -au` (no `--delete` --
never mirror-delete, since that could wipe out a file created on one side
that doesn't exist on the other yet) run in both directions around each
relayed message.

Nothing in this module raises: every public function returns a
`SyncResult`, matching the rest of the bridge's "check and reply"
convention, so callers never need a bare `except Exception` around it. A
failure here is never allowed to block or crash message handling -- the
second host isn't guaranteed to be reachable, and that's an expected,
routine condition, not an error.
"""

import asyncio
import logging
import os

from .claude_runner import encode_project_path

logger = logging.getLogger(__name__)

RSYNC_EXCLUDES = [".git", ".venv", "venv", "__pycache__", "node_modules"]

# First pull of a project that has never existed locally can be much bigger
# than a steady-state incremental sync (e.g. a scratch dir full of log/csv
# exports) -- give it more room than the per-message timeout.
FIRST_SYNC_TIMEOUT_SECONDS = 600


class SyncResult:
    def __init__(self, ok: bool, detail: str = "", skipped: bool = False, stdout: str = ""):
        self.ok = ok
        # Diagnostic text for a failure (a trimmed stderr snippet, "timed
        # out", etc.) or the reason for a no-op. Not populated on success.
        self.detail = detail
        # True = deliberate no-op (feature disabled, or a git-managed
        # project this mechanism must never touch) -- not a failure worth
        # surfacing to the user, unlike ok=False.
        self.skipped = skipped
        # Raw stdout from a successful subprocess run, for callers that
        # need to parse it (e.g. list_remote_only_projects).
        self.stdout = stdout


def sync_enabled(cfg: dict) -> bool:
    return bool(cfg.get("sync_host"))


def build_rsync_args(src: str, dst: str, connect_timeout: int) -> list[str]:
    args = ["rsync", "-au"]
    for pattern in RSYNC_EXCLUDES:
        args += ["--exclude", pattern]
    args += [
        "-e", f"ssh -o BatchMode=yes -o ConnectTimeout={connect_timeout}",
        src, dst,
    ]
    return args


def remote_endpoint(host: str, remote_dir: str, project_name: str) -> str:
    """Trailing slash so rsync syncs the directory's *contents*, not the
    directory itself nested one level deeper into the destination."""
    return f"{host}:{os.path.join(remote_dir, project_name)}/"


def build_remote_listing_command(remote_dir: str) -> str:
    """Shell one-liner run on the remote host: one "name\\tgit|nogit" line
    per top-level directory under remote_dir. Plain `*/` globbing already
    skips dotdirs (no dotglob), matching bridge/projects.py's local
    behavior of ignoring names starting with '.'."""
    return (
        f'for d in "{remote_dir}"/*/; do '
        f'n=$(basename "$d"); '
        f'[ -d "$d.git" ] && echo "$n\tgit" || echo "$n\tnogit"; '
        f"done"
    )


async def _run_subprocess(args: list[str], timeout: int) -> SyncResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        logger.warning("could not start %s: %s", args[0], exc)
        return SyncResult(False, str(exc))

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("%s timed out after %ss: %s", args[0], timeout, args)
        return SyncResult(False, "timed out")

    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip()[:200]
        logger.warning("%s failed (rc=%s): %s", args[0], proc.returncode, detail)
        return SyncResult(False, detail)

    return SyncResult(True, stdout=stdout.decode(errors="replace"))


async def sync_project_with_remote(
    cfg: dict, project_name: str, project_dir: str, direction: str
) -> SyncResult:
    """direction: "pull" (remote -> local) or "push" (local -> remote).

    No-ops (skipped=True) if the feature isn't configured, or if
    project_dir is git-managed -- this mechanism must never touch a git
    repo, on either side, since mtime-based file sync can corrupt git's
    object store."""
    if not sync_enabled(cfg):
        return SyncResult(True, "sync not configured", skipped=True)
    if os.path.isdir(os.path.join(project_dir, ".git")):
        return SyncResult(True, "git-managed project, sync skipped", skipped=True)

    remote = remote_endpoint(cfg["sync_host"], cfg["sync_remote_projects_dir"], project_name)
    local = project_dir.rstrip("/") + "/"
    src, dst = (remote, local) if direction == "pull" else (local, remote)

    args = build_rsync_args(src, dst, cfg["sync_connect_timeout_seconds"])
    return await _run_subprocess(args, cfg["sync_rsync_timeout_seconds"])


def claude_history_dir(project_dir: str) -> str:
    """Local path to this project's Claude Code session history.

    This lives under ~/.claude/projects/<encoded-path>/, entirely outside
    the project directory itself -- so neither this module's rsync (for
    non-git projects) nor git (for git-managed ones) ever touches it, and
    a session continued via the bridge on one host silently forks away
    from what `claude --continue` sees on the other host. Unlike the
    project directory itself, this is never a git repo, so it's synced
    regardless of whether the project it belongs to is git-managed."""
    encoded = encode_project_path(project_dir)
    return os.path.join(os.path.expanduser("~"), ".claude", "projects", encoded)


async def sync_history_with_remote(cfg: dict, project_dir: str, direction: str) -> SyncResult:
    """Sync a project's Claude Code session history with the remote host.

    Assumes project_dir resolves to the same absolute path on both hosts
    (same username, same layout under projects_dir) -- true for this
    deployment, since encode_project_path's encoding has to match on both
    sides for history to line up at all.

    Best-effort in a stronger sense than sync_project_with_remote: a
    missing history dir (no conversation yet, on either side) is the
    common case, not a failure, so callers should treat any non-ok result
    here as unremarkable rather than surfacing it as a warning."""
    if not sync_enabled(cfg):
        return SyncResult(True, "sync not configured", skipped=True)

    encoded = encode_project_path(project_dir)
    local_history = claude_history_dir(project_dir)
    if direction == "push" and not os.path.isdir(local_history):
        return SyncResult(True, "no local history yet", skipped=True)

    os.makedirs(local_history, exist_ok=True)
    remote_history = f"{cfg['sync_host']}:~/.claude/projects/{encoded}/"
    local = local_history.rstrip("/") + "/"
    src, dst = (remote_history, local) if direction == "pull" else (local, remote_history)

    args = build_rsync_args(src, dst, cfg["sync_connect_timeout_seconds"])
    return await _run_subprocess(args, cfg["sync_rsync_timeout_seconds"])


async def has_remote_live_session(cfg: dict, project_name: str) -> str | None:
    """Mirrors bridge/projects.py::has_live_session, but for the remote
    host: returns the pid of a live `claude` process there with this
    project as cwd, or None if there isn't one, sync is disabled, or the
    remote can't be checked right now (fails open, like the rest of this
    module -- an unreachable second host must never block message
    handling).

    This exists because history sync is not just a races risk: `claude
    --continue` always resumes the most-recently-modified transcript in a
    project's history, so pulling one in from a host with an actively
    open interactive session there can silently hijack and continue that
    live conversation instead of starting fresh -- discovered by actually
    triggering it once. Callers must check this (and the local
    has_live_session) before running any sync for a project, not just
    before sending a message through it."""
    if not sync_enabled(cfg):
        return None

    remote_dir = os.path.join(cfg["sync_remote_projects_dir"], project_name)
    target = os.path.normpath(remote_dir)
    shell_cmd = (
        "for pid in $(pgrep -x claude); do "
        'p="$(readlink -f /proc/$pid/cwd 2>/dev/null)"; '
        '[ -n "$p" ] && echo "$pid:$p"; '
        "done"
    )
    connect_timeout = cfg["sync_connect_timeout_seconds"]
    args = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}", cfg["sync_host"], shell_cmd]

    result = await _run_subprocess(args, connect_timeout + 5)
    if not result.ok:
        return None

    for line in result.stdout.splitlines():
        pid, _, cwd = line.partition(":")
        if cwd == target:
            return pid
    return None


async def list_remote_only_projects(cfg: dict, local_names: set[str]) -> list[str]:
    """Names present under the remote projects_dir but not in local_names,
    excluding remote git repos. Returns [] on any failure/timeout -- a
    failed listing just means "nothing new to show this time", not an
    error worth surfacing."""
    if not sync_enabled(cfg):
        return []

    shell_cmd = build_remote_listing_command(cfg["sync_remote_projects_dir"])
    connect_timeout = cfg["sync_connect_timeout_seconds"]
    args = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}", cfg["sync_host"], shell_cmd]

    result = await _run_subprocess(args, connect_timeout + 5)
    if not result.ok:
        return []

    remote_names = []
    for line in result.stdout.splitlines():
        name, _, kind = line.partition("\t")
        if name and kind == "nogit" and name not in local_names:
            remote_names.append(name)
    return sorted(remote_names)


async def materialize_remote_project(cfg: dict, project_name: str, project_dir: str) -> SyncResult:
    """One-time pull for a project that doesn't exist locally yet.

    Also pulls its Claude Code session history, if any, so a project used
    interactively on the other host (but never before through the bridge)
    keeps its conversation continuity from the very first message."""
    os.makedirs(project_dir, exist_ok=True)
    remote = remote_endpoint(cfg["sync_host"], cfg["sync_remote_projects_dir"], project_name)
    local = project_dir.rstrip("/") + "/"
    args = build_rsync_args(remote, local, cfg["sync_connect_timeout_seconds"])
    result = await _run_subprocess(args, FIRST_SYNC_TIMEOUT_SECONDS)

    await sync_history_with_remote(cfg, project_dir, "pull")
    return result
