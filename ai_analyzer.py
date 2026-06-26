"""
ai_analyzer.py — Claude API integration for stock + crypto analysis.
Accepts screener candidates, enriches stocks with Finnhub news, returns structured picks.
"""
from __future__ import annotations

import os
import json
import time
import requests
import yfinance as yf
from llm_client import _get_client

from sentiment_analyzer import get_sentiment
from options_flow import get_options_signal
from insider_tracker import get_insider_signal
from price_context import get_price_context
from performance_context import get_performance_context
from config_manager import (
    load_signal_cache, save_signal_cache,
    get_cached_signal, set_cached_signal,
)

MAX_TOKENS = 3500   # ~2500-3000 tokens with commodities + options plays; 3500 gives safe headroom


# ── News: Finnhub primary, yfinance fallback ──────────────────────────────────
# Finnhub /company-news returns categorised, higher-quality headlines than
# yfinance. Only called for candidates with score >= FINNHUB_NEWS_MIN_SCORE to
# stay well within the free-tier 60 req/min combined with fundamentals calls.
# Falls back to yfinance automatically if the key is absent or the call fails.
# User count has zero impact — news is fetched per-ticker, not per-user.

FINNHUB_NEWS_MIN_SCORE = 0    # Always use Finnhub news when key is available
_FINNHUB_NEWS_URL      = "https://finnhub.io/api/v1/company-news"
_FINNHUB_NEWS_DELAY    = 0.3  # seconds between Finnhub news calls


def _get_news_headlines(ticker: str, max_headlines: int = 3, score: float = 0) -> list[str]:
    """
    Fetch recent news headlines for a ticker.

    Strategy:
      - score >= FINNHUB_NEWS_MIN_SCORE + FINNHUB_API_KEY present → Finnhub /company-news
      - otherwise → yfinance (free, no key)

    Returns list of headline strings (same interface regardless of source).
    Never raises.
    """
    finnhub_key = os.environ.get("FINNHUB_API_KEY", "")

    # ── Finnhub path ──────────────────────────────────────────────────────────
    if finnhub_key and score >= FINNHUB_NEWS_MIN_SCORE:
        try:
            from datetime import date, timedelta
            today     = date.today().isoformat()
            week_ago  = (date.today() - timedelta(days=7)).isoformat()
            resp = requests.get(
                _FINNHUB_NEWS_URL,
                params={"symbol": ticker, "from": week_ago, "to": today, "token": finnhub_key},
                timeout=8,
            )
            if resp.status_code == 200:
                articles = resp.json() or []
                headlines = [a.get("headline", "") for a in articles[:max_headlines]
                             if a.get("headline")]
                if headlines:
                    time.sleep(_FINNHUB_NEWS_DELAY)
                    return headlines
        except Exception as exc:
            print(f"[ai_analyzer] Finnhub news error for {ticker}: {exc}")
        # Fall through to yfinance on any failure

    # ── yfinance fallback ─────────────────────────────────────────────────────
    try:
        news = yf.Ticker(ticker).news or []
        return [n.get("title", "") for n in news[:max_headlines] if n.get("title")]
    except Exception as exc:
        print(f"[ai_analyzer] yfinance news error for {ticker}: {exc}")
        return []


# ── Build stock candidate payload ─────────────────────────────────────────────

def _build_stock_candidates(screener_results: dict) -> list[dict]:
    """
    Combine short + long stock candidates and enrich with news + signals.

    Signal fetching strategy (keeps morning run under ~3 min):
      - Sentiment + insider: cached for 5 trading days (Gist). Cold fetch only
        for new tickers or cache misses. Saves ~1.5s per ticker per day.
      - Options flow: fetched live but ONLY for candidates with score >= 50.
        Options data is stale after a day, so caching is not useful here.
    """
    candidates = []
    seen = set()

    # Load signal cache once — avoids one Gist round-trip per ticker
    signal_cache = load_signal_cache()
    cache_updated = False

    all_picks = (
        [("short_term", s) for s in screener_results.get("short_term", [])] +
        [("long_term",  s) for s in screener_results.get("long_term",  [])]
    )

    # ── PERF: Bulk price pre-fetch for sanity check ───────────────────────────
    # One yf.download() call instead of one yf.Ticker().fast_info per candidate.
    # Reduces HTTP round-trips from N to 1 for the price sanity check loop.
    _stock_tickers_to_check = [
        s["ticker"] for _, s in all_picks
        if s.get("current_price", 0) > 0 and s["ticker"] not in _KNOWN_CRYPTO
    ]
    _bulk_live_prices: dict[str, float] = {}
    if _stock_tickers_to_check:
        try:
            import yfinance as yf
            _bulk_raw = yf.download(
                " ".join(_stock_tickers_to_check), period="1d", interval="1d",
                progress=False, auto_adjust=True,
            )
            if not _bulk_raw.empty:
                _bulk_close = _bulk_raw["Close"] if "Close" in _bulk_raw.columns else _bulk_raw
                if hasattr(_bulk_close, "columns"):
                    for _t in _stock_tickers_to_check:
                        if _t in _bulk_close.columns:
                            _s = _bulk_close[_t].dropna()
                            if len(_s):
                                _bulk_live_prices[_t] = float(_s.iloc[-1])
                else:
                    # Single-ticker flat DataFrame
                    if len(_stock_tickers_to_check) == 1 and len(_bulk_raw) > 0:
                        _bulk_live_prices[_stock_tickers_to_check[0]] = float(_bulk_raw["Close"].iloc[-1])
            print(f"[ai_analyzer] Bulk price pre-fetch: got {len(_bulk_live_prices)}"
                  f"/{len(_stock_tickers_to_check)} prices.")
        except Exception as _exc:
            print(f"[ai_analyzer] Bulk price pre-fetch failed (will skip sanity check): {_exc}")

    for category, stock in all_picks:
        ticker = stock["ticker"]

        # ── Code-level earnings pre-filter (safety net) ──────────────────────
        # Strip SHORT-TERM candidates with earnings within 2 days before Claude
        # sees them. This prevents Claude from including them even if it ignores
        # the prompt rule.
        skip = False
        if category == "short_term" and stock.get("earnings_date"):
            try:
                from datetime import datetime, date as _date
                ed = stock["earnings_date"]
                # Support "Thu May 1" style strings and ISO dates
                for fmt in ("%a %b %d", "%Y-%m-%d", "%b %d"):
                    try:
                        parsed = datetime.strptime(ed, fmt)
                        # For formats without year, assume current year
                        parsed = parsed.replace(year=_date.today().year)
                        days_away = (parsed.date() - _date.today()).days
                        if 0 <= days_away <= 2:
                            print(f"[ai_analyzer] Pre-filter: dropping {ticker} "
                                  f"from short_term (earnings in {days_away}d: {ed})")
                            skip = True
                        break
                    except ValueError:
                        continue
            except Exception as exc:
                print(f"[ai_analyzer] Earnings date parse error for {ticker}: {exc}")

        if skip:
            continue  # skip the outer loop — drop this candidate entirely

        # Price sanity check — second line of defence after screener.py's check.
        # Catches stale cache entries with yfinance data glitches (e.g. MU at $576
        # instead of $85). Uses bulk pre-fetched prices (no extra HTTP calls).
        raw_price = stock.get("current_price")
        if raw_price and raw_price > 0 and ticker not in _KNOWN_CRYPTO:
            live = _bulk_live_prices.get(ticker)
            if live and live > 0:
                ratio = raw_price / live
                if ratio > 3.0 or ratio < 0.33:
                    print(f"[ai_analyzer] Price sanity fail for {ticker}: "
                          f"cached={raw_price:.2f} vs live={live:.2f} — dropping candidate.")
                    skip = True

        if skip:
            continue

        entry = {
            "asset_type":    "stock",
            "category":      category,
            "ticker":        ticker,
            "company_name":  stock.get("company", ticker),
            "sector":        stock.get("sector", "Unknown"),
            "current_price": stock.get("current_price"),
            "score":         stock.get("score"),
            "rsi":           stock.get("rsi"),
            "macd_crossover":stock.get("macd_crossover"),
            "volume_ratio":  stock.get("volume_ratio"),
            "obv_positive":  stock.get("obv_positive"),
            "pe_ratio":      stock.get("pe_ratio"),
            "revenue_growth":stock.get("revenue_growth"),
            "debt_to_equity":stock.get("debt_to_equity"),
            "market_cap":    stock.get("market_cap"),
            "news_headlines":[],
        }
        # New enrichment fields — include only if present (not all candidates have all fields)
        for _field in (
            "pct_below_52w_high", "week52_high", "week52_low",
            "analyst_target_mean", "analyst_upside_pct",
            "analyst_buy", "analyst_hold", "analyst_sell", "analyst_consensus",
            "eps_beats", "eps_misses", "eps_quarters_checked", "last_eps_surprise_pct",
            "atr_pct", "suggested_stop_pct",
            "rs_vs_spy",
            "short_float_pct", "days_to_cover",
            "inst_own_pct",
            "days_to_earnings",
            "patterns",
            "congress_buys", "congress_members", "congress_score", "is_cluster",
        ):
            _val = stock.get(_field)
            if _val is not None:
                entry[_field] = _val

        # Earnings within 5 days — pass through to Claude
        if stock.get("earnings_date"):
            entry["earnings_date"] = stock["earnings_date"]

        if ticker not in seen:
            entry["news_headlines"] = _get_news_headlines(ticker, score=stock.get("score", 0))

            # ── Price context: multi-day chart setup ──────────────────────────
            # Fetches 6-month history once and computes MA position,
            # consolidation, breakout, support/resistance, trends.
            # Skip for known crypto symbols — they're not on Yahoo Finance as equities.
            if ticker not in _KNOWN_CRYPTO:
                try:
                    ctx = get_price_context(ticker)
                    if ctx.get("context_str"):
                        entry["price_context"] = ctx["context_str"]
                        # Surface breakout as a top-level flag Claude can weight heavily
                        if ctx.get("breakout"):
                            entry["breakout_today"] = True
                        if ctx.get("consolidation_days", 0) >= 10:
                            entry["consolidation_days"] = ctx["consolidation_days"]
                except Exception as exc:
                    print(f"[ai_analyzer] Price context error for {ticker}: {exc}")

            # ── Sentiment + insider: use cache (5-day TTL) ────────────────────
            cached = get_cached_signal(signal_cache, ticker)

            if cached:
                # Cache hit — no network calls needed
                print(f"[ai_analyzer] Cache hit for {ticker} signals.")
                sent_data = cached.get("sentiment")
                ins_data  = cached.get("insider")
            else:
                # Cache miss — fetch live and store
                sent_data = None
                ins_data  = None

                try:
                    sent_data = get_sentiment(ticker)
                except Exception as exc:
                    print(f"[ai_analyzer] Sentiment fetch error for {ticker}: {exc}")

                try:
                    ins_data = get_insider_signal(ticker)
                except Exception as exc:
                    print(f"[ai_analyzer] Insider fetch error for {ticker}: {exc}")

                set_cached_signal(signal_cache, ticker, sent_data, ins_data)
                cache_updated = True
                time.sleep(0.3)   # brief delay only on live fetches

            # Apply sentiment to entry
            if sent_data:
                try:
                    entry["social_sentiment"] = {
                        "label":           sent_data["label"],
                        "score":           sent_data["score"],
                        "reddit_mentions": sent_data["reddit_mentions"],
                        "summary":         sent_data["summary"],
                    }
                except Exception:
                    pass

            # Apply insider to entry
            if ins_data and ins_data.get("recent_buys", 0) > 0:
                try:
                    entry["insider_activity"] = {
                        "recent_buys":   ins_data["recent_buys"],
                        "is_cluster":    ins_data["is_cluster"],
                        "total_value":   ins_data["total_value"],
                        "insider_score": ins_data["insider_score"],
                        "note":          ins_data["note"],
                    }
                except Exception:
                    pass

            # ── Options flow: live, but only for strong candidates ────────────
            # Options data changes daily so caching is not useful.
            # Skipping for score < 50 saves ~0.5-1s per weak candidate.
            score = entry.get("score") or 0
            if score >= 50:
                try:
                    opts = get_options_signal(ticker)
                    has_flow = (
                        opts.get("unusual") or opts.get("bullish_flow") or
                        opts.get("bearish_flow") or opts.get("sweep_detected")
                    )
                    has_iv = opts.get("iv_rank") is not None
                    if has_flow or has_iv:
                        flow_entry = {
                            "unusual":         opts["unusual"],
                            "put_call_ratio":  opts["put_call_ratio"],
                            "bullish_flow":    opts["bullish_flow"],
                            "bearish_flow":    opts["bearish_flow"],
                            "sweep_detected":  opts.get("sweep_detected", False),
                            "expiries_loaded": opts.get("expiries_loaded", 1),
                            "note":            opts["note"],
                        }
                        # Include gamma pin when price is near peak-OI strike
                        if opts.get("gamma_pin") and opts.get("gamma_pin_strike"):
                            flow_entry["gamma_pin_strike"]   = opts["gamma_pin_strike"]
                            flow_entry["gamma_pin_dist_pct"] = opts["gamma_pin_dist_pct"]
                        # Include nearest real OTM call strike — lets the AI quote
                        # an actual strike instead of fabricating one
                        if opts.get("nearest_otm_call"):
                            flow_entry["nearest_otm_call"]    = opts["nearest_otm_call"]
                            flow_entry["nearest_call_expiry"]  = opts.get("nearest_call_expiry")
                        # Include IV rank when available
                        if has_iv:
                            flow_entry["iv_rank"]  = opts["iv_rank"]
                            flow_entry["iv_pct"]   = opts["iv_pct"]
                            flow_entry["iv_label"] = opts["iv_label"]
                        entry["options_flow"] = flow_entry
                except Exception:
                    pass

            seen.add(ticker)

        candidates.append(entry)

    # Persist cache to Gist only if we made any live fetches this run
    if cache_updated:
        try:
            save_signal_cache(signal_cache)
        except Exception as exc:
            print(f"[ai_analyzer] WARNING: Could not save signal cache ({exc}).")

    return candidates


