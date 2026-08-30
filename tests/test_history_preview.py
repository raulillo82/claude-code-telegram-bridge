import json
import os

from bridge.claude_runner import encode_project_path
from bridge.history_preview import get_last_assistant_message


def write_transcript(claude_projects_dir, project_dir, entries):
    store = os.path.join(claude_projects_dir, encode_project_path(project_dir))
    os.makedirs(store, exist_ok=True)
    path = os.path.join(store, "session.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return path


def assistant_text(text, is_sidechain=False):
    return {
        "type": "assistant",
        "isSidechain": is_sidechain,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def assistant_tool_use_only():
    return {
        "type": "assistant",
        "isSidechain": False,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "x", "name": "Bash", "input": {}}],
        },
    }


def test_returns_none_when_no_history(tmp_path):
    result = get_last_assistant_message(
        "/home/raul/claude/some_project", claude_projects_dir=str(tmp_path)
    )
    assert result is None


def test_returns_last_assistant_text(tmp_path):
    project_dir = "/home/raul/claude/nutri"
    write_transcript(
        str(tmp_path),
        project_dir,
        [
            assistant_text("first message"),
            assistant_text("second message"),
        ],
    )
    assert (
        get_last_assistant_message(project_dir, claude_projects_dir=str(tmp_path))
        == "second message"
    )


def test_skips_tool_use_only_entries(tmp_path):
    project_dir = "/home/raul/claude/nutri"
    write_transcript(
        str(tmp_path),
        project_dir,
        [
            assistant_text("the real last text"),
            assistant_tool_use_only(),
        ],
    )
    assert (
        get_last_assistant_message(project_dir, claude_projects_dir=str(tmp_path))
        == "the real last text"
    )


def test_skips_sidechain_entries(tmp_path):
    project_dir = "/home/raul/claude/nutri"
    write_transcript(
        str(tmp_path),
        project_dir,
        [
            assistant_text("main conversation text"),
            assistant_text("a subagent's text", is_sidechain=True),
        ],
    )
    assert (
        get_last_assistant_message(project_dir, claude_projects_dir=str(tmp_path))
        == "main conversation text"
    )


def test_truncates_long_text(tmp_path):
    project_dir = "/home/raul/claude/nutri"
    long_text = "x" * 500
    write_transcript(str(tmp_path), project_dir, [assistant_text(long_text)])
    result = get_last_assistant_message(project_dir, max_chars=50, claude_projects_dir=str(tmp_path))
    assert len(result) == 51  # 50 chars + the ellipsis character
    assert result.endswith("…")
