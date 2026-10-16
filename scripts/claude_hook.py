#!/usr/bin/env python
"""Bash PreToolUse hooks for this project — ONE script, both reminders.

🔴 WHY THIS IS PYTHON AND NOT A SHELL ONE-LINER.
Both hooks used to live inline in `.claude/settings.json` as bash: `$(...)`,
`case`, `2>/dev/null`, calling `python3`. On Windows none of that is valid and
`python3` opens the Microsoft Store rather than failing usefully — so on the
machine this project is moving to, both would have become SILENT NO-OPS. Not an
error, no log line, just two guards quietly gone. That is this repo's signature
failure shape, and one of the two is the DOCS-WITH-CODE rule that keeps
CLAUDE.md honest.

🔑 It lives in `scripts/`, not `.claude/hooks/`, for two reasons: `.claude/hooks`
is sandbox-protected (anything there runs on every tool call), and `scripts/` is
tracked by git, so this travels between machines like the rest of the project.

Invoked as `python3 ... || python ...` so one of the two interpreter names
resolves on either platform. The fallback is safe: a missing interpreter fails
to LAUNCH and therefore consumes no stdin, so the second attempt still receives
the payload.

Reads the hook JSON on stdin, writes at most one hookSpecificOutput to stdout.
It must NEVER raise — a crashing hook is worse than a missing one, because it
interrupts the tool call it was only meant to annotate.
"""
import json
import pathlib
import subprocess
import sys

SEARCH_TOOLS = ("grep", "rg ", "ripgrep", "find ", "fd ", "ack ", "ag ")

GRAPHIFY_NOTE = (
    "graphify: Knowledge graph exists. Read graphify-out/GRAPH_REPORT.md for "
    "god nodes and community structure before searching raw files."
)
DOCS_NOTE = (
    "DOCS-WITH-CODE rule (this project): CLAUDE.md is not part of this commit. "
    "If the change introduced a new pattern, bug class, API, or constraint, "
    "update CLAUDE.md and the memory files and git add them before committing."
)


def _emit(note):
    json.dump({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "additionalContext": note}}, sys.stdout)


def _staged_files(root):
    """Names staged for commit, or [] if git cannot answer.

    [] on failure is deliberate: it makes the DOCS note FIRE. A reminder that
    appears when git is unreadable is noise; one that silently stops appearing
    is a lost guard. For a nudge, prefer the noisy direction.
    """
    try:
        out = subprocess.run(["git", "diff", "--cached", "--name-only"],
                             cwd=str(root), capture_output=True, text=True,
                             timeout=10)
        return out.stdout.split() if out.returncode == 0 else []
    except Exception:
        return []


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(payload, dict):
        return

    cmd = str((payload.get("tool_input") or payload).get("command") or "")
    if not cmd:
        return

    root = pathlib.Path(__file__).resolve().parent.parent

    # 1. Searching raw files when a knowledge graph is already built.
    if any(t in cmd for t in SEARCH_TOOLS):
        if (root / "graphify-out" / "graph.json").exists():
            _emit(GRAPHIFY_NOTE)
            return

    # 2. Committing code without the docs that explain it.
    if "git commit" in cmd and "CLAUDE.md" not in cmd:
        if "CLAUDE.md" not in _staged_files(root):
            _emit(DOCS_NOTE)
            return


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass        # a hook must never break the tool call it annotates