# ── Build crypto candidate payload ────────────────────────────────────────────

def _build_crypto_candidates(crypto_results: dict) -> list[dict]:
    """Format crypto screener results for the Claude prompt."""
    candidates = []

    all_picks = (
        [("short_term", c) for c in crypto_results.get("short_term", [])]
    )

    for category, coin in all_picks:
        entry = {
            "asset_type": "crypto",
            "category": category,
            "id": coin.get("id", ""),                         # CoinGecko slug for price lookups
            "ticker": coin.get("symbol", coin.get("id", "")).upper(),
            "name": coin.get("name"),
            "current_price": coin.get("current_price"),
            "market_cap_usd": coin.get("market_cap"),
            "score": coin.get("score"),
            "rsi": coin.get("rsi"),
            "volume_24h_usd": coin.get("volume_24h_usd"),    # raw 24h volume in USD
            "price_change_24h_pct": coin.get("price_change_24h_pct"),
            "price_change_7d_pct": coin.get("price_change_7d_pct"),
            "price_change_30d_pct": coin.get("price_change_30d_pct"),
            "pct_below_ath": coin.get("pct_below_ath"),
            "ma_7d": coin.get("ma7d"),                        # 7-day MA from sparkline
        }
        candidates.append(entry)

    return candidates


# ── Commodity candidates ──────────────────────────────────────────────────────

_COMMODITY_TICKERS = [
    ("GLD",  "SPDR Gold Shares",                                      "Gold"),
    ("SLV",  "iShares Silver Trust",                                  "Silver"),
    ("USO",  "United States Oil Fund",                                "Oil"),
    ("WEAT", "Teucrium Wheat Fund",                                   "Wheat"),
    ("GDX",  "VanEck Gold Miners ETF",                               "Gold Miners"),
    ("UNG",  "United States Natural Gas Fund",                        "Natural Gas"),
    ("PDBC", "Invesco Optimum Yield Diversified Commodity Strategy",  "Diversified"),
]


def _build_commodity_candidates() -> list[dict]:
    """
    Fetch price, 1-month return, 14-day RSI, and volume ratio for each
    commodity ETF via yfinance.  These three signals are what the
    COMMODITY CONVICTION RUBRIC scores — without them the AI can't
    rate anything above ★★ and correctly returns [].
    Called once per morning run; no screener required.

    Uses a single yf.download() bulk call instead of N serial Ticker().history()
    calls — reduces HTTP round-trips from 7 to 1.
    """
    tickers = [t for t, _, _ in _COMMODITY_TICKERS]
    meta    = {t: (name, commodity) for t, name, commodity in _COMMODITY_TICKERS}

    try:
        raw = yf.download(
            " ".join(tickers), period="3mo", interval="1d",
            progress=False, auto_adjust=True, group_by="ticker",
        )
    except Exception as exc:
        print(f"[ai_analyzer] Commodity bulk download failed: {exc}")
        return []

    # Normalise to a dict of {ticker: DataFrame(Close, Volume)}
    # yf.download with multiple tickers returns a MultiIndex df; single ticker
    # returns a flat df.  Handle both.
    def _ticker_df(raw, ticker: str):
        if hasattr(raw.columns, "levels"):
            # MultiIndex: (field, ticker)
            try:
                return raw.xs(ticker, axis=1, level=1)[["Close", "Volume"]]
            except KeyError:
                return None
        # Single-ticker fallback (shouldn't happen with 7 tickers but be safe)
        return raw[["Close", "Volume"]] if ticker == tickers[0] else None

    candidates = []
    for ticker in tickers:
        name, commodity = meta[ticker]
        try:
            df = _ticker_df(raw, ticker)
            if df is None or len(df) < 22:
                continue

            close  = df["Close"].dropna()
            volume = df["Volume"].dropna()
            if len(close) < 22:
                continue

            price = round(float(close.iloc[-1]), 2)

            # 1-month return (approx 21 trading days)
            ret_1m = None
            if len(close) >= 22:
                ret_1m = round((float(close.iloc[-1]) / float(close.iloc[-22]) - 1) * 100, 2)

            # 14-day RSI
            rsi = None
            if len(close) >= 15:
                delta = close.diff()
                gain  = delta.clip(lower=0).rolling(14).mean()
                loss  = (-delta.clip(upper=0)).rolling(14).mean()
                rs    = gain / loss
                rsi   = round(float((100 - 100 / (1 + rs)).iloc[-1]), 1)

            # Volume ratio: today's volume vs 20-day average
            vol_ratio = None
            if len(volume) >= 21 and float(volume.iloc[-21:-1].mean()) > 0:
                vol_ratio = round(float(volume.iloc[-1]) / float(volume.iloc[-21:-1].mean()), 2)

            candidates.append({
                "ticker":        ticker,
                "name":          name,
                "commodity":     commodity,
                "current_price": price,
                "ret_1m":        ret_1m,
                "rsi":           rsi,
                "vol_ratio":     vol_ratio,
            })
        except Exception as exc:
            print(f"[ai_analyzer] Commodity candidate error for {ticker}: {exc}")

    print(f"[ai_analyzer] Built {len(candidates)} commodity candidates.")
    return candidates


# ── Claude prompts ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a financial analysis assistant for stocks and cryptocurrencies. "
    "Analyze the provided candidates and return ONLY valid JSON. "
    "Respond with no preamble, no markdown, no explanation. Just the raw JSON object."
)

