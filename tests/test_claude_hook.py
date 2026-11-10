"""scripts/claude_hook.py — the two PreToolUse reminders, and their wiring.

Both reminders used to be inline bash in .claude/settings.json calling
`python3`, which on Windows is the Microsoft Store stub — so on the new primary
machine they were SILENT NO-OPS. These tests pin the behaviour and that the
settings file actually points at the script.
"""
import io
import json
import os
import subprocess
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
import claude_hook  # noqa: E402


def _run(monkeypatch, capsys, payload, staged=()):
    monkeypatch.setattr(claude_hook, "_staged_files", lambda root: list(staged))
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    claude_hook.main()
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


def _cmd(c):
    return {"tool_input": {"command": c}}


def test_a_commit_without_claude_md_gets_the_docs_reminder(monkeypatch, capsys):
    assert "DOCS-WITH-CODE" in _run(monkeypatch, capsys, _cmd('git commit -m "x"'))


def test_a_commit_with_claude_md_staged_is_quiet(monkeypatch, capsys):
    assert _run(monkeypatch, capsys, _cmd('git commit -m "x"'), staged=["CLAUDE.md"]) == ""


def test_a_commit_naming_claude_md_is_quiet(monkeypatch, capsys):
    assert _run(monkeypatch, capsys, _cmd("git add CLAUDE.md && git commit -m x")) == ""


def test_a_search_gets_the_graphify_reminder(monkeypatch, capsys):
    if not os.path.exists(os.path.join(_ROOT, "graphify-out", "graph.json")):
        pytest.skip("no knowledge graph in this checkout")
    assert "graphify" in _run(monkeypatch, capsys, _cmd("grep -rn foo ."))


def test_a_commit_that_also_greps_gets_BOTH_reminders(monkeypatch, capsys):
    """These were two hooks; merging them into one script must not let the
    first match swallow the second."""
    if not os.path.exists(os.path.join(_ROOT, "graphify-out", "graph.json")):
        pytest.skip("no knowledge graph in this checkout")
    note = _run(monkeypatch, capsys,
                _cmd("git diff --cached | grep -q x && git commit -m x"))
    assert "graphify" in note and "DOCS-WITH-CODE" in note


@pytest.mark.parametrize("cmd", ["git status", "ls", "python -m pytest tests/ -q"])
def test_ordinary_commands_are_quiet(monkeypatch, capsys, cmd):
    assert _run(monkeypatch, capsys, _cmd(cmd)) == ""


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", '{"tool_input": null}', "{}"])
def test_garbage_input_never_raises_or_emits(raw):
    """A crashing hook interrupts the tool call it only meant to annotate."""
    r = subprocess.run([sys.executable, os.path.join(_ROOT, "scripts", "claude_hook.py")],
                       input=raw, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout == ""


# ── Wiring: settings.json must actually call the script ─────────────────────

def _settings():
    with open(os.path.join(_ROOT, ".claude", "settings.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _hook_commands():
    for entry in _settings()["hooks"]["PreToolUse"]:
        for h in entry["hooks"]:
            yield entry["matcher"], h["command"]


def test_every_hook_calls_the_portable_script():
    cmds = list(_hook_commands())
    assert cmds, "no PreToolUse hooks configured"
    for _, c in cmds:
        assert "scripts/claude_hook.py" in c
        assert "python3 -c" not in c, "inline python3 one-liners are dead on Windows"


def test_the_hook_falls_back_to_plain_python():
    """`python3` is the Store stub on Windows; without the fallback the hook
    is a silent no-op there."""
    for _, c in _hook_commands():
        assert "|| python " in c


def test_the_hook_also_watches_the_powershell_tool():
    matchers = {m for m, _ in _hook_commands()}
    assert any("PowerShell" in m for m in matchers)
    assert any("Bash" in m for m in matchers)
