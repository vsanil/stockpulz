# Engine findings — the standing agenda

*Regenerated 2026-09-22 00:41 UTC by `scripts/analyze_engine.py`. Read this at the START of a session and work the open findings.*

## Open: 0 finding(s) needing a decision

_Nothing open. Every addressable finding has been resolved._

### Decided, still present (7)

*You have already ruled on these — they are not in the worklist. They will clear themselves the day the condition disappears.*

- `entry_window/NVDA/2026-08-27` — **acknowledged** — NVDA filled 8.98% outside the published entry window
- `entry_window/AMZN/2026-08-28` — **acknowledged** — AMZN filled 4.0% outside the published entry window
- `entry_window/NOW/2026-08-31` — **acknowledged** — NOW filled 2.68% outside the published entry window
- `entry_window/MMED/2026-09-02` — **acknowledged** — MMED filled 5.74% outside the published entry window
- `entry_window/DOT/2026-09-08` — **acknowledged** — DOT filled 11.22% outside the published entry window
- `integrity/638eb7bc69ed` — **acknowledged** — levels.target_below_entry on AMBA (historical) · _Closed trades from 2026-08-03 — cannot be fixed retroactively. The GENERATOR gap is now closed: ai_analyzer._validate_and_clean_picks drops any pick whose target <= entry or stop >= entry before delivery (guard: TestUnwinnablePicksAreRejected, verified failing pre-fix). This shape can no longer ship._
- `integrity/425fd0b45bd0` — **acknowledged** — levels.target_below_entry on AMBA (historical) · _Closed trades from 2026-08-03 — cannot be fixed retroactively. The GENERATOR gap is now closed: ai_analyzer._validate_and_clean_picks drops any pick whose target <= entry or stop >= entry before delivery (guard: TestUnwinnablePicksAreRejected, verified failing pre-fix). This shape can no longer ship._

**To close one:** implement the fix, or record a decision on the **/admin** dashboard (Engine findings card) or via `scripts/findings.py` (`status`: `acknowledged` | `wont_fix`, plus a `note` saying why). A finding whose condition DISAPPEARS is marked `resolved` automatically — that is the intended path.

🔁 **A finding marked `fixed` that is still present is REOPENED.** Otherwise 'fixed' silently means 'hidden' while the defect is live.

🔴 **Engine changes are never recommended from the bot's win rate.** Mechanical fills; in July they were steering real picks and that loop was cut. Anything needing n≥30 is HELD with its clearing date.

---

## Findings

### [DECIDED] [TECHNICAL BUG] NVDA filled 8.98% outside the published entry window  *(n=114)*

`entry_window/NVDA/2026-08-27` · **acknowledged**, open 25d

NVDA was bought 8.98% above the price the morning message told people not to go past, so anyone who followed that instruction would have skipped a pick the bot itself took.

**Technical bug** — fixes broken behaviour; changes nothing about how picks are chosen.

<details><summary>Technical detail</summary>

**Evidence:** The morning message promises "enter within X% — skip if above". NVDA filled 8.98% above, so a user who OBEYED the instruction would have skipped a pick the bot bought. 10 of 114 observations breach (8.8%).

**You ruled: acknowledged.** No action is outstanding. For the record, the suggested fix was: HISTORICAL — acknowledge it; it cannot be un-made. This fill predates 2026-09-15, when the bot began both OBEYING the window (synthetic_user._entry_breach skips an out-of-window pick, and records the skip so the metric cannot go quiet) and buying punctually at 08:00 ET. Before that it bought 4-6 h late, so this fill measured its own execution lag as much as the engine's levels. Do NOT widen formatters.entry_window_pct 'to match measured reality' — proposed, investigated and rejected 2026-09-13: it would legitimise the bad fill and weaken a published promise.

</details>

### [DECIDED] [TECHNICAL BUG] AMZN filled 4.0% outside the published entry window  *(n=114)*

`entry_window/AMZN/2026-08-28` · **acknowledged**, open 24d

AMZN was bought 4.0% above the price the morning message told people not to go past, so anyone who followed that instruction would have skipped a pick the bot itself took.

**Technical bug** — fixes broken behaviour; changes nothing about how picks are chosen.

<details><summary>Technical detail</summary>

**Evidence:** The morning message promises "enter within X% — skip if above". AMZN filled 4.0% above, so a user who OBEYED the instruction would have skipped a pick the bot bought. 10 of 114 observations breach (8.8%).