STRICT_RETRY_SYSTEM = (
    "You are a JSON generator. Output ONLY a valid JSON object. "
    "No text before or after. No markdown. No code blocks. Start with { and end with }."
)


def _build_risk_profile_block(profile: str) -> str:
    """Return risk profile instructions for the Claude prompt."""
    if profile == "conservative":
        return """
RISK PROFILE: conservative
  - Only pick candidates with conviction ★★★★ or higher — skip borderline setups.
  - Favour low-volatility sectors: Consumer Staples, Utilities, Health Care, Financials.
  - Stop-losses: set 4% below entry (tighter than default).
  - Short-term stocks: maximum 1 pick (skip if no strong setup exists).
  - Reduce crypto allocations by 50%; skip crypto short-term entirely if alternatives exist.
  - Long-term picks only from companies with positive revenue growth and D/E < 0.5."""
    if profile == "aggressive":
        return """
RISK PROFILE: aggressive
  - Conviction threshold ★★★ minimum — strong setup counts even if higher risk.
  - All sectors welcome including high-beta: Technology, Energy, Consumer Discretionary.
  - Stop-losses: set 8% below entry (wider room to breathe).
  - Push toward the higher end of the allowed ranges — more picks when setups are available.
  - Full crypto allocations; include higher-risk coins with strong momentum.
  - Short-term targets can be 10-15% above entry for high-momentum plays."""
    # moderate (default)
    return """
RISK PROFILE: moderate (default)
  - Conviction threshold ★★★★ minimum — only include picks you are genuinely confident about.
  - Balanced sector exposure — no preference.
  - Stop-losses: 5% below entry.
  - Standard pick counts and crypto allocations."""


