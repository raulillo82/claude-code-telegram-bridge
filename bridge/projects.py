"""Project directory resolution and live-session safety checks.

Ports the same rules the "launcher" meta-project uses for the interactive
rc flow, as plain deterministic code instead of natural-language rules for
an agent: routing which project a message targets does not need judgment.
"""

import fnmatch
import os
import subprocess


def list_projects(projects_dir: str) -> list[str]:
    projects_dir = os.path.expanduser(projects_dir)
    if not os.path.isdir(projects_dir):
        return []
    return sorted(
        d
        for d in os.listdir(projects_dir)
        if os.path.isdir(os.path.join(projects_dir, d)) and not d.startswith(".")
    )


def resolve_project(name: str, projects_dir: str):
    """Resolve a (possibly partial) project name.

    Returns (resolved_name, "exact"|"case-insensitive"|"prefix"|"substring")
    on a unique match, or (None, []) when nothing matches, or
    (None, [candidates]) when more than one project matches.
    """
    candidates = list_projects(projects_dir)
    if name in candidates:
        return name, "exact"

    lname = name.lower()

    exact_ci = [p for p in candidates if p.lower() == lname]
    if len(exact_ci) == 1:
        return exact_ci[0], "case-insensitive"

    prefix = [p for p in candidates if p.lower().startswith(lname)]
    if len(prefix) == 1:
        return prefix[0], "prefix"
    if len(prefix) > 1:
        return None, prefix

    substring = [p for p in candidates if fnmatch.fnmatch(p.lower(), f"*{lname}*")]
    if len(substring) == 1:
        return substring[0], "substring"
    if len(substring) > 1:
        return None, substring

    return None, []


def has_live_session(project_dir: str) -> str | None:
    """Return the pid of a live `claude` process whose cwd matches
    project_dir (screen-wrapped or not), or None if there isn't one."""
    try:
        output = subprocess.check_output(["pgrep", "-x", "claude"], text=True)
    except subprocess.CalledProcessError:
        return None

    target = os.path.realpath(os.path.expanduser(project_dir))
    for pid in output.split():
        try:
            cwd = os.path.realpath(f"/proc/{pid}/cwd")
        except OSError:
            continue
        if cwd == target:
            return pid
    return None
