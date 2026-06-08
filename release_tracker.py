"""
release_tracker.py — Tracks AI-generated release notes for StockPulz.

Pending notes are saved to the Gist (release_notes.json) after each git push.
The /release command fetches pending notes, lets the admin review/edit, then
broadcasts to all users and marks the note as sent.
"""

import os
import uuid
import datetime
from config_manager import _load_gist_file, _write_gist_file

RELEASE_NOTES_FILE = "release_notes.json"


def _load() -> dict:
    data = _load_gist_file(RELEASE_NOTES_FILE) or {}
    data.setdefault("pending", [])
    data.setdefault("sent", [])
    return data


def _save(data: dict) -> None:
    _write_gist_file(RELEASE_NOTES_FILE, data)


def get_pending_notes() -> list:
    """Return list of unsent release notes."""
    return _load().get("pending", [])


def add_pending_note(summary: str, commit_hashes: list) -> None:
    """Save a new AI-generated release note as pending."""
    data = _load()
    data["pending"].append({
        "id": str(uuid.uuid4())[:8],
        "summary": summary,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "commits": commit_hashes,
    })
    _save(data)


def mark_note_sent(note_id: str, final_text: str) -> None:
    """Move a note from pending to sent after broadcasting."""
    data = _load()
    note = next((n for n in data["pending"] if n["id"] == note_id), None)
    if note:
        data["pending"] = [n for n in data["pending"] if n["id"] != note_id]
        note["sent_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        note["final_text"] = final_text
        data["sent"].append(note)
        _save(data)


def mark_note_skipped(note_id: str) -> None:
    """Discard a pending note without sending."""
    data = _load()
    data["pending"] = [n for n in data["pending"] if n["id"] != note_id]
    _save(data)