def _build_user_prompt(
    stock_candidates: list[dict],
    crypto_candidates: list[dict],
    config: dict,
    recent_losers: list[str] | None = None,
    regime_info: dict | None = None,
    pick_mode: str = "both",
    etf_candidates: list[dict] | None = None,
    commodity_candidates: list[dict] | None = None,
) -> str:
    # Pre-build conditional blocks (backslashes not allowed inside f-string expressions)
    # Performance feedback: aggregate closed trade track record
    try:
        perf_context = get_performance_context(lookback_days=30)
    except Exception:
        perf_context = ""

    if recent_losers:
        losers_block = (
            "AVOID REPEAT LOSERS (HARD RULE):\n"
            "  These tickers lost money in the last 14 days — DO NOT re-pick them today:\n"
            "  " + ", ".join(recent_losers) + "\n"
            "  If a watchlist ticker appears here, still include it but cap conviction at ★★★."
        )
    else:
        losers_block = ""

    excluded = config.get("excluded_sectors", [])
    if excluded:
        excluded_block = (
            "EXCLUDED SECTORS (HARD RULE — ZERO EXCEPTIONS):\n"
            "  Never pick stocks from these sectors regardless of score: " + ", ".join(excluded)
        )
    else:
        excluded_block = ""

    risk_block = _build_risk_profile_block(config.get("risk_profile", "moderate"))

    # Market regime block — enriched with Fear & Greed, rates, sector rotation
    if regime_info and regime_info.get("regime"):
        r  = regime_info
        fg = r.get("fear_greed") or {}
        rt = r.get("rate_trend") or {}
        sr = r.get("sector_rotation") or {}

        fg_line  = f"  Fear & Greed: {fg.get('label', 'N/A')} ({fg.get('value', '?')}) — trend: {fg.get('trend', '?')}\n" if fg else ""
        rt_line  = f"  Rates: {rt.get('summary', 'N/A')} ({rt.get('rate_regime', '?')} rate environment)\n" if rt else ""

        sr_line = ""
        if sr.get("leaders") or sr.get("laggards"):
            leaders  = ", ".join(sr.get("leaders",  [])) or "none"
            laggards = ", ".join(sr.get("laggards", [])) or "none"
            sr_line  = f"  Sector rotation — Leading: {leaders} | Lagging: {laggards}\n"

        # Build regime-specific signal priority guidance
        regime_name = r['regime'].lower()
        if regime_name == 'bull':
            signal_priority = (
                "  ST SIGNAL PRIORITY (bull regime — applies to short_term picks only): "
                "Weight MOST → rs_vs_spy > +5%, breakout_today, "
                "bullish patterns, options bullish_flow/sweep. Weight LESS → defensive fundamentals alone. "
                "Momentum backed by volume is your best friend here.\n"
                "  LT SIGNAL PRIORITY: Regime does NOT change LT evaluation — always use the "
                "LT STOCK CONVICTION RUBRIC (fundamental signals only). Ignore technical signals for LT."
            )
        elif regime_name == 'elevated':
            signal_priority = (
                "  ST SIGNAL PRIORITY (elevated regime — applies to short_term picks only): "
                "Weight MOST → analyst_consensus buy + eps_beats "
                "combination, insider_activity cluster, low debt_to_equity, high inst_own_pct. "
                "Weight LESS → pure price momentum without fundamental backing. "
                "Require at least 1 fundamental signal alongside any technical signal.\n"
                "  LT SIGNAL PRIORITY: Regime does NOT change LT evaluation — always use the "
                "LT STOCK CONVICTION RUBRIC (fundamental signals only). Ignore technical signals for LT."
            )
        elif regime_name in ('volatile', 'bear'):
            signal_priority = (
                f"  ST SIGNAL PRIORITY ({regime_name} regime — applies to short_term picks only): "
                "Weight MOST → insider_activity (especially cluster), "
                "analyst_consensus buy with high buy count, defensive sector membership, low atr_pct, "
                "high inst_own_pct. Weight LEAST → social_sentiment, short_float squeeze thesis, "
                "pure momentum. Reject any pick whose primary thesis is price momentum alone.\n"
                "  LT SIGNAL PRIORITY: Regime does NOT change LT evaluation — always use the "
                "LT STOCK CONVICTION RUBRIC (fundamental signals only). Ignore technical signals for LT."
            )
        else:  # neutral
            signal_priority = (
                "  ST SIGNAL PRIORITY (neutral regime — applies to short_term picks only): "
                "Balanced weighting across all signals. "
                "Prefer picks with 2+ signal types confirming (e.g. technical + fundamental, "
                "or technical + insider). Single-signal picks require conviction ★★★★ minimum.\n"
                "  LT SIGNAL PRIORITY: Regime does NOT change LT evaluation — always use the "
                "LT STOCK CONVICTION RUBRIC (fundamental signals only). Ignore technical signals for LT."
            )

        regime_block = (
            f"MARKET REGIME: {r['regime'].upper()}\n"
            f"  VIX: {r.get('vix', 'N/A')} | SPY above 50MA: {r.get('spy_above_50ma')} "
            f"| SPY above 200MA: {r.get('spy_above_200ma')}\n"
            f"{fg_line}{rt_line}{sr_line}"
            f"  Note: {r.get('note', '')}\n"
            f"  Adjust pick aggressiveness accordingly:\n"
            f"    bull     → normal operation, full pick counts\n"
            f"    neutral  → normal, add brief caution note in thesis\n"
            f"    elevated → SPY uptrend intact but VIX elevated; reduce size, prefer quality, "
            f"mention elevated risk in every thesis — conviction ★★★★ minimum\n"
            f"    volatile → lower-beta picks only, mention risk prominently, "
            f"conviction ★★★★ minimum\n"
            f"    bear     → defensive sectors only (Utilities, Consumer Staples, Health Care), "
            f"skip all high-momentum plays, conviction ★★★★★ or skip\n"
            f"  Use sector rotation leaders/laggards to favour/avoid sectors in thesis.\n"
            f"{signal_priority}"
        )
    else:
        regime_block = ""

    # Mode-specific instruction prefix
    show_st = pick_mode in ("st", "both")
    show_lt = pick_mode in ("lt", "both")
    mode_note = {
        "st":   "USER MODE: SHORT TERM ONLY. Generate ONLY short_term picks for stocks and crypto. Return empty arrays [] for all long_term sections.",
        "lt":   "USER MODE: LONG TERM ONLY. Generate ONLY long_term picks for stocks and crypto. Return empty arrays [] for all short_term sections.",
        "both": "",
    }.get(pick_mode, "")

    # ── Budget notes (allocation filled in post-processing after pick count is known) ─
    stock_budget  = config.get("stock_budget")   # None = unset
    crypto_budget = config.get("crypto_budget")  # None = unset

    stock_alloc_note  = (f"  Budget: ${stock_budget} total for stocks — set 'allocation' to null, it will be calculated automatically.\n"
                         if stock_budget else "  No budget set — set 'allocation' to null for all stock picks.\n")
    crypto_alloc_note = (f"  Budget: ${crypto_budget} total for crypto — set 'allocation' to null, it will be calculated automatically.\n"
                         if crypto_budget else "  No budget set — set 'allocation' to null for all crypto picks.\n")

    stocks_block = ""
    if show_st:
        stocks_block += "  Short-term: 2–3 picks (target gains within 1-4 weeks). Only include if conviction ★★★ or higher — do NOT pad with weak setups.\n"
    if show_lt:
        stocks_block += "  Long-term: 2–4 picks (dollar-cost average over 1-5 years). Quality over quantity — fewer strong picks beat many mediocre ones.\n"
    stocks_block += stock_alloc_note

    return f"""Analyze these stock AND crypto candidates for a personal investor.
{f"{mode_note}" + chr(10) if mode_note else ""}{(perf_context + chr(10) + chr(10)) if perf_context else ""}
ST STOCK CONVICTION RUBRIC — applies to short_term stock picks only:
  ★★★★★ (5) — EXCEPTIONAL: 4+ confirming signals below. Reserved for rare high-conviction setups.
  ★★★★  (4) — STRONG: 3 confirming signals. Clear setup with multi-factor confirmation.
  ★★★   (3) — MODERATE: 2 confirming signals. Acceptable for aggressive risk profiles only.
  ★★    (2) — WEAK: 1 confirming signal or conflicting signals. Do not include.
  ★     (1) — DO NOT INCLUDE. Return [] instead.

  WHAT COUNTS AS ONE ST "CONFIRMING SIGNAL":
    • RSI 35-55 AND MACD crossover present (both together = 1 signal)
    • volume_ratio > 1.5 AND obv_positive = True (together = 1 signal)
    • breakout_today = True (price closed above prior 20-day high with volume) — strong standalone signal
    • pct_from_52w_high <= 3 AND volume_ratio > 1.3 (near 52-week high breakout zone with volume)
    • patterns include "bull_flag" or "bullish_engulfing" (candlestick confirmation)
    • options_flow: unusual = True OR bullish_flow = True OR put_call_ratio < 0.7
    • insider_activity present AND is_cluster = True
    • analyst_consensus = "buy" AND analyst_upside_pct > 10%
    • eps_beats >= 3 AND last_eps_surprise_pct > 3%
    • rs_vs_spy > +5% (meaningful outperformance over 20 days)
    • congress_score >= 6 (cluster buy by multiple members)
  Do NOT count the same data point twice under different signal names.

LT STOCK CONVICTION RUBRIC — applies to long_term stock picks only (DO NOT use ST rubric for LT):
  LT picks are quality buy-and-hold candidates. Technical signals (RSI, breakout, volume) are
  IRRELEVANT for LT selection — use only fundamental and institutional signals below.

  ★★★★★ (5) — EXCEPTIONAL: 4+ LT signals. Rare — a quality compounder at a good price with institutional backing.
  ★★★★  (4) — STRONG: 3 LT signals. Clear fundamental thesis with multiple quality markers.
  ★★★   (3) — MODERATE: 2 LT signals. Solid candidate worth buying over time.
  ★★    (2) — WEAK: 1 signal only. Skip.
  ★     (1) — DO NOT INCLUDE.

  WHAT COUNTS AS ONE LT "CONFIRMING SIGNAL":
    • analyst_consensus = "buy" AND analyst_upside_pct > 10% (Wall Street sees meaningful upside)
    • eps_beats >= 3 out of last 4 quarters (consistent earnings execution)
    • PE below sector median (trading at discount to peers — value opportunity)
    • inst_own_pct > 50% (institutional conviction — smart money already in)
    • rs_vs_spy > 0% over 20 days (holding up or outperforming the market)
    • congress_score >= 6 (cluster buy — 2+ members disclosed purchases)
    • insider_activity present AND is_cluster = True (insider cluster buy)
    • last_eps_surprise_pct > 5% (strong recent beat — business momentum)
  Do NOT use RSI, MACD, volume_ratio, breakout, or options flow as LT signals — these are irrelevant.

  LT CONVICTION GATE: Minimum ★★ (1 LT signal) for moderate and aggressive risk profiles; ★★★ (2 signals) for conservative.
  LT picks are long-horizon — time in market matters. Always return at least 1 LT pick if any candidate reaches the minimum bar.
  Return [] only if truly no candidate has any qualifying LT signal.

ST STOCK CONVICTION GATE (ABSOLUTE RULE):
  - Minimum conviction is set by the risk profile (conservative ★★★★+, moderate ★★★+, aggressive ★★★+).
  - Do NOT pad to hit pick count targets. Fewer genuine picks beats more filler picks EVERY time.
  - A pick that barely squeaks past the threshold should be DROPPED, not included.
  - If only 1 stock qualifies, return 1. Always aim for at least 1 pick if any candidate clears the minimum bar.
  - Return [] only if truly no candidate reaches the minimum conviction — this should be rare, not the default.

SECTOR DIVERSITY RULE (HARD RULE — applies to the full output):
  - Maximum 2 picks from the same sector across ALL sections (ST stocks + LT stocks combined).
  - If 3+ candidates from the same sector qualify, keep the 2 highest-conviction and drop the rest.
  - This prevents an all-tech or all-energy output on sector momentum days.
  - ETFs and commodities are exempt from this rule (they are already diversified vehicles).

EARNINGS PROXIMITY RULE (ST picks only):
  - If a candidate has earnings in the next 1–3 days: cap conviction at ★★★ — gap risk is real.
  - If earnings in 4–7 days: note the risk in the thesis but do not cap conviction.
  - If earnings > 7 days away: no adjustment needed.
  - Check days_to_earnings field if provided.

ANALYSIS FRAMEWORK — WORK THROUGH THESE STEPS MENTALLY BEFORE SELECTING ANY PICK:
  Step 1 — REGIME LENS: Anchor your entire analysis to the current market regime.
    bull     → full operation; momentum and breakout setups are valid
    elevated → tighten standards; require quality signals alongside technicals; mention risk in every thesis
    volatile → technicals alone are insufficient; require fundamental or insider backing too
    bear     → defensive sectors only; reject all momentum/breakout plays regardless of score
  Step 2 — SIGNAL COUNT: Use the CORRECT rubric for the pick type:
    ST stocks → use ST STOCK CONVICTION RUBRIC (technical signals)
    LT stocks → use LT STOCK CONVICTION RUBRIC (fundamental signals ONLY — no technicals)
    ETFs      → use ETF CONVICTION RUBRIC
    Commodities → use COMMODITY CONVICTION RUBRIC
    If you are uncertain whether a signal qualifies, it does NOT qualify. Marginal = fail.
  Step 3 — STRESS TEST (devil's advocate): For each candidate that passes Step 2, ask:
    "What are the 2 most likely reasons this pick fails?"
    ST: within 2–4 weeks. LT: within 6–12 months.
    If those reasons outweigh the thesis → DROP the pick.
    If they are meaningful but manageable → lower conviction by 1 star and note the risk.
  Step 4 — NEWS CATALYST QUALITY: For each pick, assess the catalyst honestly:
    Is it upcoming (adds fuel) or already priced in (no edge)? Is it company-specific or just macro noise?
    An already-priced catalyst weakens the pick — lower conviction accordingly.
  Step 5 — PORTFOLIO COHERENCE: Review all passing picks together.
    Apply SECTOR DIVERSITY RULE — max 2 picks per sector across ST + LT combined.
    No two picks driven by the identical catalyst type.
    Prefer picks that complement each other across risk/return profile.
    Then apply the conviction gate and output JSON.

STOCKS:
{stocks_block}

ALLOCATION RULE:
  - Set 'allocation' to null for every pick — it is calculated automatically after selection.

SECTOR DIVERSITY RULE (STRICTLY ENFORCE):
  - No two picks in the same category (short-term or long-term) may share the same sector.
  - If the top candidates by score include sector duplicates, drop the lower-scored duplicate
    and substitute the best-scored stock from an unrepresented sector.
  - A pick at 60% score from a new sector beats a pick at 65% from an already-represented sector.

EARNINGS RISK RULES (HARD RULE — ZERO EXCEPTIONS):
  - If a candidate has "earnings_date" within 1-2 days: DO NOT include it in short-term picks.
    This is an absolute rule. No exceptions for high scores, strong setups, or any other reason.
    Earnings surprises cause violent moves that invalidate technical setups.
  - If "earnings_date" is 3-5 days away: you MAY include it in short-term picks, but set
    conviction to maximum 2 stars and include the earnings date in the thesis.
  - Earnings risk does NOT affect long-term picks — include normally.

LONG-TERM TARGET PRICE RULES (STRICTLY ENFORCE):
  - Use realistic MID-CASE returns, NOT bull-case or best-case scenarios.
  - Base targets on historical growth rates and current valuation multiples.
  - Annualised return benchmarks by type:
      Tech / growth stocks:    12-18% per year
      Value / defensive stocks: 8-12% per year
  - Example: a 2-3 year tech pick at $424 entry → realistic target $560-650, NOT $800+
  - Do NOT extrapolate recent momentum into long-term targets.
  - If a stock's target implies >25% annualised return, reduce it to 20% max.

CRYPTO:
{"  Short-term only: 1–2 picks (target gains within 1-2 weeks, high risk). Skip entirely if no strong setup — do NOT force picks." if show_st else "  No crypto picks requested."}
  Always return an empty array [] for crypto long_term — crypto long-term picks are not used.
{crypto_alloc_note}

CRYPTO RULE: Each crypto symbol may appear AT MOST ONCE. No duplicates. long_term must always be [].

{regime_block}

{risk_block}

{losers_block}

{excluded_block}

SIGNAL GUIDANCE (use in thesis and catalyst where relevant):
  - social_sentiment: StockTwits + Reddit signal. Label "bullish"/"hot" supports picks; "bearish" is a red flag.
  - options_flow: aggregated across up to 4 near-term expiries — more accurate P/C ratio than single-expiry. Low P/C (<0.7) = calls dominating (bullish). High P/C (>1.5) = puts dominating (bearish).
  - sweep_detected (options): large block trade through single contract — institutional conviction, weight heavily.
  - nearest_otm_call + nearest_call_expiry: the REAL nearest out-of-the-money call strike with active volume, from live options data. When suggesting an options play (e.g. "consider the $185 call expiring 2025-05-23"), ALWAYS use nearest_otm_call as the strike and nearest_call_expiry as the expiry. NEVER invent or estimate strikes. If nearest_otm_call is absent, describe the trade directionally without specifying a strike.
  - gamma_pin_strike: the strike with highest aggregate open interest across all expiries. When price is within 2% (gamma_pin=True), market makers must hedge by buying/selling as price moves, creating a magnetic pull toward this level near expiry. Use as short-term price target or support/resistance.
  - iv_rank (0-100): where current implied volatility sits vs. its 1-year historical range. <25 = LOW (options cheap, consider debit spreads / buying calls). 25-60 = NORMAL. 60-80 = ELEVATED (premium rich, better to sell spreads or use defined-risk entries). >80 = EXTREME (IV crush risk — avoid buying naked options unless expecting a major move). Always mention iv_label ("LOW"/"ELEVATED"/"EXTREME") when present in the thesis.
  - insider_activity: recent open-market buys by CEO/CFO are a strong conviction signal — always mention in thesis.
  - price_context: multi-day chart setup — breakout_today=True is a strong ST signal, consolidation_days≥10 means coiling energy.
  - obv_positive=True: On-Balance Volume rising — smart money accumulating. Confirm with volume_ratio.
  - analyst_target_mean / analyst_upside_pct: Wall Street consensus — large upside supports LT thesis.
  - analyst_buy / analyst_hold / analyst_sell: count of Wall Street ratings — high buy count reinforces LT conviction.
  - analyst_consensus: "buy" | "hold" | "sell" — Wall Street consensus in one word.
  - eps_beats / eps_misses: EPS beat/miss pattern last 4 quarters — consistent beaters (3+/4) have follow-through momentum; mention in LT thesis.
  - last_eps_surprise_pct: how much the company beat or missed last quarter — large beats are bullish catalysts.
  - pct_below_52w_high: distance from 52-week high. Near 0% = at highs (momentum), near 30%+ = deep discount (value play or troubled).
  - rs_vs_spy: 20-day return vs SPY. Positive = outperforming market (strength); negative = lagging (headwind). Always mention if >+5% or <-5%.
  - short_float_pct / days_to_cover: % of float sold short and days to cover. >15% float short = heavily shorted — either squeeze fuel or fundamental red flag. Mention if >10%.
  - inst_own_pct: institutional ownership %. >60% = smart money conviction; <20% = retail-dominated or undiscovered. Note if unusually high or low.
  - atr_pct / suggested_stop_pct: ATR as % of price and 1.5× ATR stop suggestion. Always include suggested_stop_pct in ST picks as the stop-loss level (e.g. "stop: -X% from entry").
  - days_to_earnings: days until next earnings. <5 days = binary risk — flag as "⚠️ earnings in X days" in the thesis. Never pick a stock 1-2 days before earnings without flagging it prominently.
  - patterns: list of detected chart patterns from OHLCV data. "bull_flag" and "bullish_engulfing" are strong ST signals — always mention by name in the thesis if present. "three_white_soldiers" signals sustained buying pressure. "hammer" near support signals reversal. "morning_star" confirms trend reversal after pullback. Multiple patterns on the same ticker = high-conviction ST setup.
  - congress_score / congress_buys / congress_members / is_cluster: congressional STOCK Act purchase disclosures (30-45 day lag). is_cluster=True means 2+ members bought — this is a rare, high-conviction signal; always highlight in LT thesis as "Congressional cluster buy". Single buy (score=3) adds modest weight; cluster buy (score=10) is a significant LT catalyst. Note the disclosure lag — trades already happened, this is confirmation not prediction.
  - sector rotation leaders/laggards from regime context: favour picks in leading sectors; flag lagging sectors as headwinds.
  - invalidation: one specific price level or event that would break the thesis (e.g. "closes below $145 support", "fails to hold 50-day MA", "earnings miss next week")
  - catalyst: the single most important upcoming trigger for this pick (e.g. "Q2 earnings May 28 — 4/4 beats history", "Fed rate decision June 12", "breakout above $840 52-week high", "product launch next week"). For LT picks use a fundamental catalyst.
  - entry_low / entry_high (ST stocks and crypto only): ideal buy zone. If entry is at a key level, set entry_low = entry_price * 0.99, entry_high = entry_price * 1.005. If chasing is a big risk (momentum stock, already up today), tighten: entry_high = entry_price * 1.002.
  - "plain_english": one sentence (max 20 words) for a complete beginner — what this company does and why you're buying now. No jargon.

Stock Candidates:
{json.dumps(stock_candidates, indent=2)}

Crypto Candidates:
{json.dumps(crypto_candidates, indent=2)}

ETF CONVICTION RUBRIC (applies to ETF picks only — stock rubric does not apply):
  ETF signals available: ret_1m (1-month momentum %), rsi (14-day), vol_ratio (10d avg / 30d avg volume)
  ★★★★★ (5) — ALL THREE: ret_1m > +5%, RSI 40-65 (healthy, not overbought), vol_ratio > 1.1
  ★★★★  (4) — TWO of the above confirming signals
  ★★★   (3) — ONE clear signal: either ret_1m > +3%, RSI recovering from <40, or vol_ratio > 1.15
  ★★    (2) — Marginal or conflicting (e.g. strong momentum but RSI > 72, or weak vol trend)
  ★     (1) — DO NOT INCLUDE. Return [] instead.
  REGIME ALIGNMENT (hard filter — apply before conviction):
    bear / volatile regime: ONLY include broad defensive ETFs (SPY, IVV, VTI, GLD, TLT). Drop sector/thematic ETFs.
    bull / elevated regime: sector and thematic ETFs acceptable if signals confirm.
    neutral regime: broad market ETFs only; require conviction ★★★★+ for sector picks.
  ETF picks are optional — return [] for both if nothing clears ★★★ after regime filter.

ETF Candidates:
{json.dumps(etf_candidates or [], indent=2)}

Commodity Candidates:
{json.dumps(commodity_candidates or [], indent=2)}

COMMODITY CONVICTION RUBRIC (applies to commodity picks only — use instead of ST stock rubric):
  Commodity signals available: ret_1m (1-month momentum %), rsi (14-day), vol_ratio, macro context.
  ★★★★★ (5) — ALL THREE: ret_1m > +5%, RSI 40-65, vol_ratio > 1.1 AND clear macro catalyst
  ★★★★  (4) — TWO of: ret_1m > +3%, RSI recovering or healthy, vol_ratio > 1.05, macro catalyst present
  ★★★   (3) — ONE strong signal: either ret_1m > +5% alone, or clear macro catalyst (rate cut bets, supply shock, inflation hedge)
  ★★    (2) — Marginal or macro noise only. Skip.
  ★     (1) — DO NOT INCLUDE. Return [] instead.
  Minimum conviction for commodities: ★★★. Return [] when nothing clears this bar.

COMMODITIES (OPTIONAL — 0–2 picks):
  Use only tickers from the commodity candidates above. Apply the COMMODITY CONVICTION RUBRIC above.
  Good examples: gold breaking out on rate cut bets, oil supply shock, silver momentum surge.
  Return [] for both short_term and long_term when nothing reaches ★★★.
  Short-term: entry/target/stop, 2-4 week horizon.
  Long-term: entry/target only, 3-12 month horizon.

OPTIONS PLAYS (OPTIONAL — 0–2 plays):
  For ST stock picks that have conviction 4 or 5, you may suggest a simple directional play.
  STRIKE RULE: use nearest_otm_call from the pick's options_flow data as the strike, and
    nearest_call_expiry as the expiry. If nearest_otm_call is absent, describe the play
    directionally only (e.g. "bullish call spread") — do NOT invent or estimate a strike price.
  Format: ticker, CALL or PUT, strike (from nearest_otm_call), expiry (from nearest_call_expiry), note under 12 words.
  Return [] if no ST stock picks have conviction 4+.
  These are illustrative / educational only — not buy recommendations.

Return this exact JSON structure:
{{
  "daily_summary": "one sentence overall market mood covering both stocks and crypto",
  "stocks": {{
    "short_term": [
      {{
        "ticker": "AAPL",
        "company": "Apple Inc",
        "action": "BUY",
        "entry_price": 182.50,
        "target_price": 197.10,
        "stop_loss": 173.38,
        "allocation": 12.50,
        "conviction": 4,
        "thesis": "what's happening technically + fundamental reason, max 25 words",
        "catalyst": "single most important upcoming trigger, max 12 words",
        "invalidation": "one condition that breaks this setup, max 12 words",
        "risk": "one sentence risk, max 10 words",
        "plain_english": "one sentence for a beginner — what this company does and why buying now, max 20 words",
        "theme": "market theme / narrative this pick rides, 2–4 words (e.g. 'AI infrastructure', 'rate cut play', 'consumer resilience', 'energy transition')",
        "edge": "why this ticker specifically over peers — max 15 words (e.g. 'Only pure-play with both fab and design', 'Highest margin in sector with accelerating buybacks')",
        "entry_low":  null,
        "entry_high": null,
        "earnings_date": "Thu May 1 or omit if no earnings this week"
      }}
    ],
    "long_term": [
      {{
        "ticker": "MSFT",
        "company": "Microsoft Corp",
        "action": "BUY",
        "entry_price": 415.00,
        "target_price": 500.00,
        "allocation": 16.67,
        "conviction": 5,
        "thesis": "fundamental strength + growth driver + valuation context, max 25 words",
        "catalyst": "single most important upcoming fundamental trigger, max 12 words",
        "invalidation": "one condition that breaks this setup, max 12 words",
        "plain_english": "one sentence for a beginner — what this company does and why buying now, max 20 words",
        "theme": "market theme / narrative this pick rides, 2–4 words",
        "edge": "why this ticker specifically over peers — max 15 words",
        "horizon": "2-3 years",
        "earnings_date": "Thu May 1 or omit if no earnings this week"
      }}
    ]
  }},
  "crypto": {{
    "short_term": [
      {{
        "id": "bitcoin",
        "symbol": "BTC",
        "name": "Bitcoin",
        "action": "BUY",
        "entry_price": 65000,
        "target_price": 72000,
        "stop_loss": 61750,
        "allocation": 10.00,
        "conviction": 3,
        "thesis": "momentum + on-chain or macro driver, max 25 words",
        "catalyst": "single most important upcoming trigger, max 12 words",
        "invalidation": "one condition that breaks this setup, max 12 words",
        "risk": "one sentence risk, max 10 words",
        "plain_english": "one sentence for a beginner — what this asset is and why buying now, max 20 words",
        "entry_low":  null,
        "entry_high": null
      }}
    ]
  }},
  "etfs": {{
    "short_term": [
      {{
        "ticker": "QQQ",
        "name": "Invesco QQQ Trust",
        "action": "BUY",
        "entry_price": 450.00,
        "target_price": 475.00,
        "stop_loss": 427.50,
        "conviction": 3,
        "thesis": "technical setup + sector driver, max 25 words",
        "catalyst": "single most important upcoming trigger, max 12 words",
        "invalidation": "one condition that breaks this setup, max 12 words",
        "risk": "one sentence risk, max 10 words",
        "plain_english": "one sentence for a beginner — what this ETF holds and why buying now, max 20 words"
      }}
    ],
    "long_term": [
      {{
        "ticker": "VTI",
        "name": "Vanguard Total Stock Market ETF",
        "action": "BUY",
        "entry_price": 250.00,
        "target_price": 290.00,
        "conviction": 4,
        "thesis": "macro + valuation basis for long-term allocation, max 25 words",
        "catalyst": "single most important upcoming fundamental trigger, max 12 words",
        "invalidation": "one condition that breaks this setup, max 12 words",
        "plain_english": "one sentence for a beginner — what this ETF holds and why buying now, max 20 words",
        "horizon": "1-3 years"
      }}
    ]
  }},
  "commodities": {{
    "short_term": [
      {{
        "ticker": "GLD",
        "name": "SPDR Gold Shares",
        "action": "BUY",
        "entry_price": 220.00,
        "target_price": 235.00,
        "stop_loss": 209.00,
        "conviction": 4,
        "thesis": "macro driver + technical setup for commodity, max 25 words",
        "catalyst": "single most important upcoming macro trigger, max 12 words",
        "invalidation": "one condition that breaks this setup, max 12 words",
        "risk": "one sentence risk, max 10 words",
        "plain_english": "one sentence for a beginner — what this commodity is and why buying now, max 20 words"
      }}
    ],
    "long_term": [
      {{
        "ticker": "GLD",
        "name": "SPDR Gold Shares",
        "action": "BUY",
        "entry_price": 220.00,
        "target_price": 260.00,
        "conviction": 3,
        "thesis": "macro driver + long-term supply/demand context, max 25 words",
        "catalyst": "single most important upcoming macro trigger, max 12 words",
        "invalidation": "one condition that breaks this setup, max 12 words",
        "plain_english": "one sentence for a beginner — what this commodity is and why buying now, max 20 words",
        "horizon": "6-12 months"
      }}
    ]
  }},
  "options_plays": [
    {{
      "ticker": "NVDA",
      "action": "CALL",
      "strike": 900.00,
      "expiry": "Jun 6, 2025",
      "note": "Ride the ST breakout with defined risk"
    }}
  ],
  "disclaimer": "For informational purposes only. Not financial advice. Crypto is highly volatile."
}}"""


