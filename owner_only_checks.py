"""Canary checks that must NOT summon self-heal. ONE definition, two consumers.

🔴 WHY THIS EXISTS — a real feedback loop, observed 2026-09-06.

`canary.check_selfheal_health` reports on the SELF-HEALER. Its own docstring
already promised the right behaviour:

    "NOTE the escape hatch: this reports to the OWNER, not to self-heal.
     Asking a broken self-healer to heal itself is not a plan."

**Nothing enforced it.** A failing check makes the canary exit non-zero;
`self_heal.yml` triggers on `workflow_run.conclusion == 'failure'`; so a red
`selfheal.healthy` summoned the very healer it was reporting broken. And because
that check has a SEVEN-DAY look-back, the red persists long after the cause is
fixed — so every canary run for a week re-armed a path that ends in an
auto-merge to `main` and a Render deploy.

Measured: the credit balance ran out at 22:08 UTC on 2026-09-05, five self-heal
runs failed, and the loop was still live at 05:13 UTC on 2026-09-06 — hours
after the credit was restored and the underlying cause was gone. It spent the
newly-added Claude credits diagnosing findings that were already fixed.

🔑 THE GENERAL RULE: **a monitor that reports on the repair system must not be
able to trigger the repair system.** Same shape as every other bug in this
project — a comment asserting a property that no code checks.

⚠️ **FAIL OPEN.** `all_owner_only()` returns True only when the failing set is
non-empty AND every member is listed here. An empty or unparseable set means
"run self-heal", i.e. the behaviour we had. Getting this backwards would
silently disable the auto-fix net, which is strictly worse than the loop.
"""
from __future__ import annotations

OWNER_ONLY_CHECKS: frozenset[str] = frozenset({
    # Self-referential: this check's whole subject is whether self-heal works.
    # A broken healer cannot heal itself, and trying burns Claude credits on a
    # diagnosis that is structurally guaranteed to be wrong.
    "selfheal.healthy",

    # A dead cron trigger is a cron-job.org / PAT / schedule problem. Self-heal
    # edits CODE, so it cannot fix one — but it CAN "fix" the symptom by
    # loosening the check that reports it, which would delete the monitoring
    # this was built to provide. That is the worst available outcome, so this
    # one goes to the owner too.
    "cron.all_modes_firing",

    # The cause is GitHub Actions scheduler lateness, which no code change can
    # fix — and self-heal's only available "fix" would be to loosen the
    # threshold, i.e. delete the signal. Same argument as cron.all_modes_firing.
    "morning.cache_hit_rate",
})


def all_owner_only(failed: set[str] | frozenset[str] | list[str]) -> bool:
    """True when EVERY failing check is one self-heal must not act on.

    False for an empty set — "nothing parsed" is not "nothing actionable".
    """
    names = {str(n).strip() for n in failed if str(n).strip()}
    return bool(names) and names <= OWNER_ONLY_CHECKS
