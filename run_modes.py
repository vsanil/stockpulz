"""The canonical list of agent run modes. ONE definition, three consumers.

🔴 WHY THIS EXISTS. The list was written out three times — `agent.detect_run_mode`'s
accept-tuple, `agent.main`'s elif dispatch, and `webhook._VALID_MODES` — and they
had already drifted apart:

    tax_harvest   dispatched by main(), allowed by webhook, and NOT accepted by
                  detect_run_mode. So `RUN_MODE=tax_harvest` fell through to
                  TIME-BASED detection and ran some other mode entirely. That
                  job could never have worked, and nothing said so.

🚨 THE FAILURE MODE IS SILENT AND IT GOT WORSE ON 2026-09-05. Until the cron
triggers moved off Render, `webhook.trigger_mode` validated against its own list
and returned `400 unknown mode`. Dispatching straight to GitHub's API removed
that gate: GitHub accepts any string for `inputs.run_mode`, `daily_run.yml`
passed it through untouched, and `detect_run_mode` treated an unrecognised value
as "not forced" and fell back to the clock.

MEASURED that day — `run_mode=typo_not_a_real_mode` produced:

    Resolved RUN_MODE=typo_not_a_real_mode
    Starting [CONFIRMATION]          <- silently ran a DIFFERENT job
    conclusion: success

⚠️ Between 03:00 and 09:59 ET that clock fallback is `morning`, which BROADCASTS
PICKS TO EVERY USER. A typo in one of seventeen cron-job.org bodies could send
the morning briefing at the wrong hour and report success.

🔑 The fix is both halves: one list so the three consumers cannot drift, and a
loud failure so an unknown mode stops the run instead of quietly becoming
another one. A guess is never safer than a crash here — the modes MESSAGE REAL
USERS, and they are not idempotent.
"""
from __future__ import annotations

# Every mode `agent.main()` can dispatch, plus `confirmation`, which main()
# reaches through its `else:` branch rather than an explicit `elif`.
# ⚠️ Adding a mode? Add it HERE and add its `elif` in main(). The test
# `tests/test_run_modes.py` fails if the two ever disagree again.
VALID_MODES: frozenset[str] = frozenset({
    "morning",
    "confirmation",
    "premarket",
    "digest",
    "prescreener",
    "price_alerts",
    "midday_check",
    "vix_check",
    "news_check",
    "close_check",
    "eod_summary",
    "pre_earnings",
    "macro_alert",
    "watchdog",
    "weekly",
    "week_ahead",
    "friday_wrap",
    "monthly_commentary",
    "tax_harvest",
})
# 🔎 `recap` was here until 2026-09-05 and is deliberately GONE. It had a
# function and a dispatch branch in agent.py but was never in
# webhook._VALID_MODES, so nothing could ever trigger it — dead code that still
# had to be reasoned about every time this list was read. Removed on the owner's
# call rather than wired up. Do not re-add without a trigger to go with it.


def normalise(mode: str | None) -> str | None:
    """Trim + lowercase, or None for an unset value. Does NOT validate."""
    if mode is None:
        return None
    m = mode.strip().lower()
    return m or None


def is_valid(mode: str | None) -> bool:
    return normalise(mode) in VALID_MODES


def check(mode: str | None) -> str:
    """Return the normalised mode, or raise ValueError naming the alternatives.

    ⚠️ Raises rather than falling back. Silently substituting a mode is how a
    typo became a wrong-job-at-the-wrong-hour that reported success.
    """
    m = normalise(mode)
    if m in VALID_MODES:
        return m
    near = sorted(v for v in VALID_MODES if m and (v.startswith(m[:3]) or m[:3] in v))
    hint = f" Did you mean: {', '.join(near)}?" if near else ""
    raise ValueError(
        f"unknown run mode {mode!r}."
        f"{hint} Valid modes: {', '.join(sorted(VALID_MODES))}"
    )
