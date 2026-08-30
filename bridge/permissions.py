"""Permission profiles for headless Claude Code invocations.

Two modes:
- "normal" (default): a curated allowlist covering everyday work (reading,
  editing, non-destructive git) while blocking irreversible/dangerous
  operations. Anything outside the allowlist is denied automatically, since
  headless mode has no interactive prompt to fall back on.
- "flight": bypasses all permission checks. Meant to be switched on
  deliberately (and briefly) when normal mode blocks something legitimate,
  not left on by default.
"""

NORMAL_ALLOWED_TOOLS = [
    "Read",
    "Grep",
    "Glob",
    "Edit",
    "Write",
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(git branch:*)",
    "Bash(git add:*)",
    "Bash(git commit:*)",
    "Bash(git push:*)",
    "Bash(git pull:*)",
    "Bash(git fetch:*)",
    "Bash(git stash:*)",
    "Bash(git checkout:*)",
    "Bash(ls:*)",
    "Bash(cat:*)",
    "Bash(grep:*)",
    "Bash(find:*)",
    "Bash(pwd)",
    "Bash(head:*)",
    "Bash(tail:*)",
    "Bash(wc:*)",
]

# Explicit denies win over the allowlist above for the same command family.
NORMAL_DISALLOWED_TOOLS = [
    "Bash(git push --force*)",
    "Bash(git push -f*)",
    "Bash(git reset --hard*)",
    "Bash(git clean -f*)",
    "Bash(rm -rf*)",
    "Bash(rm -f*)",
    "Bash(sudo:*)",
    "Bash(chmod:*)",
    "Bash(chown:*)",
    "Bash(curl:*)",
    "Bash(wget:*)",
    "Bash(dd:*)",
    "Bash(mkfs*)",
    "Bash(systemctl:*)",
    "Bash(kill:*)",
    "Bash(pkill:*)",
]


def build_claude_args(mode: str) -> list[str]:
    """Return the extra CLI args to pass to `claude -p` for the given mode."""
    if mode == "flight":
        return ["--dangerously-skip-permissions"]
    return [
        "--allowedTools",
        " ".join(NORMAL_ALLOWED_TOOLS),
        "--disallowedTools",
        " ".join(NORMAL_DISALLOWED_TOOLS),
    ]
