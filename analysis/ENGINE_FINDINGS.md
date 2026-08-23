# Engine findings — the standing agenda

*Regenerated 2026-08-23 05:12 UTC by `scripts/analyze_engine.py`. Read this at the START of a session and work the open findings.*

## Open: 5 finding(s) needing a decision

| id | what | age | fix in |
|---|---|---|---|
| `entry_window/ANET/2026-08-05` | ANET filled 2.43% outside the published entry window | 0d | formatters.entry_window_pct / agent._build_premarket_gap_warnings |
| `entry_window/NWSA/2026-08-06` | NWSA filled 2.15% outside the published entry window | 0d | formatters.entry_window_pct / agent._build_premarket_gap_warnings |
| `stop_tight/KMI/2.99` | KMI stop at 2.99% is inside the noise threshold | 0d | screener.suggested_stop_pct (1.5x ATR%) — add a floor |
| `integrity/638eb7bc69ed` | levels.target_below_entry on AMBA (historical) | 0d | ai_analyzer._validate_and_clean_picks |
| `integrity/425fd0b45bd0` | levels.target_below_entry on AMBA (historical) | 0d | ai_analyzer._validate_and_clean_picks |

**To close one:** implement the fix, or record a decision in `analysis/findings_state.json` (`status`: `acknowledged` | `wont_fix`, plus a `note` saying why). A finding whose condition DISAPPEARS is marked `resolved` automatically — that is the intended path.

🔁 **A finding marked `fixed` that is still present is REOPENED.** Otherwise 'fixed' silently means 'hidden' while the defect is live.

🔴 **Engine changes are never recommended from the bot's win rate.** Mechanical fills; in July they were steering real picks and that loop was cut. Anything needing n≥30 is HELD with its clearing date.

---

## Findings

### [ACT] ANET filled 2.43% outside the published entry window  *(n=48)*

`entry_window/ANET/2026-08-05` · **open**

**Evidence:** The morning message promises "enter within X% — skip if above". ANET filled 2.43% above, so a user who OBEYED the instruction would have skipped a pick the bot bought. 2 of 48 observations breach (4.2%).

**Fix:** This is a TRUST defect, not a performance one. Either widen the published window in formatters.entry_window_pct to match measured reality, or make agent._build_premarket_gap_warnings warn on the gap. Do NOT re-hardcode 2 or 3 — that constant is the ONE definition and it has drifted before.

### [ACT] NWSA filled 2.15% outside the published entry window  *(n=48)*

`entry_window/NWSA/2026-08-06` · **open**

**Evidence:** The morning message promises "enter within X% — skip if above". NWSA filled 2.15% above, so a user who OBEYED the instruction would have skipped a pick the bot bought. 2 of 48 observations breach (4.2%).

**Fix:** This is a TRUST defect, not a performance one. Either widen the published window in formatters.entry_window_pct to match measured reality, or make agent._build_premarket_gap_warnings warn on the gap. Do NOT re-hardcode 2 or 3 — that constant is the ONE definition and it has drifted before.

### [ACT] KMI stop at 2.99% is inside the noise threshold  *(n=63)*

`stop_tight/KMI/2.99` · **open**

**Evidence:** Threshold is 3.0%; median across 63 positions is 5.36%. A stop inside ordinary daily movement converts a sound thesis into a stop-out.

**Fix:** screener.suggested_stop_pct is 1.5x ATR%, which collapses for a low-volatility name. Add a floor there (or in ai_analyzer._ST_SECTIONS' 5% fallback) so no published stop sits below the noise threshold.

### [MEASURE] levels.target_below_entry on AMBA (historical)

`integrity/638eb7bc69ed` · **open**

**Evidence:** target $78.54 is at or below entry $82.67 — this long position cannot reach its target, so it can only ever close at a loss

**Fix:** Historical: acknowledge it. It cannot be fixed retroactively. Worth confirming ai_analyzer._validate_and_clean_picks now rejects the shape so it cannot recur.

### [MEASURE] levels.target_below_entry on AMBA (historical)

`integrity/425fd0b45bd0` · **open**

**Evidence:** target $79.74 is at or below entry $82.21 — this long position cannot reach its target, so it can only ever close at a loss

**Fix:** Historical: acknowledge it. It cannot be fixed retroactively. Worth confirming ai_analyzer._validate_and_clean_picks now rejects the shape so it cannot recur.

---

## Metrics (ongoing — never 'complete')

### [MEASURE] Stop distance distribution  *(n=63)*

**Evidence:** median 5.36% across 63 positions; 1 below the 3.0% threshold.

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
