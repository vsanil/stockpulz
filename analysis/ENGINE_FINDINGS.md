# Engine findings — the standing agenda

*Regenerated 2026-09-10 23:55 UTC by `scripts/analyze_engine.py`. Read this at the START of a session and work the open findings.*

## Open: 5 finding(s) needing a decision

| id | what | age | fix in |
|---|---|---|---|
| `entry_window/NVDA/2026-08-27` | NVDA filled 8.98% outside the published entry window | 13d | formatters.entry_window_pct / agent._build_premarket_gap_warnings |
| `entry_window/AMZN/2026-08-28` | AMZN filled 4.0% outside the published entry window | 12d | formatters.entry_window_pct / agent._build_premarket_gap_warnings |
| `entry_window/NOW/2026-08-31` | NOW filled 2.68% outside the published entry window | 9d | formatters.entry_window_pct / agent._build_premarket_gap_warnings |
| `entry_window/MMED/2026-09-02` | MMED filled 5.74% outside the published entry window | 8d | formatters.entry_window_pct / agent._build_premarket_gap_warnings |
| `entry_window/DOT/2026-09-08` | DOT filled 11.22% outside the published entry window | 2d | formatters.entry_window_pct / agent._build_premarket_gap_warnings |

### Decided, still present (2)

*You have already ruled on these — they are not in the worklist. They will clear themselves the day the condition disappears.*

- `integrity/638eb7bc69ed` — **acknowledged** — levels.target_below_entry on AMBA (historical) · _Closed trades from 2026-08-03 — cannot be fixed retroactively. The GENERATOR gap is now closed: ai_analyzer._validate_and_clean_picks drops any pick whose target <= entry or stop >= entry before delivery (guard: TestUnwinnablePicksAreRejected, verified failing pre-fix). This shape can no longer ship._
- `integrity/425fd0b45bd0` — **acknowledged** — levels.target_below_entry on AMBA (historical) · _Closed trades from 2026-08-03 — cannot be fixed retroactively. The GENERATOR gap is now closed: ai_analyzer._validate_and_clean_picks drops any pick whose target <= entry or stop >= entry before delivery (guard: TestUnwinnablePicksAreRejected, verified failing pre-fix). This shape can no longer ship._

**To close one:** implement the fix, or record a decision on the **/admin** dashboard (Engine findings card) or via `scripts/findings.py` (`status`: `acknowledged` | `wont_fix`, plus a `note` saying why). A finding whose condition DISAPPEARS is marked `resolved` automatically — that is the intended path.

🔁 **A finding marked `fixed` that is still present is REOPENED.** Otherwise 'fixed' silently means 'hidden' while the defect is live.

🔴 **Engine changes are never recommended from the bot's win rate.** Mechanical fills; in July they were steering real picks and that loop was cut. Anything needing n≥30 is HELD with its clearing date.

---

## Findings

### [ACT] [TECHNICAL BUG] NVDA filled 8.98% outside the published entry window  *(n=96)*

`entry_window/NVDA/2026-08-27` · **open**, open 13d

NVDA was bought 8.98% above the price the morning message told people not to go past, so anyone who followed that instruction would have skipped a pick the bot itself took.

**Technical bug** — fixes broken behaviour; changes nothing about how picks are chosen.

<details><summary>Technical detail</summary>

**Evidence:** The morning message promises "enter within X% — skip if above". NVDA filled 8.98% above, so a user who OBEYED the instruction would have skipped a pick the bot bought. 9 of 96 observations breach (9.4%).

**Fix:** This is a TRUST defect, not a performance one. Either widen the published window in formatters.entry_window_pct to match measured reality, or make agent._build_premarket_gap_warnings warn on the gap. Do NOT re-hardcode 2 or 3 — that constant is the ONE definition and it has drifted before.

</details>

### [ACT] [TECHNICAL BUG] AMZN filled 4.0% outside the published entry window  *(n=96)*

`entry_window/AMZN/2026-08-28` · **open**, open 12d

AMZN was bought 4.0% above the price the morning message told people not to go past, so anyone who followed that instruction would have skipped a pick the bot itself took.

**Technical bug** — fixes broken behaviour; changes nothing about how picks are chosen.

<details><summary>Technical detail</summary>

**Evidence:** The morning message promises "enter within X% — skip if above". AMZN filled 4.0% above, so a user who OBEYED the instruction would have skipped a pick the bot bought. 9 of 96 observations breach (9.4%).

**Fix:** This is a TRUST defect, not a performance one. Either widen the published window in formatters.entry_window_pct to match measured reality, or make agent._build_premarket_gap_warnings warn on the gap. Do NOT re-hardcode 2 or 3 — that constant is the ONE definition and it has drifted before.

</details>

### [ACT] [TECHNICAL BUG] NOW filled 2.68% outside the published entry window  *(n=96)*

`entry_window/NOW/2026-08-31` · **open**, open 9d

NOW was bought 2.68% above the price the morning message told people not to go past, so anyone who followed that instruction would have skipped a pick the bot itself took.

**Technical bug** — fixes broken behaviour; changes nothing about how picks are chosen.

<details><summary>Technical detail</summary>

**Evidence:** The morning message promises "enter within X% — skip if above". NOW filled 2.68% above, so a user who OBEYED the instruction would have skipped a pick the bot bought. 9 of 96 observations breach (9.4%).

**Fix:** This is a TRUST defect, not a performance one. Either widen the published window in formatters.entry_window_pct to match measured reality, or make agent._build_premarket_gap_warnings warn on the gap. Do NOT re-hardcode 2 or 3 — that constant is the ONE definition and it has drifted before.

