## Self-evolving rules

- After every session, if a new bug pattern, recurring mistake, or important constraint was discovered, add it to this file
- If a rule was repeatedly violated despite being listed, rewrite it more explicitly
- If a new API, library, or architectural decision was made, document it here so future sessions start with full context
- This file is the source of truth for how this project should be built — keep it current

## graphify

This project has a graphify knowledge graph at graphify-out/.

Rules:
- Before answering architecture or codebase questions, read graphify-out/GRAPH_REPORT.md for god nodes and community structure
- If graphify-out/wiki/index.md exists, navigate it instead of reading raw files
- For cross-module "how does X relate to Y" questions, prefer `graphify query "<question>"`, `graphify path "<A>" "<B>"`, or `graphify explain "<concept>"` over grep — these traverse the graph's EXTRACTED + INFERRED edges instead of scanning files
- After modifying code files in this session, run `graphify update .` to keep the graph current (AST-only, no API cost)

## Memory / OOM architecture (settled Jun 12, 2026 — do not relitigate)

- **Render 512MB CANNOT run the 600-ticker screener in any form** — full agent.py OOMs, thin run_prescreener.py OOMs. Proven empirically 5+ times. Do not try again.
- **Prescreener runs on GitHub Actions** (7GB): daily_run.yml schedules 03:00 UTC primary + 07:00 UTC backup, both → prescreener mode. Cache save is idempotent.
- **Render /trigger/prescreener is a relay** — dispatches the GH workflow via GITHUB_TOKEN; never spawns locally. cron-job.org keeps hitting it unchanged (redundant third trigger).
- **`_can_run_live_screener()` guard in agent.py**: morning run NEVER falls back to the live screener when RENDER env var is set (Render sets it automatically). Cache miss → admin-only alert + crypto/ETF picks continue; users see normal "no stock setups today" rendering.
- **Morning DELIVERY also relays to GitHub Actions (FIXED Jul 1, 2026).** The old belief "morning run WITH cache is light, well under 512MB" was FALSIFIED — even with the cache, run_morning (ETF screen + Claude analysis w/ per-ticker sentiment + signal cache + personalised send) OOMs Render's 512MB. Symptom: `cron_last_morning` updates daily (process starts, sets it) but `last_morning_run` + `picks._saved_date` freeze (killed mid-run, before save_picks), and the per-day guard then blocks retries → users silently get NO morning picks for days. Render logged "Ran out of memory (used over 512MB)" at 11:01 UTC. Fix: `webhook.trigger_morning` now RELAYS to GH Actions (dispatches daily_run.yml `run_mode=morning`, mirrors /trigger/prescreener) instead of `subprocess.Popen(agent.py)`. **Rule: any run that does screening/Claude/ETF work must execute on GH Actions (7GB), never spawn on Render — Render only relays + serves the web/API.** NOTE: deliberately did NOT add a GH `0 11 * * 1-5` schedule cron — that would race the cron-job.org relay and double-send (delivery isn't idempotent, unlike the prescreener cache). `force=true` propagates through the relay → daily_run.yml `force` input → `FORCE_MORNING` env for recovery.
- To confirm a morning-path change without spamming users: reproduce run_morning locally with `requests.post`/`requests.patch` stubbed (blocks ALL Telegram sends + Gist writes) and `_screener_cache_with_retry` stubbed to the real cache — it runs the full path read-only. (This is how the Jul 1 OOM was proven to be environmental, not a code bug.)
- Manual cache recovery: `python3 run_prescreener.py` locally, or `/trigger/prescreener?secret=…&force=true`. Manual morning recovery: `/trigger/morning?secret=…&force=true` (relays to GH with FORCE_MORNING).
- An unmapped schedule in daily_run.yml defaults to prescreener, NEVER morning — must not send user-facing picks at odd hours.

### Bug pattern: function-local datetime imports + bulk refactors
- The Jun 8 utcnow() cleanup replaced `datetime.utcnow()` → `datetime.now(timezone.utc)` but missed adding `timezone` to FUNCTION-LOCAL `from datetime import …` lines in config_manager.py. The NameError was swallowed by catch-all excepts → `load_screener_cache` and `load_macro_cache` NEVER worked in production → every morning ran the full screener → OOM → no picks.
- py_compile does NOT catch NameErrors inside function bodies. Catch-all `except Exception` around cache loaders hides them completely.
- **Rule: every save/load cache pair MUST have a save→load round-trip test** (see TestScreenerCache / TestMacroCache in tests/test_config_manager.py).
- **Rule: after any bulk find-replace refactor, grep every function-local import scope the replacement touched** — module-level imports are not enough.

