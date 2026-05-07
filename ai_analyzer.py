"""
ai_analyzer.py — Claude API integration for stock + crypto analysis.
Accepts screener candidates, enriches stocks with Finnhub news, returns structured picks.
"""

import os
import json
import time
import anthropic
import yfinance as yf

from sentiment_analyzer import get_sentiment
from options_flow import get_options_signal
from insider_tracker import get_insider_signal
from config_manager import (
    load_signal_cache, save_signal_cache,
    get_cached_signal, set_cached_signal,
)

MAX_TOKENS = 1500   # ~1000-1200 tokens actual output for 9 picks; 1500 gives safe headroom


# ── News via yfinance (no API key needed) ─────────────────────────────────────

def _get_news_headlines(ticker: str, max_headlines: int = 3) -> list[str]:
    """Fetch recent news headlines for a ticker via yfinance (free, no key)."""
    try:
        news = yf.Ticker(ticker).news or []
        return [n.get("title", "") for n in news[:max_headlines] if n.get("title")]
    except Exception as exc:
        print(f"[ai_analyzer] News fetch error for {ticker}: {exc}")
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
        # instead of $85). Fetch a fresh close and compare; drop if >3x or <1/3.
        raw_price = stock.get("current_price")
        if raw_price and raw_price > 0:
            try:
                import yfinance as yf
                live = yf.Ticker(ticker).fast_info.get("last_price") or yf.Ticker(ticker).fast_info.get("previous_close")
                if live and live > 0:
                    ratio = raw_price / live
                    if ratio > 3.0 or ratio < 0.33:
                        print(f"[ai_analyzer] Price sanity fail for {ticker}: "
                              f"cached={raw_price:.2f} vs live={live:.2f} — dropping candidate.")
                        skip = True
            except Exception:
                pass  # live fetch failed — let candidate through, Claude will handle it

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
            "pe_ratio":      stock.get("pe_ratio"),
            "revenue_growth":stock.get("revenue_growth"),
            "debt_to_equity":stock.get("debt_to_equity"),
            "market_cap":    stock.get("market_cap"),
            "news_headlines":[],
        }

        # Earnings within 5 days — pass through to Claude
        if stock.get("earnings_date"):
            entry["earnings_date"] = stock["earnings_date"]

        if ticker not in seen:
            entry["news_headlines"] = _get_news_headlines(ticker)

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
                    if opts.get("unusual") or opts.get("bullish_flow") or opts.get("bearish_flow"):
                        entry["options_flow"] = {
                            "unusual":        opts["unusual"],
                            "put_call_ratio": opts["put_call_ratio"],
                            "bullish_flow":   opts["bullish_flow"],
                            "bearish_flow":   opts["bearish_flow"],
                            "note":           opts["note"],
                        }
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
        [("short_term", c) for c in crypto_results.get("short_term", [])] +
        [("long_term",  c) for c in crypto_results.get("long_term",  [])]
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
  - Maximum 1 short-term stock pick regardless of budget.
  - Reduce crypto allocations by 50%; skip crypto short-term entirely if alternatives exist.
  - Long-term picks only from companies with positive revenue growth and D/E < 0.5."""
    if profile == "aggressive":
        return """
RISK PROFILE: aggressive
  - Include picks with conviction ★★★ and above — strong setup counts even if risky.
  - All sectors welcome including high-beta: Technology, Energy, Consumer Discretionary.
  - Stop-losses: set 8% below entry (wider room to breathe).
  - Maximise pick counts within budget — fill all slots.
  - Full crypto allocations; include higher-risk coins with strong momentum.
  - Short-term targets can be 10-15% above entry for high-momentum plays."""
    # moderate (default)
    return """
