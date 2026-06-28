"""
position_sizer.py — ATR/conviction-based position sizing with portfolio risk controls.

Methodology
-----------
1. Fixed-Fractional Risk (core):
       shares = (capital × risk_pct) / (entry × stop_pct)
   - risk_pct  = % of capital to risk per trade (default 1%)
   - stop_pct  = from suggested_stop_pct (ATR×1.5 computed in screener), fallback 7%

2. Conviction scaling (Half-Kelly proxy):
       scaled_shares = shares × conviction_factor
   - conviction 5 → 1.00×  (full size)
   - conviction 4 → 0.80×
   - conviction 3 → 0.60×
   - conviction 2 → 0.40×
   - conviction 1 → 0.25×  (probe size)

3. Hard caps applied last:
   - max_position_pct : position dollar value ≤ X% of capital  (default 10%)
   - max_risk_pct     : risk dollars ≤ X% of capital           (default 1.5%)

4. Portfolio-level guardrails (applied across all picks together):
   - max_sector_pct   : combined sector exposure ≤ X%          (default 35%)
   - max_positions    : warn when > N picks simultaneously      (default 8)

5. Correlation guardrails:
   - Fetch 30-day daily closes for all stock picks via yfinance
   - Warn pairs with Pearson r > 0.7 (high co-movement → not truly diversified)

Config keys (in Gist config under "portfolio" dict):
  portfolio_size       — total capital in USD          (default: 10 000)
  risk_per_trade_pct   — % of capital to risk/trade    (default: 1.0)
  max_position_pct     — max single position size %    (default: 10.0)
  max_risk_pct         — hard cap on risk %/trade      (default: 1.5)
  max_sector_pct       — max sector concentration %    (default: 35.0)
  max_positions        — warn threshold for positions  (default: 8)

Output per pick (added as "sizing" key):
  shares            int   — recommended number of shares (0 for crypto/ETF → use dollar_amount)
  dollar_amount     float — total position value in USD
  risk_dollars      float — max loss if stopped out
  risk_pct          float — risk as % of portfolio
  position_pct      float — position as % of portfolio
  stop_price        float — actual stop price derived from stop_pct
  conviction_factor float — the scaling multiplier used
  note              str   — human-readable sizing explanation
  capped_by         str   — "position_cap" | "risk_cap" | None
"""
from __future__ import annotations

# ── Conviction scale ──────────────────────────────────────────────────────────

_CONVICTION_SCALE = {5: 1.00, 4: 0.80, 3: 0.60, 2: 0.40, 1: 0.25}

# Correlation threshold above which we warn
_HIGH_CORR_THRESHOLD = 0.70

# Default stop if neither suggested_stop_pct nor stop_loss is available
_DEFAULT_STOP_PCT = 0.07   # 7%

# Stop floor/ceiling — prevent unrealistic stops
_MIN_STOP_PCT = 0.02   # never use a stop tighter than 2%
_MAX_STOP_PCT = 0.20   # never use a stop wider than 20%


def _as_fraction(v) -> float:
    """
    Normalize a stop/ATR value to a 0-1 fraction. The screener writes
    atr_pct / suggested_stop_pct as PERCENTS (e.g. 7.5 == 7.5%), but the sizing
    math needs a fraction. A value already < 1 is assumed to be a fraction.
    Without this every ATR-fallback stop (e.g. 7.5) was clamped to _MAX_STOP_PCT.
    """
    v = float(v or 0)
    return v / 100.0 if v > 1 else v


# ── Portfolio config helpers ──────────────────────────────────────────────────

def _portfolio_cfg(config: dict) -> dict:
    """Extract portfolio sizing config with safe defaults."""
    pc = config.get("portfolio", {}) if isinstance(config, dict) else {}
    return {
        "portfolio_size":     float(pc.get("portfolio_size",     10_000)),
        "risk_per_trade_pct": float(pc.get("risk_per_trade_pct", 1.0)),
        "max_position_pct":   float(pc.get("max_position_pct",   10.0)),
        "max_risk_pct":       float(pc.get("max_risk_pct",        1.5)),
        "max_sector_pct":     float(pc.get("max_sector_pct",     35.0)),
        "max_positions":      int(  pc.get("max_positions",        8)),
    }


# ── Core per-pick sizer ───────────────────────────────────────────────────────

