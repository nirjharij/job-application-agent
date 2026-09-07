#!/usr/bin/env python3
"""PostToolUse hook: run ruff on the file a Write/Edit tool just touched.

Report-only — never blocks. Findings are surfaced back to Claude as
additionalContext so it can decide whether to fix them.
"""
import json
import os
import subprocess
import sys

PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR") or os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return

    tool_input = payload.get("tool_input", {})
    file_path = tool_input.get("file_path")
    if not file_path or not file_path.endswith(".py"):
        return

    try:
        result = subprocess.run(
            ["uv", "run", "ruff", "check", file_path],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return

    output = (result.stdout + result.stderr).strip()
    if result.returncode == 0 or not output:
        return

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": (
                        "ruff found lint issues in the file just written:\n"
                        f"{output}"
                    ),
                }
            }
        )
    )


if __name__ == "__main__":
    main()