# ── Shared helpers ────────────────────────────────────────────────────────────

def _strip_fences(raw: str) -> str:
    """Strip accidental markdown code-fence wrappers from a JSON string."""
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return raw




# ── Ticker validator ──────────────────────────────────────────────────────────

# Known crypto symbols — don't validate these through yfinance stock lookup.
# Canonical set (was a local hardcoded list).
from price_checker import CRYPTO_SYMBOLS as _KNOWN_CRYPTO


def _is_valid_ticker(ticker: str) -> bool:
    """Return True if yfinance can find a price for this ticker (i.e. it's real)."""
    try:
        fi    = yf.Ticker(ticker).fast_info
        price = getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None)
        return bool(price)
    except Exception:
        return False


def _validate_and_clean_picks(picks: dict, valid_stock_tickers: set) -> dict:
    """
    Walk every pick returned by Claude and drop any with invalid ticker symbols.
    - Stock/ETF tickers are validated against the known screener candidates first,
      then via a live yfinance lookup if the ticker wasn't in the candidates
      (Claude sometimes picks real tickers outside the candidate list — allow those).
    - Crypto symbols are checked against _KNOWN_CRYPTO.
    - Anything that fails both checks is logged and dropped silently.
    """
    def _clean_section(picks_list: list, is_crypto: bool = False) -> list:
        cleaned = []
        for p in picks_list:
            sym = (p.get("symbol") or p.get("ticker") or "").upper().strip()
            if not sym:
                continue
            if is_crypto:
                if sym in _KNOWN_CRYPTO:
                    cleaned.append(p)
                else:
                    print(f"[ai_analyzer] Dropped unknown crypto symbol: {sym}")
                continue
            # Stock / ETF path
            if sym in valid_stock_tickers:
                cleaned.append(p)        # fast path — in original candidates
                continue
            # Not in candidates — could be a valid ticker Claude added independently
            if _is_valid_ticker(sym):
                print(f"[ai_analyzer] {sym} not in candidates but valid — keeping.")
                cleaned.append(p)
            else:
                print(f"[ai_analyzer] Dropped invalid ticker: {sym} (not in candidates, yfinance failed)")
        return cleaned

    result = {}
    stocks = picks.get("stocks", {})
    crypto = picks.get("crypto", {})
    etfs   = picks.get("etfs", {})

    if stocks:
        result["stocks"] = {
            "short_term": _clean_section(stocks.get("short_term", []), is_crypto=False),
            "long_term":  _clean_section(stocks.get("long_term",  []), is_crypto=False),
        }
    if crypto:
        result["crypto"] = {
            "short_term": _clean_section(crypto.get("short_term", []), is_crypto=True),
            "long_term":  [],   # crypto long-term picks are not used
        }
    if etfs:
        result["etfs"] = {
            "short_term": _clean_section(etfs.get("short_term", []), is_crypto=False),
            "long_term":  _clean_section(etfs.get("long_term",  []), is_crypto=False),
        }

    commodities = picks.get("commodities", {})
    if commodities:
        result["commodities"] = {
            "short_term": _clean_section(commodities.get("short_term", []), is_crypto=False),
            "long_term":  _clean_section(commodities.get("long_term",  []), is_crypto=False),
        }

    # Options plays: validate that referenced ticker is known
    options_plays = picks.get("options_plays", [])
    if options_plays:
        clean_opts = []
        for o in options_plays:
            sym = (o.get("ticker") or "").upper().strip()
            if sym and (sym in valid_stock_tickers or _is_valid_ticker(sym)):
                clean_opts.append(o)
            else:
                print(f"[ai_analyzer] Dropped options play for unknown ticker: {sym}")
        if clean_opts:
            result["options_plays"] = clean_opts

    # Preserve any top-level keys Claude adds (macro_note, regime, etc.)
    for k, v in picks.items():
        if k not in ("stocks", "crypto", "etfs", "commodities", "options_plays"):
            result[k] = v

    total_before = sum(
        len(picks.get(a, {}).get(s, []))
        for a in ("stocks", "crypto", "etfs", "commodities") for s in ("short_term", "long_term")
    )
    total_after = sum(
        len(result.get(a, {}).get(s, []))
        for a in ("stocks", "crypto", "etfs", "commodities") for s in ("short_term", "long_term")
    )
    if total_before != total_after:
        print(f"[ai_analyzer] Ticker validation: {total_before} picks → {total_after} after cleaning "
              f"({total_before - total_after} dropped).")
    else:
        print(f"[ai_analyzer] Ticker validation: all {total_after} picks passed.")

    # ── Stop-loss fallback: any short-term pick missing stop_loss gets 5% below entry ──
    _ST_SECTIONS = [
        ("stocks",      "short_term"),
        ("crypto",      "short_term"),
        ("etfs",        "short_term"),
        ("commodities", "short_term"),
    ]
    for asset_key, timeframe in _ST_SECTIONS:
        for p in result.get(asset_key, {}).get(timeframe, []):
            if not p.get("stop_loss") and p.get("entry_price"):
                fallback = round(float(p["entry_price"]) * 0.95, 2)
                p["stop_loss"] = fallback
                sym = p.get("ticker") or p.get("symbol") or "?"
                print(f"[ai_analyzer] stop_loss missing for {sym} — applied 5% fallback: {fallback}")

    return result