def size_pick(pick: dict, config: dict, is_crypto: bool = False) -> dict:
    """
    Compute position sizing for a single pick.

    pick fields used:
      entry_price / current_price
      stop_loss (absolute price)            — preferred
      suggested_stop_pct (percent or fraction; normalized) — fallback 1
      atr_pct (percent or fraction; normalized)            — fallback 2
      conviction (1-5 int or float)
      ticker / symbol
      sector                                — for portfolio checks
    """
    pc     = _portfolio_cfg(config)
    cap    = pc["portfolio_size"]
    risk_f = pc["risk_per_trade_pct"] / 100.0
    max_p  = pc["max_position_pct"]   / 100.0
    max_r  = pc["max_risk_pct"]       / 100.0

    entry = pick.get("entry_price") or pick.get("current_price") or 0
    if not entry or entry <= 0:
        return _null_sizing("no entry price")

    # ── Determine stop % ─────────────────────────────────────────────────────
    stop_abs = pick.get("stop_loss")
    if stop_abs and stop_abs > 0 and stop_abs < entry:
        stop_pct = (entry - stop_abs) / entry
    else:
        # Fallback to ATR-based stop from screener. These arrive as PERCENTS
        # (screener writes atr_pct = atr/price*100), so normalize to a fraction.
        stop_pct = _as_fraction(
            pick.get("suggested_stop_pct") or
            (pick.get("atr_pct", 0) * 1.5) or
            _DEFAULT_STOP_PCT
        )
        stop_abs = entry * (1 - stop_pct)

    stop_pct = max(_MIN_STOP_PCT, min(_MAX_STOP_PCT, float(stop_pct)))
    stop_abs = round(entry * (1 - stop_pct), 4)

    # ── Base size from fixed-fractional risk ──────────────────────────────────
    risk_dollars_base = cap * risk_f
    # shares = risk_budget / dollar_risk_per_share
    shares_base = risk_dollars_base / (entry * stop_pct)

    # ── Conviction scaling ────────────────────────────────────────────────────
    conviction      = max(1, min(5, int(round(pick.get("conviction", 3) or 3))))
    conv_factor     = _CONVICTION_SCALE.get(conviction, 0.60)
    shares_scaled   = shares_base * conv_factor
    dollar_scaled   = shares_scaled * entry
    risk_scaled     = shares_scaled * entry * stop_pct

    # ── Apply caps ────────────────────────────────────────────────────────────
    capped_by   = None
    max_dollars = cap * max_p
    max_risk_d  = cap * max_r

    if dollar_scaled > max_dollars:
        shares_scaled = max_dollars / entry
        capped_by     = "position_cap"
    if shares_scaled * entry * stop_pct > max_risk_d:
        shares_scaled = max_risk_d / (entry * stop_pct)
        capped_by     = "risk_cap"

    shares_final  = max(1, round(shares_scaled)) if not is_crypto else None
    dollar_final  = round((shares_final or 1) * entry if shares_final else shares_scaled * entry, 2)
    risk_final    = round(dollar_final * stop_pct, 2)
    position_pct  = round(dollar_final / cap * 100, 2)
    risk_pct_final= round(risk_final   / cap * 100, 2)

    # ── Human-readable note ───────────────────────────────────────────────────
    cap_note = f" (capped: {capped_by.replace('_', ' ')})" if capped_by else ""
    if is_crypto:
        note = (
            f"${dollar_final:,.0f} position · risk ${risk_final:,.0f} "
            f"({risk_pct_final}% of portfolio) · stop ${stop_abs:.4g}{cap_note}"
        )
    else:
        note = (
            f"{shares_final} shares · ${dollar_final:,.0f} "
            f"({position_pct}% of portfolio) · risk ${risk_final:,.0f} "
            f"({risk_pct_final}%) · stop ${stop_abs:.2f}{cap_note}"
        )

    return {
        "shares":           shares_final,
        "dollar_amount":    dollar_final,
        "risk_dollars":     risk_final,
        "risk_pct":         risk_pct_final,
        "position_pct":     position_pct,
        "stop_price":       round(stop_abs, 2),
        "stop_pct":         round(stop_pct * 100, 2),
        "conviction_factor": conv_factor,
        "capped_by":        capped_by,
        "note":             note,
    }


