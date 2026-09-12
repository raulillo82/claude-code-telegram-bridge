"""Keeps a git-managed project up to date with its own remote (GitHub,
etc.) around each relayed message.

Independent of the second-host `sync_host` feature in bridge/sync.py,
which explicitly *never* touches git-managed projects -- this is a
separate, always-on mechanism (no config flag: it has no external
dependency beyond the project's own git remote, and nothing here is
destructive). It also doesn't need a two-host live-session guard the way
bridge/sync.py's history sync does: `--ff-only` only ever merges already
-committed, already-pushed history, it can't touch uncommitted work on
another host or hijack a conversation.

Lower-risk than bridge/sync.py's rsync-based merge by construction: git
already has its own conflict detection, so `--ff-only` never rewrites
history or overwrites anything -- it just refuses and reports back when
it can't cleanly fast-forward, instead of guessing.
"""

import asyncio
import logging
import os

from .sync import SyncResult

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30


def is_git_project(project_dir: str) -> bool:
    return os.path.isdir(os.path.join(project_dir, ".git"))


async def _run_git(project_dir: str, args: list[str]) -> SyncResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", project_dir, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        logger.warning("could not start git %s: %s", args, exc)
        return SyncResult(False, str(exc))

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("git %s timed out after %ss", args, TIMEOUT_SECONDS)
        return SyncResult(False, "timed out")

    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip()[:200]
        logger.warning("git %s failed (rc=%s): %s", args, proc.returncode, detail)
        return SyncResult(False, detail)

    return SyncResult(True, stdout=stdout.decode(errors="replace"))


async def pull(project_dir: str) -> SyncResult:
    """`git pull --ff-only`. Never rewrites local history or touches
    uncommitted work -- if it can't cleanly fast-forward (diverged
    history, no upstream configured, network failure, ...) it just fails
    loudly instead of merging or rebasing on its own."""
    if not is_git_project(project_dir):
        return SyncResult(True, "not a git project", skipped=True)
    return await _run_git(project_dir, ["pull", "--ff-only"])


async def push_if_ahead(project_dir: str) -> SyncResult:
    """Pushes only if HEAD is ahead of its upstream tracking branch --
    a no-op (skipped) when there's nothing new, no upstream configured,
    or this isn't a git project at all."""
    if not is_git_project(project_dir):
        return SyncResult(True, "not a git project", skipped=True)

    count = await _run_git(project_dir, ["rev-list", "--count", "@{u}..HEAD"])
    if not count.ok:
        # Most commonly: no upstream configured for the current branch.
        return SyncResult(True, "no upstream to compare against", skipped=True)
    if count.stdout.strip() == "0":
        return SyncResult(True, "nothing to push", skipped=True)

    return await _run_git(project_dir, ["push"])
