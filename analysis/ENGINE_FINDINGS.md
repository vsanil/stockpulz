# Engine findings — the standing agenda

*Regenerated 2026-08-23 22:28 UTC by `scripts/analyze_engine.py`. Read this at the START of a session and work the open findings.*

## Open: 0 finding(s) needing a decision

_Nothing open. Every addressable finding has been resolved._

### Decided, still present (4)

*You have already ruled on these — they are not in the worklist. They will clear themselves the day the condition disappears.*

- `entry_window/ANET/2026-08-05` — **acknowledged** — ANET filled 2.43% outside the published entry window · _Predates the fix. acf2db4 (2026-08-08) collapsed the entry-window promise to formatters.entry_window_pct and made the gap check warn on gap > window; these fills are 2026-08-05/06. Verified against today's code: a 2.43% and a 2.15% gap both exceed the 2.0% short-term window and WOULD now warn. Historical evidence, not a live defect._
- `entry_window/NWSA/2026-08-06` — **acknowledged** — NWSA filled 2.15% outside the published entry window · _Predates the fix. acf2db4 (2026-08-08) collapsed the entry-window promise to formatters.entry_window_pct and made the gap check warn on gap > window; these fills are 2026-08-05/06. Verified against today's code: a 2.43% and a 2.15% gap both exceed the 2.0% short-term window and WOULD now warn. Historical evidence, not a live defect._
- `integrity/638eb7bc69ed` — **acknowledged** — levels.target_below_entry on AMBA (historical) · _Closed trades from 2026-08-03 — cannot be fixed retroactively. The GENERATOR gap is now closed: ai_analyzer._validate_and_clean_picks drops any pick whose target <= entry or stop >= entry before delivery (guard: TestUnwinnablePicksAreRejected, verified failing pre-fix). This shape can no longer ship._
- `integrity/425fd0b45bd0` — **acknowledged** — levels.target_below_entry on AMBA (historical) · _Closed trades from 2026-08-03 — cannot be fixed retroactively. The GENERATOR gap is now closed: ai_analyzer._validate_and_clean_picks drops any pick whose target <= entry or stop >= entry before delivery (guard: TestUnwinnablePicksAreRejected, verified failing pre-fix). This shape can no longer ship._

**To close one:** implement the fix, or record a decision on the **/admin** dashboard (Engine findings card) or via `scripts/findings.py` (`status`: `acknowledged` | `wont_fix`, plus a `note` saying why). A finding whose condition DISAPPEARS is marked `resolved` automatically — that is the intended path.

🔁 **A finding marked `fixed` that is still present is REOPENED.** Otherwise 'fixed' silently means 'hidden' while the defect is live.

🔴 **Engine changes are never recommended from the bot's win rate.** Mechanical fills; in July they were steering real picks and that loop was cut. Anything needing n≥30 is HELD with its clearing date.

---

## Findings

### [ACT] ANET filled 2.43% outside the published entry window  *(n=48)*

`entry_window/ANET/2026-08-05` · **acknowledged**

> Predates the fix. acf2db4 (2026-08-08) collapsed the entry-window promise to formatters.entry_window_pct and made the gap check warn on gap > window; these fills are 2026-08-05/06. Verified against today's code: a 2.43% and a 2.15% gap both exceed the 2.0% short-term window and WOULD now warn. Historical evidence, not a live defect.

**Evidence:** The morning message promises "enter within X% — skip if above". ANET filled 2.43% above, so a user who OBEYED the instruction would have skipped a pick the bot bought. 2 of 48 observations breach (4.2%).

**Fix:** This is a TRUST defect, not a performance one. Either widen the published window in formatters.entry_window_pct to match measured reality, or make agent._build_premarket_gap_warnings warn on the gap. Do NOT re-hardcode 2 or 3 — that constant is the ONE definition and it has drifted before.

### [ACT] NWSA filled 2.15% outside the published entry window  *(n=48)*

`entry_window/NWSA/2026-08-06` · **acknowledged**

