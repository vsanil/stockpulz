# StockPulz — Pre-Launch Smoke Checklist

The automated suite (617 tests) **mocks all external I/O** (Telegram, Gist,
yfinance, Finnhub, Anthropic). It proves the *logic* is correct; it can't prove
the live services behave as expected. These manual checks walk the **real** live
paths once, so you go public having *seen* each work — not assumed it.

Run on the live app/bot. Order matters — later checks build on earlier ones.
Steps **1–3 are must-pass gates**; 4–9 are "verify before going wide".

> 🔑 If `CRON_SECRET` contains `#`, write it as `%23` in any URL or Render
> receives the wrong secret. (This exact bug has bitten before.)

---

## 1. Webhook secret token (defense-in-depth on the bot side)
- **Do:** In Telegram, send the bot `/start` (or any command).
- **Expect:** It replies normally.
- **Proves:** Telegram is sending the `X-Telegram-Bot-Api-Secret-Token` header
  *and* the verifier accepts it.
- **If it goes silent:** delete the `TELEGRAM_WEBHOOK_SECRET` env var on Render
  (instant revert) — auth still holds via the mini-app initData check.
- *(Only relevant once you've set `TELEGRAM_WEBHOOK_SECRET` + hit `/register`.)*

## 2. Mini-app cold open (auth HMAC + Gist read)
- **Do:** Fully close Telegram, reopen, launch the mini-app, tap **all 5 tabs**.
- **Expect:** Every tab loads; no "Couldn't load data".
- **Proves:** Telegram `initData` HMAC verifies for a fresh session; Gist reads succeed.

## 3. Morning run — the core product (cache → analysis → Telegram)
The morning run does **not** screen stocks live on Render (512MB OOMs on the
600-ticker screen — `_can_run_live_screener()` is `False` there). It reads the
**prescreener cache** from Gist. So a stale/missing cache → "no stock setups
today" + crypto/ETF only — **expected, not a bug**. Do it in two parts:

- **3a — ensure a fresh cache first.** Either run on a weekday *after* the
  nightly prescreener (03:00 / 07:00 UTC), **or** kick it manually (this relays
  to GitHub Actions — Render can't screen locally), then **wait ~3–5 min**:
  ```
  https://stock-agent-enqx.onrender.com/trigger/prescreener?secret=<CRON_SECRET>&force=true
  ```
- **3b — trigger the morning run:**
  ```
  https://stock-agent-enqx.onrender.com/trigger/morning?secret=<CRON_SECRET>&force=true
  ```
- **Expect:** within a minute or two, the morning picks message arrives in
  Telegram **with real stock tickers + prices** (not just crypto).
- **Proves:** the full delivery chain — the single most important path.
  (`force=true` bypasses the once-a-day guard; it does **not** force a live screen.)

## 4. A pick renders with a live price + chart
- **Do:** In Picks, tap a stock pick to expand; open its chart.
- **Expect:** Current price looks right; chart draws daily bars.
- **Proves:** yfinance/Polygon OHLCV path + live-price overlay + the 10-min cache.

## 5. Crypto sanity (the recurring bug class)
- **Do:** Look at any crypto in picks/watchlist (e.g. BTC).
- **Expect:** A realistic price — **not ~$28**, not `$0.00`.
- **Proves:** the `-USD` suffix + non-positive-price guards hold on the real feed.

## 6. Log a position (real write to Gist → read back)
- **Do:** Log a position via the mini-app ("💵 Total invested", pick a past date).
- **Expect:** It appears in Portfolio with a sane entry price & P&L; the
  date-close lookup returns a real close.
- **Proves:** the clobber-safe write path + `close_on_date` against live data.

## 7. Settings save with a messy value (the parse_money fix)
- **Do:** Set a budget to `$1.5k` and a stop to `8%`; reopen Settings.
- **Expect:** Saved as `1500` and `8` — not blank, not silently dropped.
- **Proves:** `parse_money` on the real save round-trip.

## 8. Price alert (real trigger loop)
- **Do:** Set an alert just above/below a ticker's current price so it fires soon.
- **Expect:** You get the alert when it crosses; no false `$0.00` fires.
- **Proves:** the alert manager's live-price fetch + atomic alert write + guard.

## 9. Define / LLM endpoint (rate-limit + real Anthropic)
- **Do:** Tap a financial term for its definition; then tap rapidly ~25 times.
- **Expect:** Definition appears (real Haiku call); after ~20/min you get a
  "give it a minute" rate-limit message.
- **Proves:** the LLM path works *and* the cost guard actually triggers.

---

### If anything fails
Note **which step** and **what you saw** — that pinpoints the exact layer
(auth / Telegram / Gist / price feed / LLM). Steps 1–3 are the gates; 4–9 are
"verify before going wide".