RISK PROFILE: moderate (default)
  - Standard conviction threshold ★★★ minimum.
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
) -> str:
    # Pre-build conditional blocks (backslashes not allowed inside f-string expressions)
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

    # Market regime block
    if regime_info and regime_info.get("regime"):
        r = regime_info
        regime_block = (
            f"MARKET REGIME: {r['regime'].upper()}\n"
            f"  VIX: {r.get('vix', 'N/A')} | SPY above 50MA: {r.get('spy_above_50ma')} "
            f"| SPY above 200MA: {r.get('spy_above_200ma')}\n"
            f"  Note: {r.get('note', '')}\n"
            f"  Adjust pick aggressiveness accordingly:\n"
            f"    bull → normal operation\n"
            f"    neutral → normal, add brief caution note\n"
            f"    volatile → prefer lower-beta picks, mention risk in thesis\n"
            f"    bear → defensive sectors only (Utilities, Consumer Staples, Health Care), "
            f"skip high-momentum plays"
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

    # ── Compute per-pick allocations in code (equal split, not Claude's job) ────
    stock_budget  = config.get("stock_budget")   # None = unset
    crypto_budget = config.get("crypto_budget")  # None = unset

    max_stock_picks  = config.get("max_short_picks", 2) + config.get("max_long_picks", 3)
    max_crypto_picks = config.get("max_crypto_short_picks", 2) + config.get("max_crypto_long_picks", 2)

    if stock_budget:
        per_stock = round(float(stock_budget) / max(max_stock_picks, 1), 2)
        stock_alloc_note = (
            f"  Budget: ${stock_budget} total for stocks today, split equally → "
            f"${per_stock} per pick. Set 'allocation' to {per_stock} for every stock pick.\n"
        )
    else:
        per_stock = None
        stock_alloc_note = "  No budget set — set 'allocation' to null for all stock picks.\n"

    if crypto_budget:
        per_crypto = round(float(crypto_budget) / max(max_crypto_picks, 1), 2)
        crypto_alloc_note = (
            f"  Budget: ${crypto_budget} total for crypto today, split equally → "
            f"${per_crypto} per pick. Set 'allocation' to {per_crypto} for every crypto pick.\n"
        )
    else:
        per_crypto = None
        crypto_alloc_note = "  No budget set — set 'allocation' to null for all crypto picks.\n"

    stocks_block = ""
    if show_st:
        stocks_block += f"  Short-term: Keep best {config.get('max_short_picks', 2)} stocks (target gains within 1-4 weeks)\n"
    if show_lt:
        stocks_block += f"  Long-term: Keep best {config.get('max_long_picks', 3)} stocks (dollar-cost average over 1-5 years)\n"
    stocks_block += stock_alloc_note

    return f"""Analyze these stock AND crypto candidates for a personal investor.
{f"{mode_note}" + chr(10) if mode_note else ""}
STOCKS:
{stocks_block}

ALLOCATION RULE (STRICTLY ENFORCE):
  - Allocation values are pre-calculated and given to you above. Do NOT change them.
  - Every stock pick must use the exact same allocation value (or null if no budget set).
  - Do NOT weight by conviction — conviction affects selection only, not allocation size.

SECTOR DIVERSITY RULE (STRICTLY ENFORCE):
  - Short-term: the 2 picks MUST be from different sectors. No exceptions.
    If the top 2 are from the same sector, drop the lower-scored one and take the next
    highest-scored stock from a different sector.
  - Long-term: no 2 of the 3 picks may share the same sector. If the top 3 by score
    include duplicates, replace the lower-scored duplicate with the best-scored stock
    from an unrepresented sector. A pick at 60% score from a new sector beats a pick
    at 65% score from an already-represented sector.

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
      Crypto long-term:        20-40% per year (higher volatility)
  - Example: a 2-3 year tech pick at $424 entry → realistic target $560-650, NOT $800+
  - Do NOT extrapolate recent momentum into long-term targets.
  - If a stock's target implies >25% annualised return, reduce it to 20% max.
  - CRYPTO LONG-TERM CAP: Maximum total return of 50% over the full horizon regardless
    of ATH distance or past performance. Do NOT set crypto LT targets implying 100-200%+ gains.

CRYPTO:
{"  Short-term: Keep best " + str(config.get('max_crypto_short_picks', 2)) + " crypto (target gains within 1-2 weeks, high risk)" if show_st else ""}
{"  Long-term: Keep best " + str(config.get('max_crypto_long_picks', 2)) + " crypto (hold 6-24 months)" if show_lt else ""}
{crypto_alloc_note}

CRYPTO DEDUPLICATION RULE (HARD RULE — ZERO EXCEPTIONS):
  - Each crypto symbol may appear in AT MOST ONE category (short_term OR long_term, NEVER both).
  - If a coin scores well in both categories, place it only in the category where it scores highest.
  - Fill the other slot with the next-best coin that does NOT already appear in any category.

{regime_block}

{risk_block}

{losers_block}

{excluded_block}

SIGNAL GUIDANCE (use in thesis where relevant):
  - social_sentiment: StockTwits + Reddit signal. Label "bullish"/"hot" supports picks; "bearish" is a red flag.
  - options_flow: unusual call volume or low put/call ratio confirms bullish bets by institutional traders.
  - insider_activity: recent open-market buys by CEO/CFO are a strong conviction signal — always mention in thesis.
  - analyst_target_mean / analyst_upside_pct: Wall Street consensus — large upside supports LT thesis.

Stock Candidates:
{json.dumps(stock_candidates, indent=2)}

Crypto Candidates:
{json.dumps(crypto_candidates, indent=2)}

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
        "thesis": "one sentence why, max 15 words",
        "risk": "one sentence risk, max 10 words",
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
        "thesis": "one sentence why, max 15 words",
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
        "thesis": "one sentence why, max 15 words",
        "risk": "one sentence risk, max 10 words"
      }}
    ],
    "long_term": [
      {{
        "id": "ethereum",
        "symbol": "ETH",
        "name": "Ethereum",
        "action": "BUY",
        "entry_price": 3200,
        "target_price": 5000,
        "allocation": 15.00,
        "conviction": 4,
        "thesis": "one sentence why, max 15 words",
        "horizon": "12-18 months"
      }}
    ]
  }},
  "disclaimer": "For informational purposes only. Not financial advice. Crypto is highly volatile."
}}"""


# ── Claude call ───────────────────────────────────────────────────────────────

def _call_claude(system: str, user: str, model: str = "claude-sonnet-4-6") -> dict:
    """Call Claude API and parse JSON response. Raises on failure."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    message = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    raw = message.content[0].text.strip()
    # Strip accidental markdown fences
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


# ── Public API ────────────────────────────────────────────────────────────────

def analyze_with_claude(
    screener_results: dict,
    config: dict,
    crypto_results: dict | None = None,
    recent_losers: list[str] | None = None,
) -> dict:
    """
    Main entry point. Accepts stock screener output + optional crypto screener output.
    Enriches stocks with Finnhub news, calls Claude once for both asset classes.
    Returns unified picks dict.
    """
    print("[ai_analyzer] Building stock candidates payload...")
    stock_candidates = _build_stock_candidates(screener_results)

    crypto_candidates = []
    if crypto_results:
        print("[ai_analyzer] Building crypto candidates payload...")
        crypto_candidates = _build_crypto_candidates(crypto_results)

    # Pass market regime context from screener results if available
    regime_info = screener_results.get("regime") if isinstance(screener_results, dict) else None

    user_prompt = _build_user_prompt(
        stock_candidates, crypto_candidates, config,
        recent_losers=recent_losers or [],
        regime_info=regime_info,
        pick_mode=config.get("pick_mode", "both"),
    )

    # Sonnet for main analysis — quality matters for picks
    print("[ai_analyzer] Calling Claude Sonnet (stocks + crypto)...")
    try:
        picks = _call_claude(SYSTEM_PROMPT, user_prompt, model="claude-sonnet-4-6")
        print("[ai_analyzer] Claude response parsed successfully.")
        return picks
    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        print(f"[ai_analyzer] Parse error on first attempt ({exc}). Retrying with Haiku...")

    # Haiku for retry — just JSON reformatting, not fresh analysis
    try:
        picks = _call_claude(STRICT_RETRY_SYSTEM, user_prompt, model="claude-haiku-4-5-20251001")
        print("[ai_analyzer] Haiku retry succeeded.")
        return picks
    except Exception as exc2:
        print(f"[ai_analyzer] Claude analysis failed after retry: {exc2}")
        raise RuntimeError(f"Claude analysis failed: {exc2}") from exc2


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
    all_picks = (
        [p.get("ticker", "") for p in stocks.get("short_term", [])] +
        [p.get("ticker", "") for p in stocks.get("long_term",  [])] +
        [c.get("symbol", "") for c in crypto.get("short_term", [])] +
        [c.get("symbol", "") for c in crypto.get("long_term",  [])]
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
        "You are a personal portfolio advisor. For each stock/crypto ticker listed, "
        "write ONE very short note (max 10 words) explaining how it fits this specific user's portfolio. "
        "Be concrete and personal — reference their existing holdings or risk profile. "
        "Avoid generic phrases like 'good for growth'. "
        "Return ONLY valid JSON: {\"AAPL\": \"...\", \"BTC\": \"...\"}. No other text."
    )
    user_msg = (
        f"User's current open positions: {pos_text}\n"
        f"User's risk profile: {risk_profile}\n"
        f"Today's picks to personalise: {', '.join(all_picks)}\n\n"
        f"Return a JSON object with one short note per ticker."
    )

    try:
        client  = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
        print(f"[ai_analyzer] Personalized notes generated for {list(result.keys())}")
        return result if isinstance(result, dict) else {}
    except Exception as exc:
        print(f"[ai_analyzer] personalize_picks failed (non-critical): {exc}")
        return {}


def generate_trade_debrief(trade: dict) -> str:
    """
    Use Claude Haiku to write a 2-sentence debrief after a trade closes.

    Args:
        trade: Closed trade dict with keys: ticker, entry_price, closed_price,
               return_pct, outcome ("target" | "stop" | "expired"), gain_usd.

    Returns:
        A 2-sentence educational debrief string, or "" on failure.
    """
    ticker     = trade.get("ticker", "")
    entry      = trade.get("entry_price", "?")
    exit_price = trade.get("closed_price", "?")
    ret        = trade.get("return_pct", 0)
    outcome    = trade.get("outcome", "")
    gain       = trade.get("gain_usd", 0)

    outcome_desc = {
        "target":  "hit its profit target",
        "stop":    "hit the stop-loss",
        "expired": "expired before hitting target or stop",
    }.get(outcome, "closed")

    sign = "+" if ret >= 0 else ""

    system = (
        "You are a friendly trading coach. Write a 2-sentence post-trade debrief. "
        "Sentence 1: what likely happened (market/technical reason). "
        "Sentence 2: one actionable lesson for next time. "
        "Keep it warm, direct, under 40 words total. No disclaimers."
    )
    user_msg = (
        f"{ticker} {outcome_desc} at ${exit_price} (entry ${entry}). "
        f"Return: {sign}{ret}% (${gain:+.2f}). Outcome: {outcome}."
    )

    try:
        client  = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        return message.content[0].text.strip()
    except Exception as exc:
        print(f"[ai_analyzer] generate_trade_debrief failed (non-critical): {exc}")
        return ""


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