**You ruled: acknowledged.** No action is outstanding. For the record, the suggested fix was: HISTORICAL — acknowledge it; it cannot be un-made. This fill predates 2026-09-15, when the bot began both OBEYING the window (synthetic_user._entry_breach skips an out-of-window pick, and records the skip so the metric cannot go quiet) and buying punctually at 08:00 ET. Before that it bought 4-6 h late, so this fill measured its own execution lag as much as the engine's levels. Do NOT widen formatters.entry_window_pct 'to match measured reality' — proposed, investigated and rejected 2026-09-13: it would legitimise the bad fill and weaken a published promise.

</details>

### [DECIDED] [TECHNICAL BUG] NOW filled 2.68% outside the published entry window  *(n=114)*

`entry_window/NOW/2026-08-31` · **acknowledged**, open 21d

NOW was bought 2.68% above the price the morning message told people not to go past, so anyone who followed that instruction would have skipped a pick the bot itself took.

**Technical bug** — fixes broken behaviour; changes nothing about how picks are chosen.

<details><summary>Technical detail</summary>

**Evidence:** The morning message promises "enter within X% — skip if above". NOW filled 2.68% above, so a user who OBEYED the instruction would have skipped a pick the bot bought. 10 of 114 observations breach (8.8%).

**You ruled: acknowledged.** No action is outstanding. For the record, the suggested fix was: HISTORICAL — acknowledge it; it cannot be un-made. This fill predates 2026-09-15, when the bot began both OBEYING the window (synthetic_user._entry_breach skips an out-of-window pick, and records the skip so the metric cannot go quiet) and buying punctually at 08:00 ET. Before that it bought 4-6 h late, so this fill measured its own execution lag as much as the engine's levels. Do NOT widen formatters.entry_window_pct 'to match measured reality' — proposed, investigated and rejected 2026-09-13: it would legitimise the bad fill and weaken a published promise.

</details>

### [DECIDED] [TECHNICAL BUG] MMED filled 5.74% outside the published entry window  *(n=114)*

`entry_window/MMED/2026-09-02` · **acknowledged**, open 20d

MMED was bought 5.74% above the price the morning message told people not to go past, so anyone who followed that instruction would have skipped a pick the bot itself took.

**Technical bug** — fixes broken behaviour; changes nothing about how picks are chosen.

<details><summary>Technical detail</summary>

**Evidence:** The morning message promises "enter within X% — skip if above". MMED filled 5.74% above, so a user who OBEYED the instruction would have skipped a pick the bot bought. 10 of 114 observations breach (8.8%).

**You ruled: acknowledged.** No action is outstanding. For the record, the suggested fix was: HISTORICAL — acknowledge it; it cannot be un-made. This fill predates 2026-09-15, when the bot began both OBEYING the window (synthetic_user._entry_breach skips an out-of-window pick, and records the skip so the metric cannot go quiet) and buying punctually at 08:00 ET. Before that it bought 4-6 h late, so this fill measured its own execution lag as much as the engine's levels. Do NOT widen formatters.entry_window_pct 'to match measured reality' — proposed, investigated and rejected 2026-09-13: it would legitimise the bad fill and weaken a published promise.

</details>

### [DECIDED] [TECHNICAL BUG] DOT filled 11.22% outside the published entry window  *(n=114)*

`entry_window/DOT/2026-09-08` · **acknowledged**, open 14d

DOT was bought 11.22% above the price the morning message told people not to go past, so anyone who followed that instruction would have skipped a pick the bot itself took.

**Technical bug** — fixes broken behaviour; changes nothing about how picks are chosen.

<details><summary>Technical detail</summary>

**Evidence:** The morning message promises "enter within X% — skip if above". DOT filled 11.22% above, so a user who OBEYED the instruction would have skipped a pick the bot bought. 10 of 114 observations breach (8.8%).

**You ruled: acknowledged.** No action is outstanding. For the record, the suggested fix was: HISTORICAL — acknowledge it; it cannot be un-made. This fill predates 2026-09-15, when the bot began both OBEYING the window (synthetic_user._entry_breach skips an out-of-window pick, and records the skip so the metric cannot go quiet) and buying punctually at 08:00 ET. Before that it bought 4-6 h late, so this fill measured its own execution lag as much as the engine's levels. Do NOT widen formatters.entry_window_pct 'to match measured reality' — proposed, investigated and rejected 2026-09-13: it would legitimise the bad fill and weaken a published promise.

</details>

### [DECIDED] [TECHNICAL BUG] levels.target_below_entry on AMBA (historical)

