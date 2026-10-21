"""datefmt.strftime_portable — output identical to Linux, and the guard that
keeps Linux-only `%-X` formats out of the codebase.

`%-d` / `%-I` are glibc extensions: fine on Linux (production), ValueError on
Windows. Six sites used them and crashed the morning market-closed line, the
EOD summary, earnings warnings, the watchdog alert, an admin command and the
mini-app status on a Windows checkout — with CI green, because CI is Linux.
"""
import ast
import datetime as dt
import glob
import os
import sys

import pytest

from datefmt import strftime_portable

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── Output is exactly what Linux strftime produced ──────────────────────────

@pytest.mark.parametrize("when, fmt, expected", [
    (dt.date(2026, 5, 1),               "%a %b %-d",     "Fri May 1"),
    (dt.date(2026, 5, 10),              "%a %b %-d",     "Sun May 10"),
    (dt.date(2026, 5, 26),              "%A, %b %-d",    "Tuesday, May 26"),
    (dt.date(2026, 10, 5),              "%b %-d",        "Oct 5"),
    (dt.datetime(2026, 5, 1, 0, 15),    "%-I:%M %p ET",  "12:15 AM ET"),   # midnight
    (dt.datetime(2026, 5, 1, 9, 5),     "%-I:%M %p ET",  "9:05 AM ET"),
    (dt.datetime(2026, 5, 1, 12, 30),   "%-I:%M %p ET",  "12:30 PM ET"),   # noon
    (dt.datetime(2026, 5, 1, 23, 59),   "%-I:%M %p ET",  "11:59 PM ET"),
    (dt.datetime(2026, 1, 2, 3, 4),     "%-m/%-d %-H:%-M", "1/2 3:4"),
])
def test_output_matches_linux(when, fmt, expected):
    assert strftime_portable(when, fmt) == expected


@pytest.mark.skipif(sys.platform == "win32", reason="glibc %-X is the reference; Windows has none")
@pytest.mark.parametrize("fmt", ["%a %b %-d", "%A, %b %-d", "%b %-d", "%-I:%M %p ET"])
def test_byte_identical_to_native_strftime_on_linux(fmt):
    """Where the native form exists, the helper must reproduce it exactly —
    production output must not move by a single character."""
    for h in (0, 1, 9, 11, 12, 13, 23):
        for day in (1, 9, 10, 31):
            when = dt.datetime(2026, 1, day, h, 7)
            assert strftime_portable(when, fmt) == when.strftime(fmt)


def test_plain_formats_pass_straight_through():
    when = dt.datetime(2026, 5, 1, 9, 5)
    assert strftime_portable(when, "%Y-%m-%d %H:%M") == "2026-05-01 09:05"


def test_a_literal_percent_is_not_misread_as_a_directive():
    # "%%-d" is a literal "%-d" in strftime, NOT a day.
    assert strftime_portable(dt.date(2026, 5, 1), "100%% %%-d") == "100% %-d"


def test_an_unsupported_no_pad_code_raises_rather_than_guessing():
    with pytest.raises(ValueError):
        strftime_portable(dt.date(2026, 5, 1), "%-j")


# ── Guard: no Linux-only %-X format anywhere in project code ────────────────

def _project_files():
    return sorted(glob.glob(os.path.join(_ROOT, "*.py")) +
                  glob.glob(os.path.join(_ROOT, "scripts", "*.py")))


def _offenders(tree, path):
    """AST, not text: prose like "a 4%-ATR name" must not trip it, and a
    comment explaining the rule must not either."""
    bad = []
    for node in ast.walk(tree):
        # x.strftime("...%-d...")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "strftime" and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and "%-" in node.args[0].value):
            bad.append(f"{os.path.relpath(path, _ROOT)}:{node.lineno} strftime({node.args[0].value!r})")
        # f"{x:%b %-d}"
        if isinstance(node, ast.FormattedValue) and node.format_spec is not None:
            spec = "".join(v.value for v in node.format_spec.values
                           if isinstance(v, ast.Constant) and isinstance(v.value, str))
            if "%-" in spec:
                bad.append(f"{os.path.relpath(path, _ROOT)}:{node.lineno} f-string spec {spec!r}")
    return bad


def test_no_linux_only_strftime_in_project_code():
    bad = []
    for path in _project_files():
        with open(path, encoding="utf-8") as fh:
            bad += _offenders(ast.parse(fh.read(), path), path)
    assert not bad, ("Linux-only %-X date format (raises on Windows) — use "
                     "datefmt.strftime_portable instead:\n  " + "\n  ".join(bad))


def test_the_scan_scans_something():
    assert len(_project_files()) > 40


@pytest.mark.parametrize("src", [
    'd.strftime("%a %b %-d")',
    "t.strftime('%-I:%M %p')",
    'f"{d:%b %-d}"',
])
def test_the_guard_detects_an_offender(src):
    assert _offenders(ast.parse(src), os.path.join(_ROOT, "x.py"))


@pytest.mark.parametrize("src", [
    'strftime_portable(d, "%a %b %-d")',     # the sanctioned form
    'd.strftime("%Y-%m-%d")',
    '# a 4%-ATR name, d.strftime("%-d")',   # a comment
    'x = "a 4%-ATR name"',                   # prose in a string, not a format
])
def test_the_guard_does_not_cry_wolf(src):
    assert not _offenders(ast.parse(src), os.path.join(_ROOT, "x.py"))