</details>

### [ACT] [TECHNICAL BUG] MMED filled 5.74% outside the published entry window  *(n=96)*

`entry_window/MMED/2026-09-02` · **open**, open 8d

MMED was bought 5.74% above the price the morning message told people not to go past, so anyone who followed that instruction would have skipped a pick the bot itself took.

**Technical bug** — fixes broken behaviour; changes nothing about how picks are chosen.

<details><summary>Technical detail</summary>

**Evidence:** The morning message promises "enter within X% — skip if above". MMED filled 5.74% above, so a user who OBEYED the instruction would have skipped a pick the bot bought. 9 of 96 observations breach (9.4%).

**Fix:** This is a TRUST defect, not a performance one. Either widen the published window in formatters.entry_window_pct to match measured reality, or make agent._build_premarket_gap_warnings warn on the gap. Do NOT re-hardcode 2 or 3 — that constant is the ONE definition and it has drifted before.

</details>

### [ACT] [TECHNICAL BUG] DOT filled 11.22% outside the published entry window  *(n=96)*

`entry_window/DOT/2026-09-08` · **open**, open 2d

DOT was bought 11.22% above the price the morning message told people not to go past, so anyone who followed that instruction would have skipped a pick the bot itself took.

**Technical bug** — fixes broken behaviour; changes nothing about how picks are chosen.

<details><summary>Technical detail</summary>

**Evidence:** The morning message promises "enter within X% — skip if above". DOT filled 11.22% above, so a user who OBEYED the instruction would have skipped a pick the bot bought. 9 of 96 observations breach (9.4%).

**Fix:** This is a TRUST defect, not a performance one. Either widen the published window in formatters.entry_window_pct to match measured reality, or make agent._build_premarket_gap_warnings warn on the gap. Do NOT re-hardcode 2 or 3 — that constant is the ONE definition and it has drifted before.

</details>

### [MEASURE] [TECHNICAL BUG] levels.target_below_entry on AMBA (historical)

`integrity/638eb7bc69ed` · **acknowledged**, open 18d

> Closed trades from 2026-08-03 — cannot be fixed retroactively. The GENERATOR gap is now closed: ai_analyzer._validate_and_clean_picks drops any pick whose target <= entry or stop >= entry before delivery (guard: TestUnwinnablePicksAreRejected, verified failing pre-fix). This shape can no longer ship.

A AMBA position has levels that cannot work: the trade is already closed, so this is a record of what shipped, not something fixable now.

**Technical bug** — fixes broken behaviour; changes nothing about how picks are chosen.

<details><summary>Technical detail</summary>

**Evidence:** target $78.54 is at or below entry $82.67 — this long position cannot reach its target, so it can only ever close at a loss

**Fix:** Historical: acknowledge it. It cannot be fixed retroactively. Worth confirming ai_analyzer._validate_and_clean_picks now rejects the shape so it cannot recur.

</details>

### [MEASURE] [TECHNICAL BUG] levels.target_below_entry on AMBA (historical)

`integrity/425fd0b45bd0` · **acknowledged**, open 18d

> Closed trades from 2026-08-03 — cannot be fixed retroactively. The GENERATOR gap is now closed: ai_analyzer._validate_and_clean_picks drops any pick whose target <= entry or stop >= entry before delivery (guard: TestUnwinnablePicksAreRejected, verified failing pre-fix). This shape can no longer ship.

A AMBA position has levels that cannot work: the trade is already closed, so this is a record of what shipped, not something fixable now.

**Technical bug** — fixes broken behaviour; changes nothing about how picks are chosen.

<details><summary>Technical detail</summary>

**Evidence:** target $79.74 is at or below entry $82.21 — this long position cannot reach its target, so it can only ever close at a loss

**Fix:** Historical: acknowledge it. It cannot be fixed retroactively. Worth confirming ai_analyzer._validate_and_clean_picks now rejects the shape so it cannot recur.

</details>

---

## Metrics (ongoing — never 'complete')

### [MEASURE] [METRIC] Stop distance distribution  *(n=117)*

**Evidence:** median 5.0% across 117 positions; 0 below the 3.0% threshold.

**Fix:** Context for the geometry metric — no action on its own.

### [MEASURE] [METRIC] Exit-reason mix  *(n=29)*

**Evidence:** {'manual': 5, 'stop': 20, 'target': 4} — stops hit 5.0x as often as targets.  By levels source — pick: 27 · stop: 1 · unrecorded: 33. Only `pick` speaks to the ENGINE's levels.

**Fix:** Judge the published levels on the `pick` slice ALONE. A high stop:target ratio there means stops are too tight; the same ratio in the fallback slice means the pick's levels did not bracket the fill — levels drifting from the live price by delivery time, which is a different fix.

### [MEASURE] [METRIC] Stop/target geometry on filled positions  *(n=29)*

**Evidence:** median stop 5.4% below entry, median target 11.2% above, reward:risk 2.09:1 across 29 filled positions. The walk-forward backtest measured real ledger picks at 10.3%/5.5% = 1.9:1 — compare against that, never config defaults.

**Fix:** No action while R:R stays near 1.9:1. If it drifts materially below, the stops are tightening relative to targets and will manufacture stop-outs — route any change through scripts/backtest_compare.py first.

### [MEASURE] [METRIC] Pick ledger has cleared the honesty gate  *(n=34)*

**Evidence:** 34 matured picks (gate 30).

**Fix:** Run scripts/evaluate_picks.py and read the picked-vs-control edge — the first evidence that can speak to SELECTION quality.
