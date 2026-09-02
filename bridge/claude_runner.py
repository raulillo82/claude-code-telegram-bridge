"""Wraps headless `claude -p` invocations for a single project directory."""

import asyncio
import json
import os
import re

# Env vars a `claude` CLI process sets for its own rc/session bridge. If the
# bridge is ever started from inside another running Claude Code session,
# these would leak into the subprocess and make it try to reuse the
# parent's session/socket instead of creating its own.
CLAUDE_ENV_VARS_TO_STRIP = [
    "CLAUDE_CODE_CHILD_SESSION",
    "AI_AGENT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_TOKEN",
]

PERMISSION_DENY_PATTERNS = re.compile(
    r"permission denied|not allowed|requires approval|blocked by (?:disallowed|permission)",
    re.IGNORECASE,
)

DEFAULT_TIMEOUT_SECONDS = 300


class ClaudeResult:
    def __init__(self, text: str, permission_denied: bool, duration_ms: int | None = None):
        self.text = text
        self.permission_denied = permission_denied
        # Only reliably set when the CLI's JSON output parsed cleanly.
        # /compact and /clear can genuinely succeed while printing no text
        # at all (the confirmation is a structured event, not chat text) --
        # a real duration lets callers tell "nothing to do" (near-instant)
        # apart from "did something silently" (took real time).
        self.duration_ms = duration_ms


def clean_env() -> dict:
    env = os.environ.copy()
    for key in CLAUDE_ENV_VARS_TO_STRIP:
        env.pop(key, None)
    return env


def encode_project_path(project_dir: str) -> str:
    """Mirror Claude Code's on-disk project storage naming: the absolute
    path with every non-alphanumeric character replaced by '-'.

    Verified against a real example:
    /home/raul/claude/flight_vpn_tunnel -> -home-raul-claude-flight-vpn-tunnel
    (note underscores get collapsed to '-' too, not just '/').
    """
    abs_path = os.path.realpath(os.path.expanduser(project_dir))
    return re.sub(r"[^A-Za-z0-9]", "-", abs_path)


def has_prior_history(project_dir: str) -> bool:
    home = os.path.expanduser("~")
    store = os.path.join(home, ".claude", "projects", encode_project_path(project_dir))
    if not os.path.isdir(store):
        return False
    return any(f.endswith(".jsonl") for f in os.listdir(store))


async def run_claude(
    project_dir: str,
    prompt: str,
    extra_args: list[str],
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    force_new_session: bool = False,
) -> ClaudeResult:
    args = ["claude", "-p", prompt, "--output-format", "json"]
    if not force_new_session and has_prior_history(project_dir):
        args.append("--continue")
    args.extend(extra_args)

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=os.path.expanduser(project_dir),
        env=clean_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ClaudeResult("Claude Code timed out for this request.", False)

    out = stdout.decode(errors="replace").strip()
    err = stderr.decode(errors="replace").strip()

    if out:
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            text = payload.get("result") or ""
            duration_ms = payload.get("duration_ms")
            combined = text or err or "(empty response)"
            denied = bool(PERMISSION_DENY_PATTERNS.search(combined))
            return ClaudeResult(combined, denied, duration_ms)

    combined = out or err or "(empty response)"
    denied = bool(PERMISSION_DENY_PATTERNS.search(combined))
    return ClaudeResult(combined, denied)