# ── Claude call ───────────────────────────────────────────────────────────────

def _call_claude(system: str, user: str, model: str = "claude-sonnet-4-6",
                 use_caching: bool = False) -> dict:
    """Call Claude API and parse JSON response. Raises on failure.

    Args:
        system:      System prompt text (role / persona / rules).
        user:        User message content (candidate data + context).
        model:       Anthropic model string.
        use_caching: When True, marks the system block with cache_control so
                     Anthropic can cache identical static system prompts across
                     calls — approximately 10× cheaper on cached input tokens.
                     Use only for calls where the system text is truly static.
    """
    # Correct API semantics: system= is a separate top-level parameter, NOT
    # part of the messages array.  Combining them in a single user message
    # (old behaviour) breaks role separation and blocks prompt caching.
    if use_caching:
        system_param = [
            {"type": "text", "text": system,
             "cache_control": {"type": "ephemeral"}}
        ]
    else:
        system_param = system

    message = _get_client().messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system_param,
        messages=[{"role": "user", "content": user}],
    )
    return json.loads(_strip_fences(message.content[0].text.strip()))


# ── Public API ────────────────────────────────────────────────────────────────

def analyze_with_claude(
    screener_results: dict,
    config: dict,
    crypto_results: dict | None = None,
    recent_losers: list[str] | None = None,
    etf_results: dict | None = None,
) -> dict:
    """
    Main entry point. Accepts stock screener output + optional crypto + ETF screener output.
    Enriches stocks with Finnhub news, calls Claude once for all asset classes.
    Returns unified picks dict.
    """
    print("[ai_analyzer] Building stock candidates payload...")
    stock_candidates = _build_stock_candidates(screener_results)

    crypto_candidates = []
    if crypto_results:
        print("[ai_analyzer] Building crypto candidates payload...")
        crypto_candidates = _build_crypto_candidates(crypto_results)

    etf_candidates = []
    if etf_results:
        print("[ai_analyzer] Building ETF candidates payload...")
        etf_candidates = (
            etf_results.get("short_term", []) +
            [{**e, "_lt": True} for e in etf_results.get("long_term", [])]
        )

    print("[ai_analyzer] Building commodity candidates payload...")
    commodity_candidates = _build_commodity_candidates()

    # Build valid ticker set for post-analysis validation
    valid_stock_tickers: set = (
        {c["ticker"].upper() for c in stock_candidates if c.get("ticker")} |
        {e.get("ticker", "").upper() for e in etf_candidates if e.get("ticker")} |
        {c["ticker"].upper() for c in commodity_candidates if c.get("ticker")}
    )

    # Pass market regime context from screener results if available
    regime_info = screener_results.get("regime") if isinstance(screener_results, dict) else None

    user_prompt = _build_user_prompt(
        stock_candidates, crypto_candidates, config,
        recent_losers=recent_losers or [],
        regime_info=regime_info,
        pick_mode=config.get("pick_mode", "both"),
        etf_candidates=etf_candidates or [],
        commodity_candidates=commodity_candidates,
    )

    # Sonnet for main analysis — quality matters for picks.
    # use_caching=True adds cache_control to the system block so Anthropic
    # reuses the cached system prompt on repeat calls (≈10× cheaper).
    print("[ai_analyzer] Calling Claude Sonnet (stocks + crypto)...")
    try:
        picks = _call_claude(SYSTEM_PROMPT, user_prompt, model="claude-sonnet-4-6",
                             use_caching=True)
        print("[ai_analyzer] Claude response parsed successfully.")
        picks = _validate_and_clean_picks(picks, valid_stock_tickers)
    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        print(f"[ai_analyzer] Parse error on first attempt ({exc}). Retrying with Haiku...")
        try:
            picks = _call_claude(STRICT_RETRY_SYSTEM, user_prompt, model="claude-haiku-4-5-20251001")
            print("[ai_analyzer] Haiku retry succeeded.")
            picks = _validate_and_clean_picks(picks, valid_stock_tickers)
        except Exception as exc2:
            print(f"[ai_analyzer] Claude analysis failed after retry: {exc2}")
            raise RuntimeError(f"Claude analysis failed: {exc2}") from exc2

    # Fill in allocations now that we know actual pick counts
    _backfill_allocations(picks, config)
    return picks


