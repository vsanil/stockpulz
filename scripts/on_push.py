#!/usr/bin/env python3
"""
scripts/on_push.py — Called by the post-push git hook.

Reads the latest git changes, uses Haiku to write a plain-English release note
from the USER's perspective, and saves it as a pending note in the Gist.
Run automatically via .githooks/post-push after every git push.

Setup (one-time):
    git config core.hooksPath .githooks
"""

import os
import sys
import subprocess

# Add project root to Python path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _git(*args) -> str:
    """Run a git command and return stdout, or empty string on failure."""
    try:
        return subprocess.check_output(
            ["git"] + list(args), cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except subprocess.CalledProcessError:
        return ""


def get_push_context() -> tuple[str, str, str, list[str]]:
    """
    Return (files_changed, stat_summary, detailed_diff, commit_hashes)
    for the commits that were just pushed.
    """
    # Latest 3 commits (covers multi-commit pushes)
    commit_hashes = [h for h in _git("log", "--format=%H", "-3").split("\n") if h]

    if not commit_hashes:
        return "", "", "", []

    # Files touched across those commits
    files_changed = _git("diff", "--name-only", f"HEAD~{len(commit_hashes)}", "HEAD")
    if not files_changed:
        # Single-commit repo — compare to empty tree
        files_changed = _git("diff", "--name-only", "HEAD")

    stat_summary = _git("diff", "--stat", f"HEAD~{len(commit_hashes)}", "HEAD")

    # Actual diff (capped at 10k chars to fit within Haiku context)
    detailed_diff = _git("diff", "--unified=2", f"HEAD~{len(commit_hashes)}", "HEAD")[:10_000]

    return files_changed, stat_summary, detailed_diff, commit_hashes


def summarize_with_haiku(files_changed: str, stat_summary: str, detailed_diff: str) -> str:
    """Use Haiku to produce a friendly user-facing release note from the diff."""
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY not set.")

    client = anthropic.Anthropic(api_key=api_key)

    prompt = f"""You are writing a release note for StockPulz — a personal Telegram stock advisor bot.

Files changed:
{files_changed}

Stat summary:
{stat_summary}

Diff:
{detailed_diff}

Write a concise, friendly release note (2–5 bullet points) describing what changed FROM THE USER'S PERSPECTIVE.
Focus on: new features, bug fixes, UX improvements. Ignore internal refactors, file moves, or code style changes.
Use simple language. Start each bullet with a relevant emoji.
Do NOT include a title or header — just the bullet points."""

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=350,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


if __name__ == "__main__":
    print("[on_push] Analyzing changes...")

    files_changed, stat_summary, detailed_diff, commit_hashes = get_push_context()

    if not files_changed:
        print("[on_push] No changes detected — skipping release note.")
        sys.exit(0)

    print(f"[on_push] Files changed: {files_changed[:200]}")

    try:
        summary = summarize_with_haiku(files_changed, stat_summary, detailed_diff)
        print(f"[on_push] Summary:\n{summary}\n")
    except Exception as exc:
        print(f"[on_push] Haiku summarization failed: {exc}")
        sys.exit(1)

    try:
        from release_tracker import add_pending_note
        add_pending_note(summary, commit_hashes)
        print("[on_push] ✅ Release note saved to Gist — tap /release in the bot to review.")
    except Exception as exc:
        print(f"[on_push] Failed to save to Gist: {exc}")
        sys.exit(1)
