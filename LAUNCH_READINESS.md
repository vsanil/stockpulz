# StockPulz — Launch Readiness & Known Limitations

An honest, engineer-to-owner assessment. No marketing. Read this before sharing widely.

---

## The one-paragraph truth
The app's **core is solid and its calculations are correct** — auth, storage safety, position sizing, P&L, alerts, backtest math, and delivery are all tested (649 unit tests) and now exercised **daily against live data** by the canary (`scripts/canary.py`). What remains are **free-tier infrastructure ceilings** and the normal reality that *no live app is bug-free*. The right posture is **not "it's perfect"** — it's **"issues are now rare, caught fast (canary), and low-impact."** Ship to a small group first, watch, then widen.

---

## ✅ What's solid (tested + canary-covered)
- **Security** — mini-app requests are HMAC-verified (`initData`); the webhook has an optional secret token; no forgeable `chat_id`. (Verified live.)
- **Storage safety** — a transient read can no longer erase all users' data; per-user saves are lock-serialized.
- **Calculations** — position sizing, %-P&L, upside, stop distances, backtest win-rate (now non-overlapping), fractional-crypto sizing. The canary re-verifies these daily.
- **Delivery** — morning picks + all heavy jobs run on GitHub Actions (7 GB); Render only relays + serves. The morning OOM outage is fixed.
- **Money-path correctness** — paper/real logging store the per-share **price**, not the $ total (the old bug); alerts edit via one atomic replace; a position past its stop now gets a loud STOP-HIT alert + badge.

## ⚠️ Known limitations (free-tier ceilings — real, mostly mitigated)
1. **Cold starts.** Render free tier sleeps after ~15 min idle. Mitigated by a **24/7 keep-warm** ping, but GitHub cron can lag a few minutes, so a *rare* first message after a gap may still wait ~30–60 s for a boot. **Permanent fix = paid Render (~$7/mo), no sleep.**
2. **CoinGecko rate limits.** Crypto prices share a free-tier limit. Mitigated with a shared 60 s cache + backoff on the hot paths; a couple of minor call sites still fetch raw and could 429 under load.
3. **Storage races.** The Gist "database" is last-writer-wins across processes (web vs. scheduled jobs). Same-user simultaneous writes can rarely lose one update (e.g. a buy-count). **Permanent fix = a real DB (Supabase is scaffolded).**
4. **Manual user approval.** New users land in a pending queue for you to approve — by your choice. That's a *you* bottleneck as invites grow; revisit auto-approve if it gets heavy.
5. **External data.** yfinance / Finnhub / CoinGecko occasionally return bad/late data; guards reject non-positive prices, but a wrong-but-positive quote from an upstream can slip through.
6. **Not financial advice.** The app says so, but make sure friends understand these are algorithmic signals, not advice.

## 🔧 "If X breaks, it's probably Y"
- **Bot silent / slow first reply** → cold start (see #1) or a Render redeploy in progress. Usually self-heals in <1 min.
- **No morning picks** → check the GitHub Actions "morning" run + that the prescreener cache is fresh. Recover: `/trigger/morning?secret=…&force=true`.
- **New user clicks share link, no reply** → cold start ate their first `/start` (see #1). They should retry; keep-warm makes this rare now.
- **Crypto price looks wrong / missing** → CoinGecko 429 (see #2). Non-crypto is unaffected.
- **A number looks off in a message** → the **canary** verifies the math daily; if it's green, suspect a display edge case, not a calc error. Send me the screenshot + the day's canary report.

## 🐤 Your safety net: the daily canary
- Runs **7:30 AM ET daily** (GitHub Actions), exercises every path against live data, verifies the math, and **DMs you a pass/fail report**. Non-destructive.
- **Green report = every path + calculation worked that morning.** A red report names the exact failed check — forward it and it gets fixed fast.
- This is what turns "I keep finding issues" into "issues find *me*, before users."

## 🚀 Recommended rollout (don't skip the small beta)
1. **Watch the canary stay green for 2–3 days.** That's the real stability signal.
2. **Verify on your phone** once Render redeploys: custom alert, pick alert state, "Buy More", 2-button keyboard.
3. **Run `LAUNCH_CHECKLIST.md` once** (the manual live-path walk-through).
4. **Share with 2–3 friends first.** Watch a few days, fix what surfaces.
5. **Then widen.** And strongly consider **paid Render** before real volume — it removes the single biggest reliability ceiling (#1).

## The honest bottom line
It's in genuinely good shape and far better instrumented than a week ago — but "picture-perfect, zero-touch" isn't a real bar for any live app. Ship it **small**, let the **canary** watch it, fix on red, and widen once it's earned your trust. That's how you launch confidently without pretending it's flawless.
