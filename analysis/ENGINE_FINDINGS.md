# Engine findings — what to improve, and what the evidence supports

*Regenerated 2026-08-23 04:35 UTC by `scripts/analyze_engine.py`. Read this at the start of a session; it is the standing agenda.*

**How to read the tiers**

- **ACT** — actionable now. Binary or integrity findings where a single instance is enough to justify a change.
- **MEASURE** — descriptive and informative at small n. Safe to reason about; confirm direction before changing weights.
- **HOLD** — needs n≥30. Acting earlier is tuning on noise.

🔴 **The bot's win rate is never an input to engine changes.** Its trades are mechanical fills; in July they were steering real picks and that loop was cut. What it measures well is levels, reachability and integrity — not selection quality.

---
### [ACT] Picks filling outside the published entry window  *(n=48)*

**Evidence:** 2 of 48 observations (4.2%) filled beyond the window the morning message promises; worst 2.49%. Examples: ANET +2.43%, NWSA +2.15%. The message says "enter within X% — skip if above", so a breach means a user who obeyed would have skipped a pick the bot bought.

**Proposed action:** A TRUST defect, not a performance one — no win rate shows it. Either widen the published window to match reality, or warn on the gap. formatters.entry_window_pct is the ONE definition; do not re-hardcode 2 or 3.

### [ACT] Stops tighter than the noise threshold  *(n=63)*

**Evidence:** median stop 5.36% below entry across 63 positions; 1 tighter than the 3.0% threshold. KMI 2.99%.

**Proposed action:** A stop inside ordinary daily noise converts a good thesis into a stop-out. Check whether the ATR-based level (screener.suggested_stop_pct = 1.5x ATR%) collapsed for a low-volatility name, and floor it if so.

### [MEASURE] Position integrity violations  *(n=2)*

**Evidence:** 2 finding(s) (0 on LIVE positions): levels.target_below_entry×2

**Proposed action:** A live violation means a position that cannot win or was born stopped-out. Fix the level; then check whether the generator can still emit it — a historical-only finding needs no code change.

### [MEASURE] Stop/target geometry on filled positions  *(n=16)*

**Evidence:** median stop 6.4% below entry, median target 11.1% above, reward:risk 1.73:1 across 16 filled positions. The walk-forward backtest measured real ledger picks at 10.3%/5.5% = 1.9:1, so compare against that rather than config defaults.

**Proposed action:** If R:R drifts materially below ~1.9:1, the stops are tightening relative to targets and will manufacture stop-outs. Route any change through scripts/backtest_compare.py before shipping — a backtest win is not permission to ship, but a backtest loss is a reason not to.

### [MEASURE] Exit-reason mix  *(n=16)*

**Evidence:** {'manual': 5, 'stop': 8, 'target': 3} — stops are being hit 2.7x as often as targets.

**Proposed action:** Read WITH the geometry finding, not alone: _levels_for substitutes +/-5%/8% when a pick's levels do not bracket the actual fill, so part of this ratio is the fallback rather than the engine. Record whether levels were inherited or substituted to make this clean.

### [HOLD] Engine win-rate questions are not yet answerable  *(n=0)*

**Evidence:** 0 matured picks against a gate of 30. The synthetic bot's own record is deliberately excluded — it is a robot's mechanical fills, and feeding it back steers real recommendations.

**Proposed action:** Do not tune selection weights on anything below the gate. The descriptive findings above are safe to act on; win rate is not.

**Held until:** the ledger reaches 30 matured picks — below n=30 any conclusion is noise.
