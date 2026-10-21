"""Portable date formatting — the ONE place a no-leading-zero date is built.

WHY THIS EXISTS. `strftime("%-d")` ("May 1", not "May 01") and `"%-I"`
("9:05 AM", not "09:05 AM") are glibc extensions. They work on Linux, where
production runs (Render + GitHub Actions), and RAISE `ValueError: Invalid
format string` on Windows. Six call sites used them, so on a Windows machine
the morning market-closed line, the end-of-day summary, earnings warnings,
the watchdog alert, an admin command and the mini-app status all crashed —
while CI stayed green, because CI is Linux.

`strftime_portable(dt, fmt)` resolves the `%-X` codes itself and hands the
rest to the platform's own `strftime`, so the output on Linux is
byte-identical to what `dt.strftime(fmt)` produced before.

Guard: tests/test_datefmt.py fails if a `%-` format reappears in any
`strftime` call or f-string format spec in project code.

Deliberately dependency-free: agent.py imports it on the morning path, and
that path is memory-sensitive.
"""
from __future__ import annotations

import re

# Only the codes this repo actually uses (plus their obvious siblings).
# An unsupported %-X raises rather than guessing, so a new one is noticed.
_NO_PAD = {
    "d": lambda dt: str(dt.day),
    "m": lambda dt: str(dt.month),
    "I": lambda dt: str(dt.hour % 12 or 12),
    "H": lambda dt: str(dt.hour),
    "M": lambda dt: str(dt.minute),
}

# "%%" is a literal percent and must be passed through untouched, so it is
# matched FIRST — otherwise "%%-d" (literal "%-d") would be misread.
_TOKEN = re.compile(r"%%|%-(.)")


def strftime_portable(dt, fmt: str) -> str:
    """`dt.strftime(fmt)`, with Linux-style `%-d`/`%-I`/... working everywhere."""
    def _sub(m: re.Match) -> str:
        if m.group(0) == "%%":
            return "%%"
        code = m.group(1)
        if code not in _NO_PAD:
            raise ValueError(f"strftime_portable: unsupported code %-{code}")
        # Digits only, so the result can never introduce a new % directive.
        return _NO_PAD[code](dt)

    return dt.strftime(_TOKEN.sub(_sub, fmt))