> Predates the fix. acf2db4 (2026-08-08) collapsed the entry-window promise to formatters.entry_window_pct and made the gap check warn on gap > window; these fills are 2026-08-05/06. Verified against today's code: a 2.43% and a 2.15% gap both exceed the 2.0% short-term window and WOULD now warn. Historical evidence, not a live defect.

**Evidence:** The morning message promises "enter within X% — skip if above". NWSA filled 2.15% above, so a user who OBEYED the instruction would have skipped a pick the bot bought. 2 of 48 observations breach (4.2%).

**Fix:** This is a TRUST defect, not a performance one. Either widen the published window in formatters.entry_window_pct to match measured reality, or make agent._build_premarket_gap_warnings warn on the gap. Do NOT re-hardcode 2 or 3 — that constant is the ONE definition and it has drifted before.

### [MEASURE] levels.target_below_entry on AMBA (historical)

`integrity/638eb7bc69ed` · **acknowledged**

> Closed trades from 2026-08-03 — cannot be fixed retroactively. The GENERATOR gap is now closed: ai_analyzer._validate_and_clean_picks drops any pick whose target <= entry or stop >= entry before delivery (guard: TestUnwinnablePicksAreRejected, verified failing pre-fix). This shape can no longer ship.

**Evidence:** target $78.54 is at or below entry $82.67 — this long position cannot reach its target, so it can only ever close at a loss

**Fix:** Historical: acknowledge it. It cannot be fixed retroactively. Worth confirming ai_analyzer._validate_and_clean_picks now rejects the shape so it cannot recur.

### [MEASURE] levels.target_below_entry on AMBA (historical)

`integrity/425fd0b45bd0` · **acknowledged**

> Closed trades from 2026-08-03 — cannot be fixed retroactively. The GENERATOR gap is now closed: ai_analyzer._validate_and_clean_picks drops any pick whose target <= entry or stop >= entry before delivery (guard: TestUnwinnablePicksAreRejected, verified failing pre-fix). This shape can no longer ship.

**Evidence:** target $79.74 is at or below entry $82.21 — this long position cannot reach its target, so it can only ever close at a loss

**Fix:** Historical: acknowledge it. It cannot be fixed retroactively. Worth confirming ai_analyzer._validate_and_clean_picks now rejects the shape so it cannot recur.

---

## Metrics (ongoing — never 'complete')

### [MEASURE] Stop distance distribution  *(n=63)*

**Evidence:** median 5.36% across 63 positions; 0 below the 3.0% threshold.

**Fix:** Context for the geometry metric — no action on its own.

### [MEASURE] Exit-reason mix  *(n=16)*

**Evidence:** {'manual': 5, 'stop': 8, 'target': 3} — stops hit 2.7x as often as targets.  By levels source — unrecorded: 33. Only `pick` speaks to the ENGINE's levels.

**Fix:** Judge the published levels on the `pick` slice ALONE. A high stop:target ratio there means stops are too tight; the same ratio in the fallback slice means the pick's levels did not bracket the fill — levels drifting from the live price by delivery time, which is a different fix.

### [MEASURE] Stop/target geometry on filled positions  *(n=16)*

**Evidence:** median stop 6.4% below entry, median target 11.1% above, reward:risk 1.73:1 across 16 filled positions. The walk-forward backtest measured real ledger picks at 10.3%/5.5% = 1.9:1 — compare against that, never config defaults.

**Fix:** No action while R:R stays near 1.9:1. If it drifts materially below, the stops are tightening relative to targets and will manufacture stop-outs — route any change through scripts/backtest_compare.py first.

### [HOLD] Engine win-rate questions are not yet answerable  *(n=0)*

**Evidence:** 0 matured picks against a gate of 30. The synthetic bot's own record is deliberately excluded — mechanical fills, and feeding them back steers real recommendations.

**Fix:** Do not tune selection weights below the gate. The findings above are safe to act on; win rate is not.

**Held until:** the ledger reaches 30 matured picks (~Sep 10) — below n=30 any conclusion is noise.