`integrity/638eb7bc69ed` · **acknowledged**, open 30d

> Closed trades from 2026-08-03 — cannot be fixed retroactively. The GENERATOR gap is now closed: ai_analyzer._validate_and_clean_picks drops any pick whose target <= entry or stop >= entry before delivery (guard: TestUnwinnablePicksAreRejected, verified failing pre-fix). This shape can no longer ship.

A AMBA position has levels that cannot work: the trade is already closed, so this is a record of what shipped, not something fixable now.

**Technical bug** — fixes broken behaviour; changes nothing about how picks are chosen.

<details><summary>Technical detail</summary>

**Evidence:** target $78.54 is at or below entry $82.67 — this long position cannot reach its target, so it can only ever close at a loss

**You ruled: acknowledged.** No action is outstanding. For the record, the suggested fix was: Historical: acknowledge it. It cannot be fixed retroactively. Worth confirming ai_analyzer._validate_and_clean_picks now rejects the shape so it cannot recur.

</details>

### [DECIDED] [TECHNICAL BUG] levels.target_below_entry on AMBA (historical)

`integrity/425fd0b45bd0` · **acknowledged**, open 30d

> Closed trades from 2026-08-03 — cannot be fixed retroactively. The GENERATOR gap is now closed: ai_analyzer._validate_and_clean_picks drops any pick whose target <= entry or stop >= entry before delivery (guard: TestUnwinnablePicksAreRejected, verified failing pre-fix). This shape can no longer ship.

A AMBA position has levels that cannot work: the trade is already closed, so this is a record of what shipped, not something fixable now.

**Technical bug** — fixes broken behaviour; changes nothing about how picks are chosen.

<details><summary>Technical detail</summary>

**Evidence:** target $79.74 is at or below entry $82.21 — this long position cannot reach its target, so it can only ever close at a loss

**You ruled: acknowledged.** No action is outstanding. For the record, the suggested fix was: Historical: acknowledge it. It cannot be fixed retroactively. Worth confirming ai_analyzer._validate_and_clean_picks now rejects the shape so it cannot recur.

</details>

---

## Metrics (ongoing — never 'complete')

### [MEASURE] [METRIC] Stop distance distribution  *(n=137)*

**Evidence:** median 5.0% across 137 positions; 0 below the 3.0% threshold.

**Fix:** Context for the geometry metric — no action on its own.

### [MEASURE] [METRIC] Exit-reason mix  *(n=34)*

**Evidence:** {'manual': 5, 'stop': 25, 'target': 4} — stops hit 6.2x as often as targets.  By levels source — pick: 65 · stop: 15 · unrecorded: 64. Only `pick` speaks to the ENGINE's levels.

**Fix:** Judge the published levels on the `pick` slice ALONE. A high stop:target ratio there means stops are too tight; the same ratio in the fallback slice means the pick's levels did not bracket the fill — levels drifting from the live price by delivery time, which is a different fix.

### [MEASURE] [METRIC] Stop/target geometry on filled positions  *(n=34)*

**Evidence:** median stop 5.1% below entry, median target 10.8% above, reward:risk 2.11:1 across 34 filled positions. The walk-forward backtest measured real ledger picks at 10.3%/5.5% = 1.9:1 — compare against that, never config defaults.

**Fix:** No action while R:R stays near 1.9:1. If it drifts materially below, the stops are tightening relative to targets and will manufacture stop-outs — route any change through scripts/backtest_compare.py first.

### [MEASURE] [METRIC] Synthetic trader's book vs its SPY twin (identical cash flows)  *(n=1)*

The bot's simulated $10k book, traded the way a sized, rule-obeying user would, compared with putting the same dollars into SPY on the same days.

<details><summary>Technical detail</summary>

**Evidence:** bot $10,000.00 (+0.00%) vs SPY twin $10,000.00 (+0.00%) on identical cash flows since 2026-09-20 · 1 trading day(s) · alpha +0.00 pts · max drawdown bot 0.0% / twin 0.0% · 4 buys, 0 sells · only 1 trading day(s) — directional, not conclusive

**Fix:** Nothing to change from this alone. A strategy earns a change through the tournament (Phase 2), then propose → approve on /admin — never from one book's curve.

</details>

### [MEASURE] [METRIC] Pick ledger has cleared the honesty gate  *(n=81)*

**Evidence:** 81 matured picks (gate 30).

**Fix:** Run scripts/evaluate_picks.py and read the picked-vs-control edge — the first evidence that can speak to SELECTION quality.