def _backfill_allocations(picks: dict, config: dict) -> None:
    """
    After Claude returns picks, divide budget equally across actual pick count.
    Mutates picks in-place. Claude always returns allocation=null; we fill it here.
    """
    stock_budget  = config.get("stock_budget")
    crypto_budget = config.get("crypto_budget")

    if stock_budget:
        stock_picks = (
            picks.get("stocks",      {}).get("short_term", []) +
            picks.get("stocks",      {}).get("long_term",  []) +
            picks.get("etfs",        {}).get("short_term", []) +
            picks.get("etfs",        {}).get("long_term",  []) +
            picks.get("commodities", {}).get("short_term", []) +
            picks.get("commodities", {}).get("long_term",  [])
        )
        n = len(stock_picks)
        if n:
            per = round(float(stock_budget) / n, 2)
            for p in stock_picks:
                p["allocation"] = per
            print(f"[ai_analyzer] Stock allocation: ${stock_budget} / {n} picks = ${per} each")

    if crypto_budget:
        crypto_picks = picks.get("crypto", {}).get("short_term", [])
        n = len(crypto_picks)
        if n:
            per = round(float(crypto_budget) / n, 2)
            for p in crypto_picks:
                p["allocation"] = per
            print(f"[ai_analyzer] Crypto allocation: ${crypto_budget} / {n} picks = ${per} each")


# ── Personalization helpers ───────────────────────────────────────────────────

def personalize_picks(picks: dict, open_positions: list[dict], risk_profile: str = "moderate") -> dict:
    """
    Use Claude Haiku to write a one-line "why this fits YOUR portfolio" note per pick.

    Args:
        picks:          Claude's daily picks dict (stocks + crypto sections).
        open_positions: List of user's open trade dicts from their trade log.
        risk_profile:   "conservative" | "moderate" | "aggressive"

    Returns:
        dict mapping ticker/symbol → personalized one-line note (max 10 words).
        Empty dict on failure (graceful degradation).
    """
    if not open_positions and risk_profile == "moderate":
        return {}   # No context to personalize with — skip the Haiku call

    stocks = picks.get("stocks", {})
    crypto = picks.get("crypto", {})
    etfs   = picks.get("etfs", {})
    comms  = picks.get("commodities", {})
    all_picks = (
        [p.get("ticker", "") for p in stocks.get("short_term", [])] +
        [p.get("ticker", "") for p in stocks.get("long_term",  [])] +
        [c.get("symbol", "") for c in crypto.get("short_term", [])] +
        [e.get("ticker", "") for e in etfs.get("short_term", [])] +
        [e.get("ticker", "") for e in etfs.get("long_term",  [])] +
        [c.get("ticker", "") for c in comms.get("short_term", [])] +
        [c.get("ticker", "") for c in comms.get("long_term",  [])]
    )
    all_picks = [t for t in all_picks if t]
    if not all_picks:
        return {}

    pos_summary = []
    for t in open_positions[:8]:   # cap at 8 to keep prompt short
        ticker = t.get("ticker", "")
        ret    = t.get("return_pct")
        sector = t.get("sector", "")
        if ticker:
            ret_str = f" ({ret:+.1f}%)" if ret is not None else ""
            pos_summary.append(f"{ticker}{ret_str}{' · ' + sector if sector else ''}")

    pos_text = (", ".join(pos_summary)) if pos_summary else "no current positions"

    system = (
        "You are a personal portfolio advisor writing brief context notes for pre-selected picks. "
        "IMPORTANT: All picks have already been vetted and approved — do NOT question, warn against, "
        "or discourage any pick. Your only job is to explain why each pick is interesting for THIS user. "
        "For each ticker, write ONE very short note (max 10 words) that highlights opportunity or portfolio fit. "
        "Frame crypto as: diversification, high-growth exposure, momentum play, or sector complement. "
        "Reference existing holdings when relevant (e.g. 'complements your NVDA position'). "
        "Never say 'too volatile', 'skip', 'excessive', 'risky', or any discouraging phrase. "
        "Return ONLY valid JSON: {\"AAPL\": \"...\", \"BTC\": \"...\"}. No other text."
    )
    user_msg = (
        f"User's current open positions: {pos_text}\n"
        f"User's risk profile: {risk_profile} (context only — do not use this to warn against any pick)\n"
        f"Today's picks to personalise: {', '.join(all_picks)}\n\n"
        f"Return a JSON object with one short note per ticker. Be positive and specific."
    )

    try:
        message = _get_client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": f"{system}\n\n{user_msg}"}],
        )
        result = json.loads(_strip_fences(message.content[0].text.strip()))
        print(f"[ai_analyzer] Personalized notes generated for {list(result.keys())}")
        return result if isinstance(result, dict) else {}
    except Exception as exc:
        print(f"[ai_analyzer] personalize_picks failed (non-critical): {exc}")
        return {}


def personalize_picks_batch(
    picks: dict,
    users_data: list[dict],
    batch_size: int = 20,
) -> dict[str, dict]:
    """
    Batch-personalize picks for multiple users — replaces N per-user Haiku calls
    with ceil(N / batch_size) calls.  ~94% cost reduction vs calling per-user.

    Args:
        picks:      Daily picks dict (same structure as analyze_with_claude output).
        users_data: List of {uid, positions, risk_profile} dicts.
                    Only include users who actually have open positions.
        batch_size: Max users per Haiku call (default 20 keeps prompt under ~4K tokens).

    Returns:
        {uid: {ticker: note_str}} — missing uid means no notes (graceful skip).
    """
    stocks    = picks.get("stocks", {})
    crypto    = picks.get("crypto", {})
    all_picks = (
        [p.get("ticker", "") for p in stocks.get("short_term", [])] +
        [p.get("ticker", "") for p in stocks.get("long_term",  [])] +
        [c.get("symbol", "") for c in crypto.get("short_term", [])]
    )
    all_picks = [t for t in all_picks if t]
    if not all_picks or not users_data:
        return {}

    picks_str = ", ".join(all_picks)
    system = (
        "You are a personal portfolio advisor. All picks have been pre-vetted — never warn against them. "
        "For each user and each pick, write ONE very short note (max 10 words) explaining why it fits "
        "their portfolio: reference their holdings, risk profile, or sector exposure. "
        "Frame crypto as: diversification, momentum, or high-growth exposure. "
        "Never use discouraging language. "
        "Return ONLY valid JSON: {\"uid\": {\"TICKER\": \"note\"}, ...}. No other text."
    )

    results: dict[str, dict] = {}
    # Process in batches
    for i in range(0, len(users_data), batch_size):
        batch = users_data[i : i + batch_size]

        # Build compact user summaries
        user_lines = []
        for u in batch:
            uid      = u["uid"]
            pos_list = u.get("positions", [])
            risk     = u.get("risk_profile", "moderate")
            pos_str  = ", ".join(
                f"{t.get('ticker','')}({t.get('return_pct', 0):+.0f}%)"
                for t in pos_list[:6] if t.get("ticker")
            ) or "none"
            user_lines.append(f"  {uid}: holdings=[{pos_str}] risk={risk}")

        user_block = "\n".join(user_lines)
        user_msg   = (
            f"Today's picks: {picks_str}\n\n"
            f"Users:\n{user_block}\n\n"
            f"Return a JSON object with one note per ticker per user."
        )

        try:
            # Output budget: ~15 tokens per note × picks × users in batch + JSON overhead
            _out_tokens = max(1024, len(all_picks) * len(batch) * 18 + 400)
            message = _get_client().messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=min(_out_tokens, 4096),  # cap at 4096 (Haiku max output)
                messages=[{"role": "user", "content": f"{system}\n\n{user_msg}"}],
            )
            parsed = json.loads(_strip_fences(message.content[0].text.strip()))
            if isinstance(parsed, dict):
                results.update(parsed)
            print(f"[ai_analyzer] Batch personalisation: {len(batch)} users, batch {i // batch_size + 1}")
        except Exception as exc:
            print(f"[ai_analyzer] personalize_picks_batch failed for batch {i // batch_size + 1}: {exc}")
            # Graceful degradation — affected users just get no notes

    return results


