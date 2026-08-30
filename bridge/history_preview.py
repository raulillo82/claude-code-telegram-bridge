"""Cheap, local "where did I leave off" preview for a project.

Reads Claude Code's own transcript (.jsonl) file directly, without
invoking `claude` at all. This matters: an actual `claude -p --continue`
call pays for loading (and possibly auto-compacting) the whole
conversation, which can take minutes on a large history — far too
expensive to run just because the user tapped a project button in
/projects.
"""

import json
import os

from .claude_runner import encode_project_path

DEFAULT_MAX_CHARS = 800


def _latest_transcript(store_dir: str) -> str | None:
    if not os.path.isdir(store_dir):
        return None
    jsonl_files = [f for f in os.listdir(store_dir) if f.endswith(".jsonl")]
    if not jsonl_files:
        return None
    latest = max(jsonl_files, key=lambda f: os.path.getmtime(os.path.join(store_dir, f)))
    return os.path.join(store_dir, latest)


def get_last_assistant_message(
    project_dir: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    claude_projects_dir: str | None = None,
) -> str | None:
    """Return the last non-sidechain assistant text message for this
    project, or None if there's no history (or no plain-text message) yet.
    """
    if claude_projects_dir is None:
        claude_projects_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects")

    store_dir = os.path.join(claude_projects_dir, encode_project_path(project_dir))
    path = _latest_transcript(store_dir)
    if path is None:
        return None

    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        if entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue

        content = entry.get("message", {}).get("content", [])
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                text = block["text"].strip()
                if len(text) > max_chars:
                    text = text[:max_chars].rstrip() + "…"
                return text

    return None
