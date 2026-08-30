from bridge.permissions import build_claude_args


def test_flight_mode_skips_all_permissions():
    args = build_claude_args("flight")
    assert args == ["--dangerously-skip-permissions"]


def test_normal_mode_allows_common_git_and_read_tools():
    args = build_claude_args("normal")
    allowed = args[args.index("--allowedTools") + 1]
    for expected in ("Read", "Edit", "Bash(git status:*)", "Bash(git commit:*)", "Bash(git push:*)"):
        assert expected in allowed


def test_normal_mode_blocks_destructive_commands():
    args = build_claude_args("normal")
    disallowed = args[args.index("--disallowedTools") + 1]
    for dangerous in (
        "Bash(git push --force*)",
        "Bash(git reset --hard*)",
        "Bash(rm -rf*)",
        "Bash(sudo:*)",
        "Bash(curl:*)",
    ):
        assert dangerous in disallowed


def test_normal_mode_never_skips_permissions():
    args = build_claude_args("normal")
    assert "--dangerously-skip-permissions" not in args