### Bug pattern: function-local import SHADOWS a module-level name → UnboundLocalError (Jul 3, 8 bugs)
- A name imported at module level (e.g. `os`, `get_user_config`, `_fetch_live_price`, `load_user_trade_log`, `update_user_config`) that is ALSO re-imported *inside* a function becomes **local to that entire function** (Python binds it for the whole scope). Any code path that uses the name BEFORE the local import line raises `UnboundLocalError` — swallowed by the command catch-all into "⚠️ Something went wrong", so it looks like a transient glitch, not a bug. It only fires on the branch that reaches the early use, so it hides for months.
- The full_sweep (Jul 3) found 8 of these live: `/positions /history /dashboard /missed` (cmd_trades `import csv, io, os` at the /export branch shadowed module `os`), `/size` (cmd_market local `from cmd_helpers import _fetch_live_price`), the **no-picks morning broadcast** (agent `run_morning` — every user's "no setups today" send silently failed), `run_confirmation`, `_handle_pending_reply`, `handle_callback_query`, `_cmd_admin`, `_cmd_settings`.
- **Fix pattern: DELETE the redundant function-local import** (the name is already module-level). Only keep a local import for names that are NOT module-level (e.g. cmd_settings kept `load_user_trade_log`, cmd_admin kept `save_user_trade_log`).
- **Guard: `tests/test_no_shadow_imports.py`** does a scope-aware AST scan of every project `.py` and FAILS on any use-before-local-import where the name is module-level. Keep it at zero. **Rule: never re-import a module-level name inside a function — if you need it, it's already imported; a local re-import is a latent UnboundLocalError.** `py_compile` does NOT catch this (runtime error); only the AST scan / a test that actually calls the command does.

### Bug pattern: price-fetch failures must never reach price logic (Jun 23)
- **Non-positive price = failed fetch, never a real quote.** yfinance `fast_info.last_price` can return `0.0` or `nan` on a bad/exotic symbol (e.g. HYPE). `if price:` rejects 0/None but **`nan` is truthy** — it slips through. A `$0.00` price made every "below" alert fire ("HYPE is now $0.00", because `0.00 <= target`). Guard with `price and price > 0` at EVERY return point in `_current_price`, AND at the trigger site (`price_alert_manager.py` check_alerts: skip `current is None or current <= 0`). Defense in depth — guard at both the source and the consumer.

### Bug pattern: `> 0` is NOT enough — tiny-positive garbage on a market holiday (Jul 3, sequel to Jun 23)
- The Jun 23 guard (`price > 0` / `<= 0`) was under-generalised: it fixed the exact symptom (`$0.00`) not the class. On the **Jul 3 US-holiday** the feed returned tiny POSITIVE garbage — **TSLA `$0.01`, UNI/WEN/SHOP ~`$0.004`** (displays `$0.00`) — which passes `> 0` and then trivially satisfies any "below $X" target → a cascade of false "now $0.00 · −100%" alerts, a false EOD `▼100% stop hit`, a false `🔴 STOP HIT` sell nudge, and a collapsed portfolio total that made two positions each read "49% of your portfolio". Fired live while the canary was green (nothing injected a bad price).
- **Fix = validate against a REFERENCE, not just `> 0`.** `market_data.plausible_price(price, reference, lo=0.1, hi=10.0)` — the ONE class-level guard: rejects None/NaN/≤0 AND anything outside `[reference*0.1, reference*10]` (a >90% collapse / >10x spike between checks is a bad fetch). Reference = the price we last knew for real: an alert's `price_at_set` (fallback `target`), a position's `entry`. Applied at the notification/headline paths: `price_alert_manager.check_alerts`, `formatters` EOD `_row` + the portfolio-P&L aggregate loop, `agent._check_portfolio_health` (implausible → fall back to entry/cost-basis, so no false concentration/nudge), `agent._check_hold_or_fold` (no false STOP-HIT sell signal).
- **Rule: any NEW code that compares a live price to a stop/target/entry, or leads a P&L number, must gate on `plausible_price(price, reference)` — never a bare `> 0` / `_is_pos`.** Guard/regression: `TestImplausiblePriceGuard` (alerts), `TestEODImplausiblePriceGuard` (EOD), `TestPlausiblePrice` (unit), and canary `check_price_guard` (injects `$0.01`, keeps the monitor honest). RESIDUAL (documented, lower-risk — display-only, not a notification): pre-market card crypto row + watchlist move-alert still use the weaker `if price` — tighten with `plausible_price` if they ever surface garbage.
- **Crypto needs the `-USD` suffix in EVERY yfinance call that takes user tickers.** A bare `yf.download("BTC")` resolves to an unrelated ~$28 instrument, not BTC-USD (~$27k). The Jun 8 sweep converted "all 5 yf.download calls" but **missed the watchlist big-move check** (agent.py) → BTC priced at $28. Fixed by extracting `_yf_symbol_map(tickers)` (BTC→BTC-USD via `_SYMBOL_TO_CG_ID`) and routing the move-check through it. **Rule: any new `yf.download`/`yf.Ticker` on watchlist/position/user tickers MUST go through `_yf_symbol_map` or `_download_prices` — never raw `" ".join(tickers)`.** Note: position card uses `_download_prices` (correct); only standalone download sites are at risk.

### Single sources of truth (consolidated Jun 26 — do not re-fork)
- **Crypto symbols: `price_checker._SYMBOL_TO_CG_ID` is the ONE map** (symbol→CoinGecko id) and `price_checker.CRYPTO_SYMBOLS = frozenset(_SYMBOL_TO_CG_ID)` is the ONE set. There used to be 8 divergent hardcoded lists; a picked coin (HYPE, TON) worked in one feature and broke in another. Every module imports `CRYPTO_SYMBOLS` now (market_data, chart_generator, cmd_helpers, price_alert_manager, webhook `_CHART_CRYPTO`, cmd_market `/size`, ai_analyzer `_KNOWN_CRYPTO`). Frontend mirror: `isCryptoTicker()` in index.html. **Rule: never add a new hardcoded crypto literal — import the canonical set; to add a coin, add it to `_SYMBOL_TO_CG_ID` with its CG id.**
- **Watchlist: trade_log is the monitored store.** `webhook._load_watchlist` (union of trade_log ∪ user_config) and `_save_watchlist` (writes BOTH) keep the Watch tab and Settings tab in sync — they used to be separate stores so a ticker added in one vanished from the other. Bot `/track` writes trade_log directly. **Rule: any watchlist read/write goes through `_load_watchlist`/`_save_watchlist`.**
- **Default budgets: `config_manager.DEFAULT_STOCK_BUDGET` (200) / `DEFAULT_CRYPTO_BUDGET` (50).** `/size` used to fall back to 500/100 (double everyone else). All fallbacks derive from these.
- **`_is_pos(x)` (agent.py): the price guard.** Returns True only for a real positive number (rejects None/0/neg/nan — `if not x` lets nan through since nan is truthy). Use at every nudge/alert that divides by or compares a price.
- **`STOP_PROXIMITY_PCT` (agent.py, 3%): one "near stop" radius** for the NEAR STOP badge and EOD health insight.
- Position entry: the bot `buy_pick` callback parses shares as `float` (was `int(...) if .isdigit() else 1`, which collapsed fractional crypto to 1 whole coin). Mini-app Position Sizer + Paper Trade use fractional crypto sizing. Both buy forms (Log Position + Paper Trade) use the total-invested/exact-$ toggle.

### More single sources of truth (second audit, Jun 26)
- **Dates: `config_manager.et_today()` (US/Eastern date) for ALL dedup keys, idempotency guards, date stamps, and day-diffs.** Bare `date.today()` is UTC on Render → rolls over at 7-8 PM ET, so evening alerts double-fired/suppressed and date stamps were off by one. Ranged historical lookbacks may stay on `date.today()`. **Rule: any new dedup key / date stamp / "days held" uses `et_today()`.**
- **Crypto/ETF/commodity visibility: `config_manager.shows_crypto(cfg)` / `shows_etfs(cfg)`** reconcile the mini-app `assets` key with the bot's `show_crypto`/`show_etfs` toggles. Broadcast (formatters) AND the picks API use them, so app and morning message agree. Don't read `show_crypto`/`assets` raw.
- **`asset_type` is stored, not re-derived.** `add_holding` takes an `asset_type` param (mini-app forwards it), types by pick section (stock/crypto/etf/commodity), and falls back to `CRYPTO_SYMBOLS` — never a blind "stock" default. Legacy `asset_type=None` positions classify via `CRYPTO_SYMBOLS`, not digit/length heuristics.
- **`get_dynamic_pick_counts` honors `max_stock_picks`/`max_crypto_picks`** (total caps); `add_holding` fills a missing stop/target from `stop_loss_pct`/`target_gain_pct` (AI/explicit always wins). Settings that were written-but-ignored now do something.
- **Callback bug class**: `be_stop` passed field `"stop"` (only `"stop_loss"` exists) + a string into `round()`. When wiring a callback to `_execute_update_level`, the field must be `"stop_loss"`/`"target_price"` and the price a float. Duplicate-alert ValueErrors must be caught and returned as `⚠️ {e}`, never left to the webhook catch-all ("Something went wrong").
- **Money parsing: `cmd_helpers.parse_money(raw)`** — one tolerant parser (strips `$ , %`, handles `k`/`m`, returns float|None) for ALL user-typed amounts (settings stop/target/budget/portfolio, change_buy_amount, paper_buy). Don't hand-strip a subset inline. The two settings-update endpoints are de-diverged: `/settings/update` is a superset allowlist of `/update_settings`.
- **Frontend crypto set auto-syncs**: picks `_meta.crypto_symbols` serves `price_checker.CRYPTO_SYMBOLS`; `isCryptoTicker()` overlays it on load. The hardcoded JS literal is only the pre-load fallback — to add a coin, just add it to `_SYMBOL_TO_CG_ID`.
- **Fractional crypto in nudge text**: any "sell X of your Y shares" / partial-profit text must format shares fractional-safe (`int(x) if is_integer else %g`), never `max(1, int(...))` — that printed "Sell 1 of your 0 shares" for a 0.0008 BTC position.

### Bug pattern: NaN/Infinity in an API response breaks the frontend (Jun 27)
- Flask `jsonify` emits the literal `NaN`/`Infinity` (Python `json` allows them), but those are **invalid JSON** — the browser's `response.json()` throws, and the tab silently shows "Couldn't load data". This took down the Performance tab: `build_community_stats` returned `alpha`/`spy_return_30d = NaN` (SPY fetch produced a NaN close; `if x is not None` doesn't catch NaN — same class as `_is_pos`).
- **Fix is app-wide**: `webhook._NanSafeJSONProvider` (set as `app.json`) runs `_clean_nan` on EVERY jsonify response, mapping NaN/Inf → null — so no endpoint can leak NaN, not just the ones we remember to guard. Also fix at source where practical (`math.isfinite`/`x == x` before `round()`).
- **SPY benchmark: `performance_tracker._spy_return(period)`** is the one NaN-safe SPY fetch — `.dropna()` skips yfinance's trailing NaN bar (the actual cause of the blank `alpha`/`spy_return_30d`). All 3 SPY fetch sites route through it.
- **Decorator gotcha**: never insert a helper `def` between `@app.route(...)` and its view function — the decorator then wraps the helper and the route 500s. Put module helpers ABOVE the decorator.

### Bug pattern: the "dynamic" stock universe was silently frozen (Jun 28)
- `screener.get_stock_universe()` is meant to fetch the LIVE S&P 500 + Nasdaq-100 + MidCap 400 constituents so new public companies (e.g. SPCX after its Jun 2026 IPO) auto-enter the screener — the app's freshness does NOT depend on the model's training cutoff. But all sources had broken: the datahub.io CSV URL 404s, and **Wikipedia 403s any request without a browser `User-Agent`**. So it silently fell back to the 423-ticker hardcoded `FALLBACK_TICKERS` — the universe was frozen.
- Fix: S&P 500 from the GitHub mirror `raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv`; Wikipedia via `_wiki_symbols(url)` which sets `_BROWSER_UA` and **scans all tables for the one with a symbol/ticker column + >50 rows** (the old code hardcoded table index `[0]`/`[4]`, which breaks when the page layout shifts). Now returns 600 live tickers, cached 7d.
- **Rule: any `pd.read_html`/`read_csv` on a public site must send `_BROWSER_UA` (Wikipedia/most sites 403 the default UA) and select tables by COLUMN content, never a hardcoded index. A silent fallback to a static list = stale picks; log loudly and keep a mocked test.**
- **Recent IPOs / hot names surface before index inclusion** via `screener._high_interest_tickers()` — Yahoo predefined screens (`most_actives` + `day_gainers`, via `yf.screen()`), filtered to plain equities (`_TICKER_RE`, drops crypto/futures). Injected BEFORE MidCap-400 so they survive the 600 cap. This is why a just-IPO'd name (e.g. SPCX) gets scanned without waiting weeks for S&P/Nasdaq membership. Needs yfinance with `screen()` (≥0.2.54; the `hasattr` guard degrades to [] otherwise). **The whole point: discovery is dynamic and NOT tied to the model's training cutoff — never hardcode "current" tickers.**

### Release-hardening (5-agent pre-public audit, Jun 28) — batch 1 done
- **Global JSON error handler**: `webhook.py @app.errorhandler(Exception)` returns JSON (HTTPException keeps its code; else 500 + logged) so the mini-app's `response.json()` never hits HTML. Rule: never let an endpoint return Flask's default HTML error.
- **`nan` price guard at the SOURCE modules too**: `price_checker.py` (3 sites) + `market_data._yf_price` now use `if price and price > 0` (the `_is_pos` guard wasn't only an alert-layer concern). Any new `fast_info.last_price` read must do this — `nan` is truthy.
- **`/health` returns `{"status":"ok"}` only** — it's unauthenticated; never serialize the config dict (allowlist/admin id) to it.
- **IDOR rule: mutating endpoints use the AUTHENTICATED chat_id, never a client-supplied `chat_id` body/param** (seed_backtest was overriding it). Same root cause as the auth gap below.
- **Anthropic client**: `anthropic.Anthropic(timeout=60, max_retries=2)` — the SDK default (~600s×2) can hang the whole morning run.
- **Cron-secret compares use `hmac.compare_digest`** (timing-safe).
- **Every per-user broadcast loop body must be try/except-wrapped** so one user's failure can't abort delivery for the rest (run_midday_check was missing it).
- **Gunicorn: `--workers 1 --threads 8`** (Procfile + render.yaml were 1 vs 4) — one copy of pandas avoids 512MB OOM; threads give concurrency for I/O-bound handlers + coherent in-memory caches.
- **Auth (FIXED batch 2)**: `_miniapp_auth` now verifies the Telegram `initData` HMAC (`_verify_init_data`: secret=HMAC("WebAppData", bot_token), compare to `hash`) and trusts ONLY the verified user id — never a client `chat_id` param (which was forgeable → account takeover). Enforced in production (bot token set); param fallback only in dev/tests (no token). Emergency valve `MINIAPP_AUTH_DISABLED=1` reverts to param auth without a redeploy. Webhook secret-token verification is opt-in via `TELEGRAM_WEBHOOK_SECRET` (set it + re-register via /register). Tests bypass HMAC via a conftest fixture (the real verifier is tested directly in TestMiniappAuthSecurity).
- **Storage concurrency (FIXED batch 3)**: per-user saves go through `config_manager._update_user_keyed_file` (and shared files through `mutate_gist_file`) — a per-file `threading.Lock` + `GistBackend.read_strict` (RAISES on fetch error, vs old `read()` returning None). This kills (a) the catastrophic "a transient read fails → `or {}` → write erases ALL users" clobber, and (b) cross-user lost-updates (re-read fresh + merge only this uid; gunicorn `--workers 1` means the lock serializes all web-side mutations). Refactored: save_user_trade_log / save_user_paper / save_user_config / reset_user_config, and price_alert_manager add_alert / remove_alert. **Rule: never `_load_gist_file(F) or {}` then write — use `_update_user_keyed_file`/`mutate_gist_file`.** RESIDUAL (acceptable, documented): same-user mutation racing across processes (web vs the GitHub-Actions jobs) is still last-writer-wins — the full fix is etag/If-Match or a row store; `buy_counts` increments can still lose (cosmetic).

- **LLM-cost endpoints must be rate-limited.** `webhook._rate_limited(chat_id, bucket, max_calls, window_sec)` is a per-user sliding-window limiter (in-process dict + `threading.Lock`; authoritative because `--workers 1`). `/api/miniapp/define` is gated at 20/min → 429. **Rule: any new endpoint that calls the Anthropic API (or any costly external) must wrap with `_rate_limited` before the call, and pass a distinct `bucket` name.** Inline `anthropic.Anthropic(...)` clients must set `timeout=` + `max_retries=` (matches llm_client).
- **Screener admits young IPOs for LT scoring.** The old `len(hist) < 30: continue` gate dropped any stock public for <30 trading days (e.g. SPCX) before it reached the fundamentals-driven `_long_term_score`, so the universe high-interest injection was wasted. Now `screener._bar_eligibility(n_bars)` → `(admit, compute_st)`: <10 bars rejected; 10-29 bars admitted with a zero ST score + `{"young_ipo": True}` (LT-eligible, no technicals); ≥30 full. So a recent IPO is *eligible* and scored honestly on Finnhub fundamentals — it surfaces only if its fundamentals/liquidity qualify (we don't force unproven names into BUY picks). `young_ipo` flag is available in `st_metrics` for the frontend to badge.

### Morning message UX (simplified Jul 1, 2026 — do not re-add clutter)
- **`formatters.build_picks_keyboard` is TWO buttons, not per-pick.** Deliberately collapsed from 14+ per-pick `📌 Log X` / `📡 Alert at $Y` rows + section dividers to `📊 Today's Picks — Log / Set Alert` (opens `/miniapp?tab=picks`) + `📄 Paper Trade`. The per-pick actions (Log via "I Bought This", Set Alert, Chart) live in the picks-tab pick sheet. **Rule: don't re-add per-pick buttons to the morning keyboard — route to the picks tab.** Returns `[]` with no mini-app URL.
- **Pick-sheet alert is a CUSTOM price**, not fixed-at-entry. The mini-app pick sheet's alert button calls `openWatchAlert(sym, entryPrice)` (the shared watchlist overlay), which takes an optional `defaultPrice` and guards `(_watchAlertsData || [])` so it works cross-tab. The old one-tap `setPickEntryAlert` (fixed at entry) was removed. **Rule: reuse `openWatchAlert` for any per-ticker alert UI — one overlay, custom price.**
- **Pick cards reflect alert STATE.** `_pickAlertBtnHtml(sym, entry)` renders `🔔 Alert $X` (amber) when an alert exists for the ticker, else `📡 Set Alert` (green); `_loadPickAlerts()` fetches `/api/miniapp/alerts` on picks-tab load (the watchlist tab already did) into `_watchAlertsData` and repaints every `[id^="alert-btn-"]` in place via `_repaintPickAlertBtns()` (no full re-render). The overlay's `🗑 Remove alert` button (`removeWatchAlert`, shown only when editing) resets it. **Rule: after any alert add/remove, optimistically update `_watchAlertsData` + `_repaintPickAlertBtns()` so the button flips immediately, then `_loadPickAlerts()` to reconcile.** NOTE: a manual alert replaces the morning run's auto stop alert for that ticker (one alert per ticker, by design).
- **Alert edit = ONE atomic replace, never two HTTP calls.** `add_alert(chat_id, ticker, price, replace=True)` drops ALL existing alerts for the ticker inside the single `mutate_gist_file` mutate, then appends the new one (bypasses the dup-rejection). `saveWatchAlert` calls `/api/miniapp/alerts` (add) with `replace: true` ONCE. **Rule: replacing a shared-Gist record must be one atomic mutate — a client-side remove-then-add (two requests) can drop the change to GitHub's read-after-write lag between requests** (this exact bug left the old alert price after an edit).
- **"On Your Radar" shows movers only** (`abs(change_pct) >= 1.5` or has an active alert, cap 6) — not every watchlist ticker. Flat ±0.5% names are dropped as noise.
- Morning footer is ONE tip line (was two repeated paragraphs) pointing at the `📊 Today's Picks` button; the date uses a neutral `📅` (was the market-mood emoji, redundant with the Market line). Empty sections (Commodities/Options "no setup") were KEPT per owner preference.
- **ST/LT duplicate picks collapse to one card.** A ticker that's both a short-term trade AND a long-term hold renders ONCE in Short Term with a `· also a long-term hold → $<LT target> (<horizon>)` note; the duplicate LT card is dropped (`_lt_hold` map in `format_daily_message`). If every LT pick collapsed, the LT section shows a compact "shown above" line, not "no setups". **Rule: don't re-fork ST/LT dupes into two full cards.**
- **EOD position pricing must be partial-tolerant.** The EOD "📂 Your other positions" + Portfolio P&L sections silently vanished from a user's End-of-Day: `run_eod_summary` fetched position prices via `_download_prices(missing)`, which THREW on a single failed/rate-limited ticker (crypto 429) and the `except` then dropped ALL positions → the section filtered out (needs `current_prices.get(ticker)`). Fixed by using `market_data.get_live_prices` (per-ticker, crypto-aware, returns partial results). **Rule: any batch price fetch feeding a user-facing block must degrade per-ticker, never all-or-nothing.** The section shows P&L vs entry + a "stop hit" badge (EOD `_row` uses `price <= stop`, already correct).

### Scale hardening (Jun 28) — what's done vs deliberately deferred
- **Telegram 429 backpressure**: `telegram_api._retry_after_secs(resp)` reads `parameters.retry_after` (then the `Retry-After` header, capped 30s) and all 4 send paths (send_message, send_inline_keyboard, send_photo, async broadcast_all) sleep that long on 429 instead of the fixed RETRY_DELAY. **Rule: a new send path must handle 429 via `_retry_after_secs`, not a blind fixed retry.**
- **OHLCV cache**: `market_data.get_ohlcv` caches per `(ticker, days)` for `_OHLCV_TTL` (600s). Safe because chart callers fetch the live price separately — cached *historical bars* never stale a quote. Don't cache live-price functions this way (violates "live price always trumps stale").
- **Webhook dedup**: `webhook._is_duplicate_update(update_id)` (bounded in-process dict) drops Telegram retries of an already-handled update — prevents double position-logs / double replies when a slow handler (LLM) doesn't ack fast.
- **DEFERRED (not done, by design)**: full async-webhook backgrounding (ack-then-dispatch-in-thread) and converting the serial `for uid in _all_recipients(): send_message()` broadcast loops in agent.py to `broadcast_all`. At ~2 users these give ~no benefit and carry real regression risk on the core request path / live delivery; the `update_id` dedup already fixes the double-processing that backgrounding would. Revisit with load testing as concurrent users grow (the morning picks loop already uses `broadcast_all` as the template).

### Bug pattern: "NEAR STOP" must not mask a blown-through stop (Jul 2)
- The pre-market card showed BNB (−13.9% from entry, price $560 vs stop $645 — 13% BELOW its stop) as "⚠️ NEAR STOP". The badge condition was `price <= stop*1.03`, which is true for ANY price at/below stop, so a position that already blew through read "near". Worse, NO alert fired: `_check_hold_or_fold` only nudges in the −2%..−8%-from-entry band, so a position crashed past −8% / past its stop went silent.
- Fix: `agent._stop_badge(price, stop)` → `🔴 STOP HIT` when `price <= stop`, `⚠️ NEAR STOP` only when `stop < price <= stop*1.03` (used at both pre-market badge sites). AND `_check_hold_or_fold` now fires a loud `🔴 STOP HIT` alert (dedup key `stophit_{uid}_{ticker}_{et_today}`) whenever `curr <= stop`, BEFORE the softer hold/fold window, with `continue` so a past-stop position doesn't also get the soft nudge. **Rule: any stop-proximity UI/alert must treat at/below-stop as a distinct, louder state than 'near' — never let a −13% position read 'near stop' or go unalerted.**

### CoinGecko free-tier rate limits — one cached fetch (Jul 2)
- CoinGecko's free tier (~30 calls/min) is shared across ALL our crypto price call sites (price_alerts, positions, watchlist, paper, screener). Uncoordinated per-coin calls were 429-ing (`[price_checker] CoinGecko call failed (429...)` seen in Render logs). **`price_checker.cg_prices(cg_ids)` is the ONE cached fetch**: batched `/simple/price`, a 60s in-process cache (`_CG_CACHE`, so each coin hits the network at most once/min regardless of how many callers ask), and a single 429 backoff (honors `Retry-After`, capped). `get_current_prices` + `price_alert_manager._current_price` route through it. **Rule: any new crypto-price CoinGecko `/simple/price` fetch must go through `cg_prices`, never a raw per-coin `requests.get`.** (Remaining raw call sites — webhook `_fast_quote` [has its own 60s `_quote_cache`], paper_trader, crypto_screener — can be migrated if 429s persist.)

### Bug pattern: overlapping backtest entries inflate N (Jul 1)
- `miniapp_backtest_pick` counted EVERY day within 2% of entry as a separate sim, so a stock that hovered near the entry level for 10 days produced ~10 overlapping "trades" from one real setup — inflating both wins and losses and making win rates look far more statistically robust than the handful of independent setups behind them (AMBA showed 0/21 that was really 0/3 distinct episodes).
- Fix: NON-OVERLAPPING trades — each entry starts one trade, then the scan skips to `exit_idx + 1` (past the target/stop hit, or past the 60-day window) so consecutive days of the same setup aren't re-counted. **Rule: any historical-simulation/backtest that scans a price series must advance past each trade's exit, never count overlapping entries — N must reflect independent trials.**

### Bug pattern: "moving X% today" must use live price, not daily bars (Jun 30)
- The watchlist big-move alert in `run_price_alerts` used `yf.download(period="2d", interval="1d")` and compared `iloc[-2]` vs `iloc[-1]` (daily close bars). Pre-market / at the open the "today" daily bar isn't formed, so it showed a **stale close** as the price and could report **yesterday's** move as today's — the giveaway was the same `(was $X)` two days running (AMZN "was $232.69" on consecutive days) while a held position (LRCX) rolled correctly.
- Fix: `agent._watch_move_quote(ticker)` → `(live_price, previous_close)` using the live price (prepost 1m bar → `get_live_price` fallback) + `fast_info.previous_close` — the SAME method as the pre-market positions card. **Rule: any "% today" / intraday-move display uses a LIVE price vs `previous_close`, never `iloc[-2]/[-1]` daily bars** (matches "live price always trumps stale").
- **Scope reminder that bit here:** `agent.py` imports `yfinance as yf` ONLY inside functions — a new MODULE-LEVEL helper that uses `yf` must `import yfinance as yf` itself, or it NameErrors (and a swallowing try/except turns that into a silently-dead feature). Same class as the config_manager `timezone` bug.
- **Watchlist move alerts re-fire on escalation bands, not once-flat-per-day.** The dedup key is `watch_move_{uid}_{ticker}_{date}_{band}` where `band = agent._move_band(pct)` = `±int(abs(pct)//3)` (±3% steps). So a ticker that crosses +3% then later +6% alerts twice (distinct bands), but never spams within a band; a reversal (+4% → −4%) is a different band and re-alerts. Was once-per-day-per-ticker, which silenced a mover that kept running past its first small alert. The other two gates are unchanged and by design: `abs(pct)<3` skip, and 30-min sampling (a spike that reverts between checks is never seen).

### Units & user-data hygiene (release polish, Jun 28)
- **Screener stop fields are PERCENTS, not fractions.** `screener.py` writes `atr_pct = atr/price*100` and `suggested_stop_pct = atr_pct*1.5` as percents (e.g. 7.5 == 7.5%). `position_sizer` needs a 0-1 fraction, so it normalizes via `_as_fraction(v)` (`v/100 if v>1 else v`). Before the fix every ATR-fallback stop was clamped to `_MAX_STOP_PCT` (0.20). **Rule: any consumer of `atr_pct`/`suggested_stop_pct` must normalize — don't assume a fraction.**
- **`_get_rsi` (agent.py) maps crypto to `-USD`** via `_SYMBOL_TO_CG_ID` before `yf.Ticker`, like every other yf call on user tickers (the take-profit nudge was getting garbage RSI for BTC/ETH).
- **Money settings go through `parse_money`, never `float()`.** `/api/miniapp/update_settings` used `float()` + bare-except → "$1.5k"/"5%" silently dropped while still returning `ok:true`. It now uses `parse_money` and returns a `warning` listing any unparseable field. **Rule: never `try: float(user_value) except: pass` — use `parse_money` and surface failures.**
- **User-supplied strings (Telegram name/photo_url) → DOM construction, never `innerHTML` interpolation.** The header avatar built an `<img onerror="...'${initials}'">` string; a quote in the display name broke out. Now uses `createElement` + `.src`/`.onerror`. **Rule: never interpolate `tg.initDataUnsafe.user.*` (or any user string) into an HTML template literal.**

### Background jobs must fail silently to users
- run_digest suppresses "Something went wrong" replies from the command layer — scheduled jobs never surface errors to users (only admin logs). Never send a "Building…" teaser before the content is built.

### Ops API access (Render/GitHub)
- Render logs: `GET https://api.render.com/v1/logs?resource=<srv-id>&ownerId=<tea-id>` (ownerId required). Events: `/v1/services/<id>/events` — shows `oomKilled` with memory limit.
- Render OOM kills destroy agent.py's buffered stdout — only stderr (yfinance noise) survives in logs. A run with yfinance lines but no `[agent]` lines = stdout buffer lost to a kill.
- pip ResolutionImpossible on Render claiming "no matching distribution" for a transitive dep (httpx/httpcore) → add explicit top-level pins; verify the wheel exists with `pip download --python-version <ver> --no-deps` first.

## Position logging: total-invested is the primary input (settled Jun 15, 2026)

- **Robinhood does dollar-based investing** — users buy "$500 of LRCX" / "$50 of BTC" and receive fractional shares (1.377, 0.0008). They remember the **total invested** and roughly **when**, NOT the per-share price or share count. Designing the Log Position form around per-share price was the root of the LRCX `entry_price=500` bug (a total typed into a price field poisons every %-based P&L and alert).
- **The form has two paths** (`miniapp/index.html` buypos sheet):
  - **Default — "💵 Total invested"**: total $ + buy-date chips (Today/Yesterday/2d/Pick…). Derives `entry_price = close(date)` and `shares = total / close`. Today reuses the live quote; past dates call `/api/miniapp/close_on_date`.
  - **Toggle — "🎯 Exact $/share"**: per-share entry + optional shares. For intraday traders who know their fill.
- **Why two paths, not one smart field**: a single field that accepts both "500 the total" and "362 the price" is ambiguous — that ambiguity WAS the bug. Forcing the user to declare which they mean is the fix.
- **`/api/miniapp/close_on_date?ticker=X&date=YYYY-MM-DD`** (webhook.py): returns the close on that date or nearest prior trading day (weekend/holiday → `is_estimate=true`). Reuses `_backtest_fetch_prices` (yfinance + CoinGecko fallback, handles crypto suffix). Rejects future dates (400), missing history (404).
- **Honest caveat baked into UI**: close ≠ exact fill (Robinhood fills at live intraday price). Total+date is a *good estimate* (off by intraday drift, usually <1-2%), labelled "· est." when derived from a non-exact day. The exact-price toggle is for anyone who wants precision.
- **What actually needs entry_price**: every %-based alert/nudge (hold-fold, take-profit, EOD % P&L) needs only per-share entry price. Shares ONLY affects dollar figures ($ P&L, "$X at risk"). So the date-lookup's real job is recovering entry price for someone who knows only a total — it is load-bearing, not optional.
- `cmd_market.py /size` and `formatters.py` build_picks_keyboard already use fractional crypto sizing (`round(budget/price, 6)`) — never `max(1, int(...))` which fabricates whole coins.

## Development rules

### Scope — always apply changes everywhere
- When making any change, apply it in ALL relevant places — not just the most obvious one
- For any frontend change, check webhook.py for the matching API endpoint and update it too
- For any API change, check miniapp/index.html for all call sites and update them too
- For any data a page needs on load, ensure it is included in the API response
- After fixing a bug, scan for the same pattern in adjacent code paths and fix those too

### Auto-save / UX
- All form inputs must auto-save on blur — never require a manual Save button as the only save path
- Never call `tg.enableClosingConfirmation()` — always use `tg.disableClosingConfirmation()` + `window.onbeforeunload = null`

### State persistence
- Never use sessionStorage as the sole source of truth for user state — sessionStorage is wiped on app close
- For state that must survive app restarts, piggyback it onto an existing API response (e.g. `_meta.bought_tickers` in picks)
- Live price always trumps stale stored price — use `get_live_price()` for any current price display

### Timing / schedule references
- Morning run cron: `0 11 * * 1-5` = 7:00 AM ET
- All user-facing strings must say 7 AM ET — never 8 AM ET
- **Primary scheduler**: cron-job.org (free) → POST `https://<render-url>/trigger/morning?secret=CRON_SECRET` at 11:00 AM UTC on weekdays. Fires within seconds, reliable.
- **Backup scheduler**: GitHub Actions (`.github/workflows/daily_run.yml`) — still runs all other jobs (confirmation, EOD, alerts, etc.). Morning cron kept as fallback but duplicate-run guard in `/trigger/morning` prevents double-sending.
- GitHub Actions free tier can delay cron jobs by hours during peak load — this is why cron-job.org was added for the morning run
- `CRON_SECRET` env var must be set on Render — include as `?secret=CRON_SECRET` in cron-job.org URL
- Late-delivery guard was removed — picks now always send since cron-job.org fires on time
- **Keep-warm is 24/7** (`keepwarm.yml`, `*/10 * * * *`). Render free tier spins down after ~15 min idle → a cold server makes the first `/start`/button wait ~30-60s for a boot, or drops the reply mid-boot ("bot not working"). This bit NEW users worst: a share-link click at ANY hour (other timezones/evenings/weekends) hit a cold server and their first `/start` was eaten, so the invite looked broken. The repo is PUBLIC → GitHub Actions minutes are unlimited, so 24/7 warming is free. Confirmed via Render logs that `/health` pings land and the webhook/handler are healthy — the drops were purely cold-start. GH cron can lag a few min; for hard reliability mirror as a cron-job.org `/health` ping (their REST API takes a Bearer key). Render ops: `srv-d7pem7ojs32c73drh9r0` (web) / owner `tea-d7pehdmgvqtc73a72lbg`.

### Performance stats — median not mean
- `get_recent_stats()` in `performance_tracker.py` now returns both `avg_return` and `median_return`
- The morning message perf bar uses `median_return` (outlier-resistant) — a single large crypto return can make `avg_return` misleading (e.g. +441%)
- All other callers (cmd_market.py, cmd_trades.py, cmd_admin.py) still use `avg_return` — do not change those without checking the display context
- **Weekly-recap Avg-line dot = ALPHA, not absolute return** (`formatters._section`). When a benchmark (`spy`) is present the 🟢/🟡/🔴 reflects vs-S&P: 🟢 beat, 🟡 in line (within 1%), 🔴 trailed by >1%. Only falls back to absolute-return direction when there's no benchmark (e.g. the crypto section passes `spy=None`). A green dot next to a negative `(-x%)` alpha was the contradiction that prompted this. Guard: `TestWeeklyRecapAlphaDot` in tests/test_formatters.py.

### Commit discipline
- Always `git add` ALL changed files together — never leave related changes uncommitted
- Provide the full git command (add + commit + push) at the end of every change set

### Read before editing
- Always read the full function/block before modifying it — never edit blindly from grep results alone
- When a bug is in function X, also read all callers of X before deciding the fix
- When adding a field to an API response, read the frontend render function to confirm it actually uses it

### Full data flow verification
- For every change, trace the complete chain: user action → JS handler → API call → Python endpoint → storage → response → JS render
- If any link in that chain is missing or broken, fix the whole chain — not just the link you noticed
- When a feature "doesn't work", check all 5 layers before concluding where the bug is

### Error handling — every async path needs it
- Every `api(...)` call in the frontend must have a try/catch with visible user feedback (toast or inline message)
- Every Python endpoint must handle exceptions and return a meaningful error response, not a 500
- Loading states: every async section must show a spinner/skeleton while fetching and hide it on completion or error

### UI/UX standards
- Every action must give immediate feedback — button state change, haptic, or toast within 200ms
- Never leave a button enabled while its async action is in-flight — always disable + show loading text
- Empty states must be handled: if a list/section has no data, show a helpful message, not a blank space
- All overlays/sheets must close cleanly — reset all inputs, clear error states, re-enable buttons
- Mobile-first: all UI must work on a narrow 375px viewport with touch targets ≥ 44px

### Cache invalidation
- When server data changes (position updated, pick bought, watchlist changed), always call `_invalidatePortfolioCache()` or equivalent and set `loadedTabs[tab] = false` for affected tabs
- Never show stale data after a write — reload the affected tab/section immediately after a successful save

### pandas 2.x compatibility — always use StringIO
- `pd.read_html(string)` and `pd.read_csv(string)` no longer accept raw HTML/CSV strings in pandas 2.x — they try to open the string as a file path and raise `[Errno 2] No such file or directory`
- Always wrap response text: `pd.read_html(StringIO(resp.text))` / `pd.read_csv(StringIO(resp.text))`
- When touching any file that calls `pd.read_html` or `pd.read_csv` with response content, grep all call sites and verify they all use `StringIO`
- Every module that makes external HTTP calls and parses the response must have at least one mocked response test

### Syntax check before declaring done
- After editing any Python file, run `python -m py_compile <file>` mentally or literally — never skip this
- After editing index.html JS, run `python scripts/check_js.py` (or `pytest tests/test_frontend_js.py`) — no exceptions
  - This catches: Node.js syntax errors, TDZ bugs (let/const used in IIFEs before declaration), blocked Telegram APIs
  - The TDZ bug class is SILENT at parse time — `node --check` does not catch it, only this checker does
  - Root cause: a `let`/`const` variable assigned inside a top-level IIFE before its declaration line crashes the entire script at runtime (ReferenceError = blank page). Always move such assignments inside the `_appReady` listener
- A change is NOT done until syntax is verified

### Daily canary — synthetic E2E monitor (catches what mocked tests can't)
- `scripts/canary.py` is a live-data "fake user" run daily by `.github/workflows/canary.yml` (12:30 UTC) that exercises the whole app AS THE ADMIN and DMs a pass/fail report. It's the safety net for the class of bug the 649 mocked tests never see (cold starts, rate limits, stale/real data, delivery, live-price math). Run locally: `python3 scripts/canary.py --dry-run`.
- It checks: picks integrity+math, live price sanity (BTC/ETH ranges, CoinGecko cache), sizing math, backtest non-overlap, delivery/cron health (time-aware via `_expected_delivery_date`), `/health`, and MUTATING round-trips (paper buy → entry==per-share-price not the $ total; alert add→replace→remove; watchlist add) each wrapped in **snapshot→act→restore** so the admin's real data is byte-identical after a run.
- **Rule: when you add or change a user-facing feature or a calculation, add a matching canary check** — that's how a real-world regression gets caught automatically instead of the owner finding it in a screenshot. Keep every mutating check snapshot/restore-safe.

### Synthetic-user bot — accumulating stabilization tester (`scripts/synthetic_user.py`)
- Distinct from the canary (which round-trips + restores). This one behaves like a real active user on the ADMIN account and LEAVES its activity so time-based bugs (P&L drift, alerts firing, position tracking over days) surface. Scheduled by `.github/workflows/synthetic_user.yml`: `--phase open` at 12:00 UTC (8 AM ET) logs REAL "I Bought This" positions + paper-buys from the day's picks, sets target alerts, watchlists them; `--phase manage` HOURLY 14:00–20:00 UTC (10 AM–4 PM ET) sells its winners at target / cuts at stop (real via `close_trade`, paper via `paper_sell`). `open` always DMs a report; `manage` reports ONLY when it actually sells/cuts/errors (`phase_manage` returns actionable-only events; empty list → `main` stays silent) so the hourly cadence doesn't spam the admin — the full run is still in the Actions log. Both phases are per-ticker try/except so one bad ticker never aborts the run; `open` records state the instant a position is logged + saves in a `finally` so a logged position is never orphaned (a `manage` error keeps tracking the ticker + surfaces the error).
- **SAFETY (do not break): it tracks ONLY the tickers it opened in the Gist file `synthetic_state.json` and manages only those — it must NEVER close the owner's real pre-existing positions.** By design it logs REAL trades because the owner uses the admin account as a test account and will reset it before real use. Do NOT point this at a production/real user account.

### Full sweep — exhaustive surface exerciser (`scripts/full_sweep.py`, 3-day hardening window)
- Third leg alongside canary (verifies MATH/delivery) and synthetic-user (real lifecycle): this one verifies the **whole user SURFACE responds without 500/crash**. In ONE run it drives every GET `/api/miniapp/*`, every informational **bot command** (via `handle_incoming_command`), the inline-button **callback dispatcher** (via `handle_callback_query`, prompt-only callbacks), and the **LLM** endpoint (`/define`, every run per owner's choice). Scheduled by `.github/workflows/full_sweep.yml` ~3×/day (13:00/17:00/20:30 UTC).
- **READ-ONLY by design — do NOT re-add a mutation sweep.** The first version exercised every MUTATING POST too, inside snapshot→restore. That did ~20 rapid gist PATCHes → tripped GitHub's **secondary rate-limit (403)**, and when the *restore* PATCH was the blocked one it **lost the admin's data** (clear_alerts wiped all alerts, paper_reset wiped paper — recovered from gist revision history, but only just). Lesson: never batch many gist writes against one gist in seconds, and never make data-safety depend on a final restore that shares that rate limit. Mutation round-trips belong in the **canary** (curated few writes + retrying restore), NOT here.
- **Mini-app auth in the in-process Flask test client**: set `MINIAPP_AUTH_DISABLED=1` BEFORE importing webhook, then authenticate each request with a `chat_id` param (prod is unaffected — separate process). This is the pattern for any test-client mini-app call.
- **Telegram muter**: `_mute_telegram()` monkeypatches `requests.post/get/Session.request` to stub only the SEND methods on `api.telegram.org` (sendMessage/sendPhoto/…) so driving dozens of commands never DMs the admin; **read-only `getMe` passes through** (needed for bot-username resolution → mini-app deep-link buttons, else share_link + /positions/etc. falsely 500). Gist/github.com + price APIs pass through. NOTE: `broadcast_all` uses aiohttp (not requests) so it would bypass — the sweep deliberately never triggers broadcast paths.
- **Reporting**: loud DM on ANY failure (forward-to-Claude); on green stays silent except one end-of-day summary (`utcnow().hour >= 20`) → ~1 green DM/day. **Runs daily with NO end date** (owner asked to keep it running until they say stop — Jul 3; the earlier 2026-07-06 `_SWEEP_END` self-retirement was removed). To stop it: disable/delete `full_sweep.yml`. Its FIRST real run (Jul 3) caught 8 production bugs (the shadow-import class below).

### Test suite — mandatory before every commit
- Run `python -m pytest tests/ -q` before every commit, no exceptions
- A commit is NOT done until all tests pass
- When fixing a bug in a critical path (agent delivery, picks sending, etc.), write a regression test FIRST that reproduces the failure, then fix it
- When adding a function that handles user-facing delivery or state, add a test that asserts the output lands in the right place
- Scope isolation bugs (function referencing caller-scope variables) are NOT caught by py_compile — only tests catch them

### Tests must travel with every code change
- Every code change must be accompanied by a test update in the same commit — no exceptions
- Adding a new function → add tests for the happy path, error path, and at least one edge case
- Changing a function signature or behaviour → update every test that calls it
- Fixing a bug → add a regression test that would have caught it BEFORE applying the fix
- Deleting a function → delete its tests in the same commit
- If a change touches config_manager, formatters, trade_logger, telegram_api, or position_sizer,
  open the corresponding test file and verify coverage still reflects the updated behaviour
- Never commit "I'll add tests later" — later never comes

### Proactive bug hunting
- After fixing any bug, grep for the same pattern across the whole codebase and fix all occurrences
- When a feature is reported broken, check if the same feature has a parallel implementation (e.g. Telegram bot command + mini app UI) and fix both
- When adding a new field/feature, check if any existing tests need updating

### Always ask before coding
- Before making any code change, present the plan and wait for approval
- State exactly which files will change and why — never just start editing
- If the fix requires touching more than 2 files, list all of them upfront

### Verification before declaring done
- After every change, mentally walk through the complete user flow end-to-end
- Explicitly state what was checked: "verified: X works, Y works, Z edge case handled"
- Never say "done" without confirming syntax is clean and the chain is unbroken

### Feature parity — bot + mini app
- Any feature that exists in both the Telegram bot commands AND the mini app must be kept in sync
- When fixing a bug in the mini app, check if the same logic exists in bot_commands.py and fix it there too
- When adding a bot command, check if the mini app needs a matching UI element

### Telegram WebView constraints
- `window.confirm()` and `window.alert()` are blocked — always use `tg.showPopup()` for confirmations
- `window.open()` may be blocked — use `tg.openLink()` for external URLs
- Keyboard appearance changes viewport height — UI must not break when soft keyboard opens

### Never introduce regressions
- Before changing shared utility functions (api(), showToast(), openOverlay(), etc.), grep all call sites first
- When refactoring, verify every existing call site still works with the new signature
- If changing a Python function signature, update all callers in the same commit

### Completeness check before closing a task
- Does the feature work on first load (cold start)?
- Does it work after app close and reopen (state persistence)?
- Does it handle the empty state (no data)?
- Does it handle the error state (API failure)?
- Does it work if the user does it twice (idempotency)?

### Loyal users first — every change must be zero-impact on UX
- This app has active users who trust it for real trading decisions. Any change that degrades their experience — slower loads, spinners where there were none, stale data, layout shifts — is unacceptable regardless of how clean the code is.
- Before any backend change: verify the API response shape is identical. Never rename, remove, or reorder fields that the frontend consumes.
- Before any frontend change: ask "does this cause a flash, a re-render, or a spinner that didn't exist before?" If yes, find a way to make it silent (in-place DOM update, background fetch, stale-while-revalidate).
- Performance improvements must not compromise data freshness. Users watching live positions expect prices to be real-time. A cache that makes the tab feel faster but shows a stale price is worse than a slow load with a correct price.
- Decouple structure from data: load stored data (positions, picks, watchlist) instantly, then overlay live prices asynchronously. Never block the render on a live API call.
- Auto-refresh timers must be silent: no spinners, no full re-renders, no innerHTML swaps that reset scroll position. Use targeted DOM updates (getElementById, textContent, style.width).
- If a background job (refresh, sync, price update) fails, swallow the error silently. Never surface a background failure to the user as an error state.
- When in doubt: do less. A conservative change that improves load time by 20% with zero UX risk beats an aggressive change that saves 60% but has a 5% chance of a visible flash.

### cron-job.org management
- CRON_SECRET contains `#` — must always be URL-encoded as `%23` in cron-job.org job URLs or Render receives the wrong secret
- cron-job.org has a REST API: `GET/PATCH https://api.cron-job.org/jobs/<id>` with `Authorization: Bearer <key>` — use it for programmatic fixes instead of saying "I can't access that"
- To fix a broken cron job: fetch job details, patch the URL, set `enabled: true`

### Memory updates — do it as you go, not just at the end
- Update project_state.md after every significant change, not just at session end
- If a new bug pattern is found mid-session, add it to CLAUDE.md immediately
- "Updating memory" is not optional cleanup — it is part of every commit

### External service access — always ask for API keys first
- Before saying "I can't access X", check if X has a REST API and ask the user for an API key
- cron-job.org, Render, GitHub — all have REST APIs accessible via Bearer token
- One API call saves multiple manual steps for the user