def generate_trade_debrief(trade: dict) -> str:
    """
    Use Claude Haiku to write a 2-3 sentence debrief after a trade closes.
    Uses the original pick thesis to give context-aware, actionable feedback.

    For stop-outs: tells the user whether the thesis is broken or it's noise,
    and suggests re-entry conditions if the setup still has merit.
    For targets: reinforces what worked.
    For expired: explains what stalled and what to watch for.

    Args:
        trade: Closed trade dict — must include ticker, entry_price, closed_price,
               return_pct, outcome, gain_usd. Optionally includes thesis.

    Returns:
        A short debrief string (2-3 sentences), or "" on failure.
    """
    ticker     = trade.get("ticker", "")
    entry      = trade.get("entry_price", "?")
    exit_price = trade.get("closed_price", "?")
    ret        = trade.get("return_pct", 0)
    outcome    = trade.get("outcome", "")
    gain       = trade.get("gain_usd", 0)
    thesis     = trade.get("thesis", "")

    sign = "+" if ret >= 0 else ""

    outcome_desc = {
        "target":  "hit its profit target",
        "stop":    "hit the stop-loss",
        "expired": "expired before hitting target or stop",
    }.get(outcome, "closed")

    thesis_line = f"\nOriginal thesis: \"{thesis}\"" if thesis else ""

    if outcome == "stop":
        system = (
            "You are a concise trading coach. A position just hit its stop-loss. "
            "Write exactly 2-3 sentences:\n"
            "1. What likely caused the stop to hit (market or technical reason).\n"
            "2. Whether the original thesis is broken or just temporarily invalidated.\n"
            "3. If the thesis still has merit, give ONE specific re-entry condition "
            "   (e.g. 'Re-enter if price reclaims $X with volume'). "
            "   If thesis is broken, say so plainly and move on.\n"
            "Be direct and specific. Under 50 words. No disclaimers."
        )
    elif outcome == "target":
        system = (
            "You are a concise trading coach. A position just hit its profit target. "
            "Write exactly 2 sentences:\n"
            "1. What drove the move (market or technical reason).\n"
            "2. One key takeaway to apply to future similar setups.\n"
            "Be warm and specific. Under 35 words. No disclaimers."
        )
    else:  # expired
        system = (
            "You are a concise trading coach. A position expired without hitting "
            "its target or stop. Write exactly 2 sentences:\n"
            "1. What likely caused the thesis to stall.\n"
            "2. What signal to look for before re-entering this type of setup.\n"
            "Be direct and specific. Under 40 words. No disclaimers."
        )

    user_msg = (
        f"{ticker} {outcome_desc} at ${exit_price} (entry ${entry}). "
        f"Return: {sign}{ret}% (${gain:+.2f}).{thesis_line}"
    )

    try:
        message = _get_client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": f"{system}\n\n{user_msg}"}],
        )
        return message.content[0].text.strip()
    except Exception as exc:
        print(f"[ai_analyzer] generate_trade_debrief failed (non-critical): {exc}")
        return ""


# ── /swap: generate one alternative pick from a single screener candidate ─────

def get_swap_pick(candidate: dict, category: str = "short_term", config: dict | None = None) -> dict | None:
    """
    Generate a single alternative pick from one screener candidate.
    Called by the /swap command. Returns a pick dict (same shape as daily picks)
    or None if the candidate is too weak to recommend.

    Steps:
      1. Enrich the candidate with news + signals (reuses _build_stock_candidates logic)
      2. Call Claude with a focused single-pick prompt
      3. Return the parsed pick or None
    """
    config = config or {}

    # ── Enrich: reuse the same pipeline as the morning run ───────────────────
    screener_input = {category: [candidate]}
    enriched = _build_stock_candidates(screener_input)
    if not enriched:
        return None
    cand = enriched[0]

    # ── Regime context ────────────────────────────────────────────────────────
    regime_info = None
    try:
        from market_regime import get_market_regime
        regime_info = get_market_regime()
    except Exception:
        pass

    regime_note = ""
    if regime_info and regime_info.get("regime"):
        regime_note = f"\nCurrent market regime: {regime_info['regime'].upper()}. Adjust conviction accordingly."

    is_lt = category == "long_term"
    horizon_note = (
        "LONG-TERM pick (1-5 year hold). Set realistic target: 8-18% annualised return. No stop_loss needed."
        if is_lt else
        "SHORT-TERM pick (1-4 week trade). Include entry_price, target_price, stop_loss (ATR-based if available)."
    )

    risk_profile = config.get("risk_profile", "moderate")
    # Conservative: require ★★★★ (3 signals). Moderate/aggressive: allow ★★★ (2 signals).
    # The post-Claude conviction gate in agent.py does final filtering per user preference.
    min_stars = 4 if risk_profile == "conservative" else 3

    prompt = f"""You are a financial analyst. Evaluate this single stock candidate and decide if it is worth recommending as a {category.replace('_', '-')} pick.

Candidate data:
{json.dumps(cand, indent=2)}
{regime_note}

TASK: {horizon_note}

CONVICTION RUBRIC (count confirming signals):
  ★★★★★ (5) — 4+ confirming signals
  ★★★★  (4) — 3 confirming signals
  ★★★   (3) — 2 confirming signals
  ★★    (2) — 1 signal or conflicting — DO NOT INCLUDE
  ★     (1) — DO NOT INCLUDE

What counts as one confirming signal:
  • RSI 35-55 AND MACD crossover (both = 1 signal)
  • volume_ratio > 1.5 AND obv_positive = True (both = 1 signal)
  • breakout_today = True OR bull_flag / bullish_engulfing pattern
  • options_flow: unusual = True OR bullish_flow = True OR put_call_ratio < 0.7
  • insider_activity present AND is_cluster = True
  • analyst_consensus = "buy" AND analyst_upside_pct > 10%
  • eps_beats >= 3 AND last_eps_surprise_pct > 3%
  • rs_vs_spy > +5%
  • congress_score >= 6

MINIMUM CONVICTION for this user's risk profile ({risk_profile}): {min_stars} stars.
If conviction < {min_stars}, return {{"pick": null, "reason": "below conviction threshold"}}.

Return ONLY valid JSON — no preamble, no markdown:
{{
  "pick": {{
    "ticker": "...",
    "company": "...",
    "action": "BUY",
    "entry_price": 0.0,
    "target_price": 0.0,
    "stop_loss": 0.0,
    "conviction": 4,
    "thesis": "max 25 words",
    "catalyst": "max 12 words",
    "invalidation": "max 12 words"
  }},
  "reason": "brief explanation of conviction decision"
}}
"""

    client = _get_client()
    for attempt in range(2):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=600,
                system="You are a financial analysis assistant. Return ONLY valid JSON. No preamble, no markdown.",
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text.strip()
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            result = json.loads(raw)
            pick = result.get("pick")
            if not pick:
                print(f"[ai_analyzer] swap: candidate {cand.get('ticker')} below threshold — {result.get('reason', '')}")
                return None
            # Tag the category so callers know what type of pick this is
            pick["_category"] = category
            return pick
        except Exception as exc:
            print(f"[ai_analyzer] get_swap_pick attempt {attempt+1} failed: {exc}")
            if attempt == 1:
                return None


# ── CLI test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import pprint
    mock_stocks = {
        "short_term": [
            {"ticker": "AAPL", "company": "Apple Inc", "sector": "Technology",
             "current_price": 182.50, "score": 85, "rsi": 48.2,
             "macd_crossover": True, "volume_ratio": 1.8},
        ],
        "long_term": [
            {"ticker": "MSFT", "company": "Microsoft Corp", "sector": "Technology",
             "current_price": 415.00, "score": 90, "pe_ratio": 32,
             "revenue_growth": 0.17, "debt_to_equity": 0.45, "market_cap": 3_000_000_000_000},
        ],
    }
    mock_crypto = {
        "short_term": [
            {"id": "bitcoin", "symbol": "BTC", "name": "Bitcoin",
             "current_price": 65000, "score": 80, "rsi": 55.0,
             "volume_ratio": 1.7, "price_change_24h_pct": 3.2},
        ],
        "long_term": [
            {"id": "ethereum", "symbol": "ETH", "name": "Ethereum",
             "current_price": 3200, "score": 85, "market_cap": 385_000_000_000,
             "price_change_30d_pct": 12.5, "pct_below_ath": 34.0},
        ],
    }
    mock_config = {
        "stock_budget": 200, "crypto_budget": 50,
        "max_short_picks": 2, "max_long_picks": 3,
        "max_crypto_short_picks": 2, "max_crypto_long_picks": 2,
    }
    picks = analyze_with_claude(mock_stocks, mock_config, mock_crypto)
    pprint.pprint(picks)