def _null_sizing(reason: str) -> dict:
    return {
        "shares": None, "dollar_amount": None, "risk_dollars": None,
        "risk_pct": None, "position_pct": None, "stop_price": None,
        "stop_pct": None, "conviction_factor": None, "capped_by": None,
        "note": reason,
    }


# ── Portfolio-level guardrails ────────────────────────────────────────────────

def apply_portfolio_sizing(picks: dict, config: dict) -> tuple[dict, list[str]]:
    """
    Attach sizing to every pick in the picks dict and run portfolio-level checks.

    Returns (enriched_picks, warnings) where warnings is a list of strings
    suitable for the admin alert or morning message footer.
    """
    pc       = _portfolio_cfg(config)
    cap      = pc["portfolio_size"]
    warnings = []

    # Collect all picks across categories for portfolio checks
    all_picks_flat = []   # (category, pick_dict, is_crypto)
    for cat in ("short_term", "long_term"):
        for p in picks.get(cat, []):
            all_picks_flat.append((cat, p, False))
    for p in picks.get("crypto", []):
        all_picks_flat.append(("crypto", p, True))
    for p in picks.get("etf", []):
        all_picks_flat.append(("etf", p, False))
    for p in picks.get("commodities", []):
        all_picks_flat.append(("commodities", p, False))

    # ── Size each pick ────────────────────────────────────────────────────────
    for cat, pick, is_crypto in all_picks_flat:
        sizing = size_pick(pick, config, is_crypto=is_crypto)
        pick["sizing"] = sizing

    # ── Portfolio-level checks ────────────────────────────────────────────────

    # Total position count
    n_positions = len(all_picks_flat)
    if n_positions > pc["max_positions"]:
        warnings.append(
            f"⚠️ {n_positions} picks today exceeds max_positions={pc['max_positions']} "
            f"— consider sizing down or skipping lower-conviction picks."
        )

    # Sector concentration
    sector_exposure: dict[str, float] = {}
    for _, pick, _ in all_picks_flat:
        sector = pick.get("sector") or pick.get("asset_type") or "Unknown"
        dollar = (pick.get("sizing") or {}).get("dollar_amount") or 0
        sector_exposure[sector] = sector_exposure.get(sector, 0) + dollar

    total_deployed = sum(sector_exposure.values())
    max_sector_pct = pc["max_sector_pct"]
    for sector, exposure in sorted(sector_exposure.items(), key=lambda x: -x[1]):
        if total_deployed > 0:
            pct = round(exposure / cap * 100, 1)
            if pct > max_sector_pct:
                warnings.append(
                    f"⚠️ Sector concentration: {sector} = {pct}% of portfolio "
                    f"(limit {max_sector_pct}%) — reduce exposure."
                )

    # Total capital deployed
    if total_deployed > 0:
        deployed_pct = round(total_deployed / cap * 100, 1)
        if deployed_pct > 80:
            warnings.append(
                f"📊 Today's picks deploy {deployed_pct}% of portfolio "
                f"(${total_deployed:,.0f} of ${cap:,.0f})."
            )

    # Total risk across all picks
    total_risk = sum(
        (p.get("sizing") or {}).get("risk_dollars") or 0
        for _, p, _ in all_picks_flat
    )
    if total_risk > 0:
        total_risk_pct = round(total_risk / cap * 100, 1)
        if total_risk_pct > 5:
            warnings.append(
                f"⚠️ Total portfolio risk today: {total_risk_pct}% "
                f"(${total_risk:,.0f}) — elevated. Consider skipping weakest picks."
            )

    # Correlation check across stock picks
    corr_warnings = _correlation_warnings(picks)
    warnings.extend(corr_warnings)

    return picks, warnings


# ── Correlation matrix ────────────────────────────────────────────────────────

def _correlation_warnings(picks: dict) -> list[str]:
    """
    Fetch 30-day daily closes for all stock picks and warn on highly correlated pairs.
    Only stock tickers (short_term + long_term) are included — crypto is excluded
    since yfinance coverage for coins is inconsistent.

    Returns a list of warning strings (empty if no high correlations found).
    """
    # Collect stock tickers only
    stock_tickers: list[str] = []
    for cat in ("short_term", "long_term"):
        for p in picks.get(cat, []):
            sym = p.get("ticker", "").upper()
            if sym:
                stock_tickers.append(sym)

    if len(stock_tickers) < 2:
        return []

    try:
        import yfinance as yf

        # Download 30-day close history for all tickers in one batch call
        hist = yf.download(
            stock_tickers,
            period="30d",
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=True,
            timeout=15,
        )

        # Handle both single-ticker (Series) and multi-ticker (DataFrame) results
        if hasattr(hist, "columns") and isinstance(hist.columns, object):
            if len(stock_tickers) == 1:
                closes = hist[["Close"]].rename(columns={"Close": stock_tickers[0]})
            else:
                try:
                    closes = hist["Close"]
                except KeyError:
                    closes = hist.xs("Close", axis=1, level=0) if hasattr(hist.columns, "levels") else hist
        else:
            return []

        # Drop tickers with too few data points (< 15 trading days)
        closes = closes.dropna(axis=1, thresh=15)
        if closes.shape[1] < 2:
            return []

        # Daily log returns
        log_rets = closes.pct_change().dropna()
        if len(log_rets) < 10:
            return []

        # Pearson correlation matrix
        corr_matrix = log_rets.corr()

        warnings: list[str] = []
        tickers = list(corr_matrix.columns)
        for i in range(len(tickers)):
            for j in range(i + 1, len(tickers)):
                r = corr_matrix.iloc[i, j]
                if r >= _HIGH_CORR_THRESHOLD:
                    warnings.append(
                        f"📐 High correlation: {tickers[i]} ↔ {tickers[j]} "
                        f"(r={r:.2f}) — consider skipping one to reduce portfolio overlap."
                    )

        return warnings

    except Exception as exc:
        print(f"[position_sizer] Correlation check failed (non-critical): {exc}")
        return []


# ── Convenience summary ───────────────────────────────────────────────────────

def portfolio_summary(picks: dict, config: dict) -> str:
    """Return a compact one-line portfolio summary string for the morning message."""
    pc  = _portfolio_cfg(config)
    cap = pc["portfolio_size"]

    total_deployed = 0
    total_risk     = 0
    n_picks        = 0

    for cat in ("short_term", "long_term", "crypto", "etf", "commodities"):
        for p in picks.get(cat, []):
            sz = p.get("sizing") or {}
            total_deployed += sz.get("dollar_amount") or 0
            total_risk     += sz.get("risk_dollars")  or 0
            n_picks        += 1

    if not total_deployed:
        return ""

    deployed_pct = round(total_deployed / cap * 100, 1)
    risk_pct     = round(total_risk     / cap * 100, 1)
    return (
        f"📊 <b>Portfolio sizing</b> · {n_picks} picks · "
        f"${total_deployed:,.0f} deployed ({deployed_pct}%) · "
        f"risk ${total_risk:,.0f} ({risk_pct}%) · "
        f"capital ${cap:,.0f}"
    )


if __name__ == "__main__":
    import pprint

    _cfg = {
        "portfolio": {
            "portfolio_size":     25_000,
            "risk_per_trade_pct": 1.0,
            "max_position_pct":   10.0,
            "max_risk_pct":       1.5,
            "max_sector_pct":     35.0,
            "max_positions":      8,
        }
    }

    sample_picks = {
        "short_term": [
            {"ticker": "NVDA", "entry_price": 875, "stop_loss": 812,
             "target_price": 960, "conviction": 5, "sector": "Technology"},
            {"ticker": "AAPL", "entry_price": 185, "suggested_stop_pct": 0.06,
             "target_price": 205, "conviction": 3, "sector": "Technology"},
        ],
        "long_term": [
            {"ticker": "MSFT", "entry_price": 415, "atr_pct": 0.018,
             "target_price": 480, "conviction": 4, "sector": "Technology"},
        ],
        "crypto": [
            {"symbol": "BTC", "entry_price": 65000, "stop_loss": 59000,
             "target_price": 75000, "conviction": 4},
        ],
    }

    enriched, warns = apply_portfolio_sizing(sample_picks, _cfg)
    print("=== Sizing per pick ===")
    for cat in ("short_term", "long_term", "crypto"):
        for p in enriched.get(cat, []):
            sym = p.get("ticker") or p.get("symbol")
            pprint.pprint({sym: p["sizing"]})
    print("\n=== Portfolio summary ===")
    print(portfolio_summary(enriched, _cfg))
    print("\n=== Warnings ===")
    for w in warns:
        print(w)
