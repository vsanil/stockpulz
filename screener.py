"""
screener.py — Broad US stock screener using yfinance + ta + Finnhub fundamentals.

Covers up to 600 tickers across S&P 500 + NASDAQ 100 + S&P MidCap 400 via a
single yf.download() bulk call — deduped and capped at MAX_TICKERS.
Short-term and long-term candidates are selected independently:
  ST pool → top 30 by technical score   (momentum/breakout plays)
  LT pool → top 30 by dollar volume     (large caps + quality mid-caps)

Fundamental scoring uses Finnhub basic metrics (/stock/metric) when the
API key is available — more reliable than yfinance.info which has ~30% null
rates on P/E, revenue growth, and free cash flow. Falls back to yfinance
gracefully if the key is absent or a call fails.
"""
from __future__ import annotations

import os
import time
import warnings
from io import StringIO
import requests
import pandas as pd
import yfinance as yf
import ta

from earnings_checker import get_upcoming_earnings
from market_regime import get_market_regime, regime_pick_multiplier
from congressional_tracker import get_all_congressional_trades, get_congressional_signal

FINNHUB_BASE = "https://finnhub.io/api/v1"

warnings.filterwarnings("ignore")

# Sector median P/E ratios (approximate)
SECTOR_MEDIAN_PE = {
    "Technology": 28,
    "Health Care": 22,
    "Financials": 14,
    "Consumer Discretionary": 24,
    "Communication Services": 20,
    "Industrials": 20,
    "Consumer Staples": 22,
    "Energy": 12,
    "Utilities": 18,
    "Real Estate": 35,
    "Materials": 17,
    "Unknown": 20,
}

MAX_TICKERS    = 600   # S&P 500 + NASDAQ 100 + MidCap 400, deduped, capped here
ST_CANDIDATE_N = 30    # Top N by technical score  → short-term pool
LT_CANDIDATE_N = 50    # Top N (by dollar volume, after liquidity gate) → long-term pool
                        # Larger pool gives fundamental scoring more quality options to pick from
MIN_LT_DOLLAR_VOL = 50_000_000  # $50M/day avg — filters out small/micro-caps from LT pool
SLEEP_INFO     = 0.1   # Delay between individual .info calls


# ── Broad US stock universe ───────────────────────────────────────────────────
# Fallback list: S&P 500 core + NASDAQ 100 extras + S&P MidCap 400 highlights
# Used when live sources are unreachable. Covers large + quality mid-caps.

# Minimal last-resort fallback — only ultra-stable mega-caps unlikely to ever
# be acquired or delisted. The real universe comes from live sources or the
# Redis/memory cache (see get_stock_universe). Keep this list short and boring.
FALLBACK_TICKERS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "BRK-B", "AVGO",
    "LLY", "JPM", "V", "UNH", "XOM", "MA", "PG", "JNJ", "COST", "HD",
    "MRK", "ABBV", "CVX", "CRM", "BAC", "NFLX", "AMD", "KO", "WMT", "PEP",
    "TMO", "ACN", "MCD", "CSCO", "ABT", "ADBE", "TXN", "DIS", "WFC", "NEE",
    "PM", "RTX", "UPS", "INTU", "AMGN", "MS", "SPGI", "GS", "LOW", "HON",
    "ISRG", "CAT", "ELV", "BKNG", "PLD", "NOW", "MDLZ", "TGT", "ZTS", "CB",
    "SHW", "CI", "MO", "DUK", "SO", "BMY", "GILD", "EOG", "SLB", "BDX",
    "ITW", "NOC", "APD", "AON", "CME", "ICE", "ECL", "REGN", "HUM", "F",
    "GM", "UBER", "MMC", "PNC", "USB", "AXP", "BLK", "SCHW", "TJX", "DE",
    "ETN", "EMR", "ADI", "KLAC", "LRCX", "MCHP", "AMAT", "SNPS", "CDNS", "FTNT",
    "ADP", "PAYX", "CTAS", "ROP", "IDXX", "FAST", "ODFL", "CPRT", "VRSK",
    "IQV", "MTD", "A", "ILMN", "WAT", "BIIB", "VRTX", "MRNA", "DXCM",
    "CVS", "CNC", "MCK", "CAH", "HCA", "BAX", "BSX", "EW", "PODD", "PHM",
    "DHI", "LEN", "NVR", "TOL", "AMT", "CCI", "SBAC", "EQIX", "DLR", "PSA",
    "EXR", "AVB", "EQR", "ESS", "VTR", "WELL", "CFG", "KEY", "FITB", "MTB",
    "HBAN", "RF", "AEP", "EXC", "SRE", "PEG", "FE", "PPL", "CMS", "WEC",
    "COP", "DVN", "OXY", "VLO", "MPC", "PSX", "LIN", "PPG", "RPM", "VMC",
    "MLM", "AGCO", "UAL", "DAL", "AAL", "LUV", "ALK", "UNP", "CSX", "NSC",
    "CP", "CNI", "FDX", "CHRW", "EXPD", "JBHT", "SAIA", "AZO", "ORLY", "GPC",
    "TSCO", "FIVE", "LMT", "GD", "BA", "HII", "TDG", "LDOS", "SAIC", "KTOS",
    "PFE", "NVO", "AZN", "SNY", "GSK", "NVS", "C", "COF", "SYF", "ALLY",
    "PYPL", "SQ", "AFRM", "FIS", "FI", "WU", "OMF",
    # ── NASDAQ 100 ────────────────────────────────────────────────────────────
    "QCOM", "INTC", "MU", "SMCI", "ON", "MRVL", "ARM", "CRWD", "PANW", "ZS",
    "OKTA", "NET", "DDOG", "MDB", "SNOW", "PLTR", "RBLX", "U", "HOOD", "COIN",
    "SHOP", "MELI", "SE", "GRAB", "BABA", "JD", "PDD", "BIDU", "TCEHY", "NTES",
    "ASML", "TSM", "SONY", "SAP", "ORCL", "IBM", "HPQ", "HPE", "DELL", "LOGI",
    "STX", "WDC", "NTAP", "PSTG", "ANET", "FFIV", "ZBRA", "TEAM", "HUBS", "ZM",
    "DOCU", "WIX", "BILL", "APPF", "PCTY", "ABNB", "LYFT", "DASH", "OPEN", "CVNA",
    "KMX", "AN", "PAG", "NDAQ", "CBOE", "MKTX", "VRTS", "LPLA", "RJF", "IBKR",
    "BMRN", "ALNY", "IONS", "INCY", "NBIX", "EXAS", "NTRA", "PACB", "TWST",
    "CRSP", "NTLA", "BEAM", "EDIT", "ARVN", "PRAX", "DNLI",
    # ── MidCap highlights (verified active as of 2025) ────────────────────────
    "DECK", "LULU", "PTON", "NKE", "UA", "CROX", "HBI", "PVH", "RL", "TPR",
    "CPRI", "SIG", "CAKE", "TXRH", "BJRI", "JACK", "WEN", "QSR", "YUM", "CMG",
    "SBUX", "DPZ", "PZZA", "FAT", "PLAY", "EAT", "DRI", "CBRL", "DENN", "FWRG",
    "KRUS", "FRSH", "XPOF", "PLNT", "BOOT", "SHOO", "CATO", "AEO", "ANF", "URBN",
    "GAP", "BURL", "ROST", "DLTR", "DG", "BJ", "PRGO", "SFM", "CASY", "MUSA",
    "RH", "WSM", "LOVE", "SNBR", "BLMN", "ELF", "ULTA", "COTY", "SPB", "CHD",
    "CLX", "ENR", "MLI", "GFF", "APOG", "TREX", "AAON", "IBP", "BLDR", "BECN",
    "GMS", "STLD", "NUE", "RS", "CMC", "ZEUS", "KALU", "CENX", "CRS", "ATI",
    "HWM", "TKR", "GTLS", "FLOW", "GHM", "CECO", "FELE", "POWI", "FORM", "MKSI",
    "ONTO", "ICHR", "ACLS", "ENTG", "UCTT", "KLIC", "COHU", "EPC", "GEF", "SLGN",
    "BERY", "SEE", "SON", "IP", "PKG", "CRUS", "SLAB", "DIOD", "LSCC", "ALGM",
    "MTSI", "LITE", "COHR", "SMTC", "SWKS", "QRVO", "CIEN", "VIAV", "CALX",
    "ADTN", "DCOM", "SHEN",
]

# Cache key + TTL for the live-fetched universe
_UNIVERSE_CACHE_KEY = "ticker_universe"
_UNIVERSE_CACHE_TTL = 7 * 24 * 3600  # 7 days — tickers change slowly


# Wikipedia 403s any request without a browser User-Agent — set one explicitly.
_BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def _wiki_symbols(url: str) -> list[str]:
    """
    Fetch a Wikipedia constituents page (with a browser UA so it doesn't 403) and
    return the Symbol/Ticker column. Scans ALL tables for the one with a
    symbol/ticker column and >50 rows, so it's robust to table-index changes
    (the old code hardcoded [0]/[4], which silently broke when the page layout
    shifted). pd.read_html needs StringIO in pandas 2.x.
    """
    try:
        resp = requests.get(url, headers=_BROWSER_UA, timeout=15)
        resp.raise_for_status()
        for df in pd.read_html(StringIO(resp.text)):
            col = next((c for c in df.columns
                        if "symbol" in str(c).lower() or "ticker" in str(c).lower()), None)
            if col is not None and len(df) > 50:   # the constituents table, not a sidebar
                return [str(t) for t in df[col].tolist()]
    except Exception as exc:
        print(f"[screener] Wikipedia fetch failed ({url.rsplit('/', 1)[-1]}): {exc}")
    return []


def get_stock_universe() -> list[str]:
    """
    Build a broad US stock universe: S&P 500 + NASDAQ 100 + S&P MidCap 400.

    Fetch priority:
      1. Live sources (datahub.io, Wikipedia) — always fresh, current members only
      2. Cache (Redis if configured, else in-process memory) — last good fetch,
         TTL 7 days. Survives transient network failures without touching the
         hardcoded list. Redis persists across GitHub Actions runs / restarts.
      3. FALLBACK_TICKERS — minimal mega-cap list, last resort only.
    """
    from cache_layer import get_cache
    cache = get_cache()

    seen:    set[str]  = set()
    tickers: list[str] = []

    def _add(new_tickers: list[str]) -> None:
        for t in new_tickers:
            t = t.strip().replace(".", "-")
            if t and t not in seen:
                seen.add(t)
                tickers.append(t)

    # ── Source 1: S&P 500 via the datasets GitHub CSV mirror ─────────────────
    # (the old datahub.io URL 404s now). Stable raw CSV, current constituents.
    try:
        resp = requests.get(
            "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv",
            headers=_BROWSER_UA, timeout=15,
        )
        resp.raise_for_status()
        df = pd.read_csv(StringIO(resp.text))
        sp500 = [str(t) for t in df["Symbol"].tolist()]
        if len(sp500) > 100:
            _add(sp500)
            print(f"[screener] S&P 500: {len(sp500)} tickers from GitHub CSV.")
    except Exception as exc:
        print(f"[screener] S&P 500 GitHub CSV failed ({exc}).")

    # ── Source 2: S&P 500 via Wikipedia (fallback) ────────────────────────────
    if len(tickers) < 100:
        before = len(tickers)
        _add(_wiki_symbols("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"))
        print(f"[screener] S&P 500 Wikipedia: added {len(tickers)-before} tickers.")

    # ── Source 3: NASDAQ 100 via Wikipedia ────────────────────────────────────
    before = len(tickers)
    _add(_wiki_symbols("https://en.wikipedia.org/wiki/Nasdaq-100"))
    print(f"[screener] NASDAQ 100: added {len(tickers)-before} new tickers.")

    # ── Source 4: S&P MidCap 400 via Wikipedia ───────────────────────────────
    before = len(tickers)
    _add(_wiki_symbols("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"))
    print(f"[screener] MidCap 400: added {len(tickers)-before} new tickers.")

    # ── Live fetch succeeded — persist to cache and return ────────────────────
    if len(tickers) > 100:
        result = tickers[:MAX_TICKERS]
        print(f"[screener] Universe: {len(result)} tickers (capped at {MAX_TICKERS}). Caching for 7d.")
        cache.set(_UNIVERSE_CACHE_KEY, result, ttl_seconds=_UNIVERSE_CACHE_TTL)
        return result

    # ── Live sources failed — try cache ───────────────────────────────────────
    cached = cache.get(_UNIVERSE_CACHE_KEY)
    if cached and len(cached) > 100:
        print(f"[screener] Live sources failed. Using cached universe ({len(cached)} tickers, backend={cache.name()}).")
        return cached[:MAX_TICKERS]

    # ── Cache cold — use minimal hardcoded mega-cap list ─────────────────────
    print(f"[screener] No cache available. Using hardcoded fallback ({len(FALLBACK_TICKERS)} tickers).")
    return FALLBACK_TICKERS[:MAX_TICKERS]


# Keep old name as alias so nothing else breaks
def get_sp500_tickers() -> list[str]:
    return get_stock_universe()


# ── Technical indicators (short-term scoring) ─────────────────────────────────

def _short_term_score(hist: pd.DataFrame) -> tuple[int, dict]:
    """Score a ticker for short-term trading (out of 100). Returns (score, metrics)."""
    score   = 0
    metrics = {}

    try:
        close  = hist["Close"].squeeze()
        volume = hist["Volume"].squeeze()

        # RSI (14-day) — sweet spot 35-55: momentum building, not overbought
        rsi_val = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
        rsi     = round(float(rsi_val), 2) if not pd.isna(rsi_val) else None
        metrics["rsi"] = rsi
        if rsi and 35 <= rsi <= 55:
            score += 25

        # MACD crossover in last 3 days
        macd_ind  = ta.trend.MACD(close, window_fast=12, window_slow=26, window_sign=9)
        macd_line = macd_ind.macd()
        macd_sig  = macd_ind.macd_signal()
        crossed   = False
        for i in range(-3, 0):
            if (not pd.isna(macd_line.iloc[i]) and not pd.isna(macd_sig.iloc[i]) and
                    macd_line.iloc[i] > macd_sig.iloc[i] and
                    macd_line.iloc[i - 1] <= macd_sig.iloc[i - 1]):
                crossed = True
                break
        metrics["macd_crossover"] = crossed
        if crossed:
            score += 25

        # Volume surge (today vs 20-day avg)
        vol_ratio = float(volume.iloc[-1] / volume.iloc[-21:-1].mean()) if len(volume) >= 21 else None
        metrics["volume_ratio"] = round(vol_ratio, 2) if vol_ratio else None
        if vol_ratio and vol_ratio > 1.5:
            score += 20

        # Price above 20-day EMA (uptrend confirmation)
        ema20_val     = ta.trend.EMAIndicator(close, window=20).ema_indicator().iloc[-1]
        current_price = float(close.iloc[-1])
        week_high     = float(close.rolling(252).max().iloc[-1])
        if not pd.isna(ema20_val):
            ema_val          = float(ema20_val)
            metrics["ema20"] = round(ema_val, 2)
            if current_price >= ema_val:
                score += 15

        # Breakout today — price closed above 20-day rolling high (momentum breakout).
        # This is the William O'Neil breakout definition: new multi-week high with volume.
        # Separately track near-52w-high as a bonus (within 3% = potential 52w breakout).
        try:
            high_20d = float(close.rolling(20).max().shift(1).iloc[-1])   # prior 20d high
            metrics["breakout_today"] = (not pd.isna(high_20d) and current_price > high_20d)
            if metrics["breakout_today"] and vol_ratio and vol_ratio > 1.3:
                score += 15   # breakout + volume confirmation

            # Near 52-week high (within 3%) — approaching breakout zone
            if not pd.isna(week_high) and week_high > 0:
                pct_from_high = (week_high - current_price) / week_high * 100
                metrics["pct_from_52w_high"] = round(pct_from_high, 1)
                if 0 <= pct_from_high <= 3:
                    score += 10   # within 3% of 52w high — breakout setup
        except Exception:
            pass

        # Near Bollinger lower band (potential bounce).
        # Two guards prevent false signals:
        #   1. Volume > 1.2× avg — eliminates dead-cat bounces on thin volume.
        #   2. Green candle (close >= open) — confirms buying pressure at support,
        #      not heavy selling that happens to be near the lower band.
        bb         = ta.volatility.BollingerBands(close, window=20, window_dev=2)
        lower_band = float(bb.bollinger_lband().iloc[-1])
        metrics["bb_lower"] = round(lower_band, 2)
        try:
            last_open = float(hist["Open"].squeeze().iloc[-1])
        except Exception:
            last_open = None
        if (not pd.isna(lower_band)
                and current_price <= lower_band * 1.05
                and vol_ratio and vol_ratio > 1.2
                and (last_open is None or current_price >= last_open)):
            score += 15

        # OBV slope — positive = smart money accumulating (+10 pts)
        # Positive OBV slope means up-day volume dominates down-day volume.
        try:
            obv       = ta.volume.OnBalanceVolumeIndicator(close, volume).on_balance_volume()
            obv_clean = obv.dropna()
            if len(obv_clean) >= 10:
                obv_slope = float(obv_clean.tail(10).diff().mean())
                metrics["obv_positive"] = obv_slope > 0
                if obv_slope > 0:
                    score += 10
        except Exception:
            pass

        # ATR (14-day) — average true range as % of price.
        # Used to suggest a professional stop-loss distance (1.5× ATR is standard).
        # No scoring impact — purely informational for Claude's risk guidance.
        try:
            high  = hist["High"].squeeze()
            low   = hist["Low"].squeeze()
            atr_s = ta.volatility.AverageTrueRange(high, low, close, window=14).average_true_range()
            atr_v = float(atr_s.iloc[-1])
            if not pd.isna(atr_v) and current_price > 0:
                atr_pct = round(atr_v / current_price * 100, 2)
                metrics["atr_pct"]            = atr_pct
                metrics["suggested_stop_pct"] = round(atr_pct * 1.5, 1)
        except Exception:
            pass

        # Candlestick + chart patterns — expert-level visual signals.
        # Bull flag and bullish engulfing are the highest-conviction ST patterns.
        patterns = _detect_patterns(hist)
        if patterns:
            metrics["patterns"] = list(patterns.keys())
            if patterns.get("bull_flag"):
                score += 15
            if patterns.get("bullish_engulfing"):
                score += 15
            if patterns.get("three_white_soldiers"):
                score += 10
            if patterns.get("hammer"):
                score += 10
            if patterns.get("morning_star"):
                score += 10

    except Exception as exc:
        print(f"[screener] Short-term indicator error: {exc}")

    return score, metrics


# ── Candlestick pattern detection ────────────────────────────────────────────

def _detect_patterns(hist: pd.DataFrame) -> dict:
    """
    Detect key bullish candlestick and chart patterns from OHLCV data.
    Uses only data already in the bulk download — zero extra API calls.

    Patterns detected:
      bullish_engulfing  — today's green candle body engulfs yesterday's red body
      hammer             — small body, long lower wick (potential reversal)
      three_white_soldiers — 3 consecutive higher-closing green candles
      bull_flag          — strong 5-day move followed by tight 3-day consolidation
      morning_star       — 3-bar bullish reversal (red → small → green)

    Returns dict of pattern name → True (only present keys are active patterns).
    """
    patterns: dict[str, bool] = {}
    try:
        if len(hist) < 10:
            return patterns

        o = hist["Open"].squeeze().astype(float)
        h = hist["High"].squeeze().astype(float)
        l = hist["Low"].squeeze().astype(float)
        c = hist["Close"].squeeze().astype(float)

        # Current and previous candle values
        c0, o0, h0, l0 = c.iloc[-1], o.iloc[-1], h.iloc[-1], l.iloc[-1]
        c1, o1         = c.iloc[-2], o.iloc[-2]
        c2, o2         = c.iloc[-3], o.iloc[-3]

        body0      = abs(c0 - o0)
        body1      = abs(c1 - o1)
        range0     = h0 - l0
        lower_wick = min(o0, c0) - l0
        upper_wick = h0 - max(o0, c0)

        # Bullish Engulfing: yesterday red, today green, today body engulfs yesterday's
        if c1 < o1 and c0 > o0 and o0 <= c1 and c0 >= o1 and body0 > body1 * 0.8:
            patterns["bullish_engulfing"] = True

        # Hammer: small body in upper portion, lower wick ≥ 2× body, tiny upper wick
        if (range0 > 0 and body0 > 0
                and lower_wick >= 2.0 * body0
                and upper_wick <= body0 * 0.5):
            patterns["hammer"] = True

        # Three White Soldiers: 3 consecutive green candles, each closing higher
        if (c0 > o0 and c1 > o1 and c2 > o2
                and c0 > c1 > c2
                and o0 > o1):
            patterns["three_white_soldiers"] = True

        # Bull Flag: ≥5% move over 5 days → tight ≤3% range over last 3 days
        if len(c) >= 9:
            base   = float(c.iloc[-9])
            peak   = float(c.iloc[-4])
            flag_h = float(h.iloc[-4:-1].max())
            flag_l = float(l.iloc[-4:-1].min())
            if base > 0 and (peak - base) / base * 100 >= 5:
                flag_range = (flag_h - flag_l) / peak * 100 if peak > 0 else 999
                if flag_range <= 3.5:
                    patterns["bull_flag"] = True

        # Morning Star: big red → small body (doji-like) → big green
        if (c2 < o2 and body1 < body0 * 0.4 and body1 < abs(c2 - o2) * 0.4
                and c0 > o0 and c0 > (o2 + c2) / 2):
            patterns["morning_star"] = True

    except Exception as exc:
        print(f"[screener] Pattern detection error: {exc}")

    return patterns


# ── Alpaca real-time price snapshot ──────────────────────────────────────────

def _get_alpaca_snapshots(tickers: list[str]) -> dict[str, float]:
    """
    Fetch real-time latest prices for a list of tickers via Alpaca free tier.
    Requires ALPACA_KEY_ID + ALPACA_SECRET_KEY env vars (free paper account).
    Returns {ticker: price} — only for tickers successfully fetched.
    Falls back silently to empty dict if keys absent or call fails.
    """
    key    = os.environ.get("ALPACA_KEY_ID", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        return {}

    prices: dict[str, float] = {}
    # Alpaca allows up to 1000 symbols per snapshot call
    chunk_size = 500
    headers = {
        "APCA-API-KEY-ID":     key,
        "APCA-API-SECRET-KEY": secret,
        "Accept":              "application/json",
    }
    url = "https://data.alpaca.markets/v2/stocks/snapshots"

    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i + chunk_size]
        try:
            resp = requests.get(
                url,
                headers=headers,
                params={"symbols": ",".join(chunk), "feed": "iex"},
                timeout=10,
            )
            if resp.status_code != 200:
                print(f"[screener] Alpaca snapshot error: {resp.status_code}")
                continue
            data = resp.json()
            for ticker, snap in data.items():
                try:
                    price = (snap.get("latestTrade") or {}).get("p") or \
                            (snap.get("latestQuote") or {}).get("ap") or \
                            (snap.get("dailyBar") or {}).get("c")
                    if price:
                        prices[ticker.upper()] = float(price)
                except Exception:
                    pass
        except Exception as exc:
            print(f"[screener] Alpaca snapshot chunk failed: {exc}")

    if prices:
        print(f"[screener] Alpaca real-time prices: {len(prices)} tickers fetched.")
    return prices


def _alpaca_single_bars(ticker: str, days: int = 365) -> "pd.DataFrame | None":
    """Fetch daily bars for one stock ticker via Alpaca. Returns DataFrame or None."""
    import datetime as _dt
    key    = os.environ.get("ALPACA_KEY_ID", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        return None
    start_date = (_dt.date.today() - _dt.timedelta(days=days + 10)).isoformat()
    headers = {
        "APCA-API-KEY-ID":     key,
        "APCA-API-SECRET-KEY": secret,
        "Accept":              "application/json",
    }
    try:
        resp = requests.get(
            f"https://data.alpaca.markets/v2/stocks/bars/{ticker}",
            headers=headers,
            params={"timeframe": "1Day", "start": start_date, "limit": 1000, "feed": "iex"},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        bars = resp.json().get("bars") or []
        if not bars:
            return None
        df = pd.DataFrame(bars)
        df["t"] = pd.to_datetime(df["t"], utc=True).dt.tz_localize(None)
        df = df.set_index("t").rename(columns={
            "o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume",
        })[["Open", "High", "Low", "Close", "Volume"]]
        df.index.name = "Date"
        return df
    except Exception:
        return None


def _alpaca_bulk_bars(tickers: list, days: int = 92) -> dict:
    """
    Fetch daily bars for many tickers via Alpaca multi-symbol endpoint.
    Returns {ticker: DataFrame} with OHLCV columns — empty dict on failure.
    Chunks 100 tickers per request; handles pagination via next_page_token.
    """
    import datetime as _dt
    key    = os.environ.get("ALPACA_KEY_ID", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        return {}
    start_date = (_dt.date.today() - _dt.timedelta(days=days + 10)).isoformat()
    headers = {
        "APCA-API-KEY-ID":     key,
        "APCA-API-SECRET-KEY": secret,
        "Accept":              "application/json",
    }
    url       = "https://data.alpaca.markets/v2/stocks/bars"
    raw_bars: dict = {}
    chunk_size = 100

    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i + chunk_size]
        params: dict = {
            "symbols":   ",".join(chunk),
            "timeframe": "1Day",
            "start":     start_date,
            "limit":     10000,
            "feed":      "iex",
        }
        page_token = None
        while True:
            if page_token:
                params["page_token"] = page_token
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=30)
                if resp.status_code != 200:
                    print(f"[screener] Alpaca bars chunk {i // chunk_size} error: {resp.status_code}")
                    break
                data = resp.json()
                for sym, bars in (data.get("bars") or {}).items():
                    raw_bars.setdefault(sym.upper(), []).extend(bars)
                page_token = data.get("next_page_token")
                if not page_token:
                    break
            except Exception as exc:
                print(f"[screener] Alpaca bars chunk {i // chunk_size} failed: {exc}")
                break

    result: dict = {}
    for ticker, bars in raw_bars.items():
        if not bars:
            continue
        df = pd.DataFrame(bars)
        df["t"] = pd.to_datetime(df["t"], utc=True).dt.tz_localize(None)
        df = df.set_index("t").rename(columns={
            "o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume",
        })[["Open", "High", "Low", "Close", "Volume"]]
        df.index.name = "Date"
        result[ticker] = df

    print(f"[screener] Alpaca bulk bars: {len(result)} tickers fetched.")
    return result


# ── Finnhub company profile (replaces yfinance .info for name/sector) ────────

# Map Finnhub industry labels → SECTOR_MEDIAN_PE keys (GICS-style)
_FINNHUB_INDUSTRY_MAP = {
    "Technology":              "Technology",
    "Software":                "Technology",
    "Semiconductors":          "Technology",
    "Hardware":                "Technology",
    "Healthcare":              "Health Care",
    "Health Care":             "Health Care",
    "Biotechnology":           "Health Care",
    "Pharmaceuticals":         "Health Care",
    "Financial Services":      "Financials",
    "Financials":              "Financials",
    "Banks":                   "Financials",
    "Insurance":               "Financials",
    "Consumer Cyclical":       "Consumer Discretionary",
    "Consumer Defensive":      "Consumer Staples",
    "Communication Services":  "Communication Services",
    "Industrials":             "Industrials",
    "Energy":                  "Energy",
    "Utilities":               "Utilities",
    "Real Estate":             "Real Estate",
    "Basic Materials":         "Materials",
}


def _get_finnhub_profile(ticker: str) -> dict:
    """
    Fetch company name and sector from Finnhub /stock/profile2.
    Returns {"company": str, "sector": str} — falls back to ticker/Unknown on failure.
    Much more reliable than yfinance .info on cloud IPs (no crumb/cookie needed).
    """
    api_key = os.environ.get("FINNHUB_API_KEY", "")
    if not api_key:
        return {"company": ticker, "sector": "Unknown"}
    try:
        resp = requests.get(
            f"{FINNHUB_BASE}/stock/profile2",
            params={"symbol": ticker, "token": api_key},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        name     = data.get("name") or ticker
        industry = data.get("finnhubIndustry") or "Unknown"
        sector   = _FINNHUB_INDUSTRY_MAP.get(industry, "Unknown")
        return {"company": name, "sector": sector}
    except Exception as exc:
        print(f"[screener] Finnhub profile error for {ticker}: {exc}")
        return {"company": ticker, "sector": "Unknown"}


# ── Finnhub fundamental metrics ───────────────────────────────────────────────

def _get_finnhub_metrics(ticker: str) -> dict:
    """
    Fetch basic financial metrics from Finnhub /stock/metric.
    Returns the 'metric' dict, or {} if key is missing / call fails.

    Key fields used:
      peBasicExclExtraTTM        — trailing P/E
      revenueGrowthTTMYoy        — revenue growth YoY (decimal, e.g. 0.17)
      netMarginTTM               — net profit margin (decimal)
      totalDebt/totalEquityAnnual — D/E ratio (ratio, e.g. 1.2 = 120%)
      marketCapitalization       — market cap in $ millions
    """
    api_key = os.environ.get("FINNHUB_API_KEY", "")
    if not api_key:
        return {}
    try:
        resp = requests.get(
            f"{FINNHUB_BASE}/stock/metric",
            params={"symbol": ticker, "metric": "all", "token": api_key},
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json().get("metric", {})
    except Exception as exc:
        print(f"[screener] Finnhub metrics error for {ticker}: {exc}")
        return {}


# ── Finnhub analyst price targets ────────────────────────────────────────────

def _get_analyst_target(ticker: str) -> dict:
    """
    Fetch analyst consensus price target from Finnhub /stock/price-target.
    Returns dict with target_mean, target_high, target_low, upside_pct, or {}
    """
    api_key = os.environ.get("FINNHUB_API_KEY", "")
    if not api_key:
        return {}
    try:
        resp = requests.get(
            f"{FINNHUB_BASE}/stock/price-target",
            params={"symbol": ticker, "token": api_key},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("targetMean"):
            return {}
        return {
            "analyst_target_mean": round(float(data["targetMean"]), 2),
            "analyst_target_high": round(float(data.get("targetHigh", 0)), 2),
            "analyst_target_low":  round(float(data.get("targetLow", 0)), 2),
        }
    except Exception:
        return {}


# ── Finnhub EPS surprise history ─────────────────────────────────────────────

def _get_eps_surprises(ticker: str) -> dict:
    """
    Fetch last 4 quarters of EPS surprise from Finnhub /stock/earnings (free tier).
    Called for LT candidates only — adds meaningful fundamental context.
    Returns beat/miss pattern and most recent surprise %, or {} on failure.
    """
    api_key = os.environ.get("FINNHUB_API_KEY", "")
    if not api_key:
        return {}
    try:
        resp = requests.get(
            f"{FINNHUB_BASE}/stock/earnings",
            params={"symbol": ticker, "limit": 4, "token": api_key},
            timeout=8,
        )
        if resp.status_code != 200:
            return {}
        data = resp.json() or []
        if not data:
            return {}

        beats  = sum(1 for q in data if (q.get("surprise") or 0) > 0)
        misses = len(data) - beats

        # Most recent quarter surprise %
        last_pct = None
        if data[0].get("surprisePercent") is not None:
            last_pct = round(float(data[0]["surprisePercent"]), 1)

        return {
            "eps_beats":            beats,
            "eps_misses":           misses,
            "eps_quarters_checked": len(data),
            "last_eps_surprise_pct": last_pct,
        }
    except Exception as exc:
        print(f"[screener] EPS surprise error for {ticker}: {exc}")
        return {}


# ── Finnhub analyst buy/hold/sell count ───────────────────────────────────────

def _get_analyst_recommendations(ticker: str) -> dict:
    """
    Fetch analyst buy/hold/sell consensus from Finnhub /stock/recommendation (free tier).
    Called for LT candidates only — buy/sell ratio is a meaningful LT signal.
    Returns analyst_buy, analyst_hold, analyst_sell, analyst_consensus, or {} on failure.
    """
    api_key = os.environ.get("FINNHUB_API_KEY", "")
    if not api_key:
        return {}
    try:
        resp = requests.get(
            f"{FINNHUB_BASE}/stock/recommendation",
            params={"symbol": ticker, "token": api_key},
            timeout=8,
        )
        if resp.status_code != 200:
            return {}
        data = resp.json() or []
        if not data:
            return {}

        # Finnhub returns newest first
        latest     = data[0]
        strong_buy = int(latest.get("strongBuy",  0) or 0)
        buy        = int(latest.get("buy",         0) or 0)
        hold       = int(latest.get("hold",        0) or 0)
        sell       = int(latest.get("sell",        0) or 0)
        strong_sell = int(latest.get("strongSell", 0) or 0)

        total_buy  = strong_buy + buy
        total_sell = strong_sell + sell
        total      = total_buy + hold + total_sell

        if total == 0:
            return {}

        buy_pct  = total_buy  / total
        sell_pct = total_sell / total
        consensus = "buy" if buy_pct >= 0.60 else ("sell" if sell_pct >= 0.30 else "hold")

        return {
            "analyst_buy":       total_buy,
            "analyst_hold":      hold,
            "analyst_sell":      total_sell,
            "analyst_total":     total,
            "analyst_consensus": consensus,
        }
    except Exception as exc:
        print(f"[screener] Analyst recommendation error for {ticker}: {exc}")
        return {}


# ── Correlation filter ────────────────────────────────────────────────────────

def _deduplicate_by_correlation(
    picks: list[dict],
    hist_data,
    max_picks: int,
    threshold: float = 0.85,
) -> list[dict]:
    """
    Remove highly-correlated picks to improve portfolio diversification.
    Keeps the higher-scored pick when two are correlated above threshold.

    picks      — list of candidate dicts (must have 'ticker' and 'score' keys)
    hist_data  — DataFrame from yf.download (multi-level columns)
    max_picks  — final number of picks to return
    threshold  — correlation above this triggers deduplication (default 0.85)
    """
    if len(picks) <= 1:
        return picks[:max_picks]

    # hist_data may be None if the download failed — skip correlation filter
    if hist_data is None:
        return picks[:max_picks]

    # Build close price matrix for candidate tickers
    close_dict = {}
    for p in picks:
        t = p["ticker"]
        try:
            if hasattr(hist_data.columns, "levels"):
                col = (t, "Close") if (t, "Close") in hist_data.columns else None
                if col:
                    close_dict[t] = hist_data[col].dropna()
            else:
                if t in hist_data.columns:
                    close_dict[t] = hist_data[t].dropna()
        except Exception:
            pass

    if len(close_dict) < 2:
        return picks[:max_picks]

    price_df  = pd.DataFrame(close_dict).dropna()
    corr_mat  = price_df.pct_change().corr()

    kept   = []
    removed = set()

    for p in picks:
        t = p["ticker"]
        if t in removed:
            continue
        kept.append(p)
        if len(kept) >= max_picks:
            break
        # Check correlation with all already-kept tickers
        for k in [x["ticker"] for x in kept[:-1]]:
            try:
                c = corr_mat.loc[t, k] if (t in corr_mat.index and k in corr_mat.columns) else 0
                if abs(c) >= threshold:
                    # Keep the higher-scored pick; remove the lower-scored duplicate
                    k_score = next((x["score"] for x in kept if x["ticker"] == k), 0)
                    lower   = t if p["score"] <= k_score else k
                    removed.add(lower)
                    if lower == t:
                        kept.pop()           # discard the one we just appended
                    else:
                        kept[:] = [x for x in kept if x["ticker"] != k]  # remove k from kept
                    break
            except Exception:
                pass

    print(f"[screener] Correlation filter: {len(picks)} → {len(kept)} picks "
          f"(removed {len(removed)} correlated duplicates)")
    return kept[:max_picks]


# ── Fundamental scoring (long-term) ──────────────────────────────────────────

def _long_term_score(
    info: dict,
    fh: dict,
    dynamic_sector_pe: dict | None = None,
    ticker: str = "",
) -> tuple[int, dict]:
    """
    Score a ticker for long-term investing (out of 100).
    Prioritises Finnhub metrics (fh) — more reliable than yfinance info.
    Falls back to yfinance info fields when Finnhub returns None.

    dynamic_sector_pe — live sector median P/E computed from the screened universe.
    When provided this replaces the hardcoded SECTOR_MEDIAN_PE dict, giving a
    market-conditions-aware benchmark rather than a fixed historical average.
    """
    score   = 0
    metrics = {}

    sector    = info.get("sector", "Unknown")
    # Use dynamic PE table if provided and has data for this sector; fall back to hardcoded
    pe_table  = dynamic_sector_pe if dynamic_sector_pe else SECTOR_MEDIAN_PE
    median_pe = pe_table.get(sector, SECTOR_MEDIAN_PE.get(sector, SECTOR_MEDIAN_PE["Unknown"]))

    # ── P/E vs sector median (30 pts) ────────────────────────────────────────
    # Tiered: full credit at or below median, partial credit for reasonable growth premium.
    # Avoids unfairly penalising quality compounders (NVDA, AMZN) that trade above median.
    pe = fh.get("peBasicExclExtraTTM") or info.get("trailingPE")
    metrics["pe_ratio"] = round(pe, 1) if pe else None
    if pe and 0 < pe:
        if pe < median_pe:
            score += 30          # at or below median — value opportunity
        elif pe < median_pe * 1.5:
            score += 15          # moderate growth premium — still reasonable
        elif pe < median_pe * 2.5:
            score += 5           # high growth premium — acceptable for top-tier compounders

    # ── Revenue growth > 10% YoY (25 pts) ────────────────────────────────────
    rev_growth = fh.get("revenueGrowthTTMYoy") or info.get("revenueGrowth")
    metrics["revenue_growth"] = rev_growth
    if rev_growth and rev_growth > 0.10:
        score += 25

    # ── Net margin > 10% — replaces FCF (more reliably available) (20 pts) ───
    net_margin = fh.get("netMarginTTM") or info.get("profitMargins")
    metrics["net_margin"] = round(net_margin, 3) if net_margin else None
    if net_margin and net_margin > 0.10:
        score += 20

    # ── Debt-to-equity < 1.0 (15 pts) ────────────────────────────────────────
    # Finnhub: ratio (1.2 = 120%)  |  yfinance: percentage (120 = 1.2x)
    dte_fh = fh.get("totalDebt/totalEquityAnnual")
    dte_yf = info.get("debtToEquity")
    if dte_fh is not None:
        dte = dte_fh          # already a ratio
        dte_ok = dte_fh < 1.0
    elif dte_yf is not None:
        dte = round(dte_yf / 100, 2)
        dte_ok = dte_yf < 100
    else:
        dte, dte_ok = None, False
    metrics["debt_to_equity"] = dte
    if dte_ok:
        score += 15

    # ── Market cap > $10B — gate only, not scored ────────────────────────────
    # Large-cap is a quality minimum, not a scoring differentiator.
    fh_mcap = fh.get("marketCapitalization")
    mktcap  = (fh_mcap * 1_000_000) if fh_mcap else info.get("marketCap", 0)
    metrics["market_cap"] = int(mktcap) if mktcap else None

    # ── Price above 200-day MA — trend confirmation (10 pts) ─────────────────
    # A stock with strong fundamentals but in a long-term downtrend is a value trap.
    # Requiring price > 200MA ensures we buy quality in an uptrend, not falling knives.
    try:
        _sym_lt = ticker or info.get("symbol", "")
        hist_lt = _alpaca_single_bars(_sym_lt, days=365)
        if hist_lt is None or hist_lt.empty:
            import yfinance as _yf_lt
            hist_lt = _yf_lt.Ticker(_sym_lt).history(period="1y", interval="1d")
        if not hist_lt.empty and len(hist_lt) >= 200:
            ma200    = float(hist_lt["Close"].rolling(200).mean().iloc[-1])
            cur_px   = float(hist_lt["Close"].iloc[-1])
            above_ma = cur_px > ma200
            metrics["above_200ma"] = above_ma
            if above_ma:
                score += 10
        elif not hist_lt.empty:
            # < 200 days data — use what we have (50-day proxy)
            ma_proxy = float(hist_lt["Close"].rolling(min(50, len(hist_lt))).mean().iloc[-1])
            cur_px   = float(hist_lt["Close"].iloc[-1])
            metrics["above_200ma"] = cur_px > ma_proxy
            if cur_px > ma_proxy:
                score += 10
    except Exception:
        metrics["above_200ma"] = None

    return score, metrics


# ── Near-miss reason helper ───────────────────────────────────────────────────

def _add_near_miss_reason(pick: dict, is_st: bool) -> dict:
    """
    Add a human-readable 'near_miss_reason' to a candidate that almost qualified.
    Reads the existing metrics dict to find the primary limiting factor.
    """
    pick = dict(pick)
    m = pick.get("st_metrics" if is_st else "lt_metrics") or {}
    reasons = []

    if is_st:
        rsi = m.get("rsi")
        if rsi is not None:
            if rsi > 65:
                reasons.append(f"RSI {rsi:.0f} — overbought")
            elif rsi < 30:
                reasons.append(f"RSI {rsi:.0f} — oversold")
        if not m.get("macd_crossover"):
            reasons.append("no MACD crossover")
        vol = m.get("volume_ratio")
        if vol is not None and vol < 1.5:
            reasons.append(f"vol ratio {vol:.1f}x")
        if not m.get("above_ema20"):
            reasons.append("below EMA20")
    else:
        pe = m.get("pe_ratio")
        if pe and pe > 40:
            reasons.append(f"P/E {pe:.0f} — stretched")
        if not m.get("revenue_growth") and m.get("revenue_growth") != 0:
            reasons.append("no revenue growth data")
        debt = m.get("debt_to_equity")
        if debt and debt > 2:
            reasons.append(f"high debt/equity {debt:.1f}")

    pick["near_miss_reason"] = " · ".join(reasons[:2]) if reasons else "close to threshold"
    return pick


# ── Main screener ─────────────────────────────────────────────────────────────

def run_screener(
    watchlist: list[str] | None = None,
    excluded_sectors: list[str] | None = None,
) -> dict:
    """
    Screen S&P 500 stocks and return top candidates.

    Short-term and long-term candidates are chosen independently:
      ST pool: top 30 by technical score     (momentum/breakout plays)
      LT pool: top 30 by avg dollar volume   (large, liquid, fundamentally screened)

    Steps:
      1. Bulk-download 3 months of price history for 400 tickers (1 API call).
      2. Score all for short-term technicals + compute dollar volume.
      3. Fetch earnings calendar (1 Finnhub call).
      4. Fetch yfinance .info (name/sector) + Finnhub metrics (financials) for
         the union of ST pool + LT pool (~40-50 unique tickers).
      5. Score those for long-term fundamentals using Finnhub data.
      6. Apply sector exclusions, then return top 5 ST + top 5 LT.

    watchlist      — tickers always included in both pools regardless of score
    excluded_sectors — sectors to strip from final picks (e.g. ["Energy", "Utilities"])
    """
    watchlist        = [t.upper() for t in (watchlist or [])]
    excluded_sectors = [s.lower() for s in (excluded_sectors or [])]

    # ── Market regime: adjust pick aggressiveness ─────────────────────────────
    regime_info = get_market_regime()
    regime      = regime_info["regime"]
    multiplier  = regime_pick_multiplier(regime)
    max_st      = max(2, round(5 * multiplier))
    max_lt      = max(2, round(5 * multiplier))
    print(f"[screener] Market regime: {regime} (pick multiplier {multiplier}x, "
          f"max ST={max_st}, LT={max_lt})")

    tickers = get_sp500_tickers()

    # Add watchlist tickers not already in the S&P 500 list
    if watchlist:
        existing = set(tickers)
        extra = [t for t in watchlist if t not in existing]
        if extra:
            tickers = tickers + extra
            print(f"[screener] Watchlist added {len(extra)} extra tickers: {extra}")

    import gc as _gc

    # ── Step 1: SPY baseline — fetched once, freed immediately after ──────────
    # Relative strength vs SPY = stock 20d return minus SPY 20d return.
    spy_20d_return = None
    try:
        _spy_df = _alpaca_single_bars("SPY", days=65)
        _spy_close = _spy_df["Close"].dropna() if _spy_df is not None else pd.Series(dtype=float)
        if len(_spy_close) >= 21:
            spy_20d_return = round(
                (float(_spy_close.iloc[-1]) - float(_spy_close.iloc[-21])) / float(_spy_close.iloc[-21]) * 100, 1
            )
            print(f"[screener] SPY 20d return: {spy_20d_return:+.1f}% (RS baseline)")
    except Exception as exc:
        print(f"[screener] SPY 20d return fetch failed: {exc}")
    finally:
        _spy_df = None; _spy_close = None; _gc.collect()

    # ── Step 2: Chunk download + score (100 tickers at a time) ────────────────
    # Processing in chunks keeps peak DataFrame memory at ~100 tickers instead
    # of 400+ all at once. Same API calls, same data, same scores — 4× lower
    # peak RAM. Each chunk is freed with gc.collect() before the next loads.
    _CHUNK = 100
    all_scored: list[dict] = []
    n_chunks = (len(tickers) + _CHUNK - 1) // _CHUNK
    print(f"[screener] Scoring {len(tickers)} tickers in {n_chunks} chunks of {_CHUNK}...")

    for _ci, _i in enumerate(range(0, len(tickers), _CHUNK), start=1):
        _chunk = tickers[_i:_i + _CHUNK]
        _raw = _alpaca_bulk_bars(_chunk)

        if not _raw:
            # Alpaca unavailable — yfinance fallback for this chunk
            try:
                _yf_raw = yf.download(
                    _chunk, period="3mo", group_by="ticker",
                    auto_adjust=True, threads=True, progress=False, timeout=30,
                )
                if hasattr(_yf_raw.columns, "levels"):
                    _raw = {t: _yf_raw[t].dropna(how="all")
                            for t in set(_yf_raw.columns.get_level_values(0))}
                else:
                    _raw = {}
            except Exception as _exc:
                print(f"[screener] Chunk {_ci}/{n_chunks} fallback failed: {_exc}")
                continue
            finally:
                _yf_raw = None

        for ticker in _chunk:
            try:
                if ticker not in _raw:
                    continue
                hist = _raw[ticker].dropna(how="all")
                if len(hist) < 30:
                    continue
                current_price = float(hist["Close"].iloc[-1])
                if pd.isna(current_price) or current_price <= 0:
                    continue

                # Sanity-check: skip wild outliers (split/data glitches)
                median_price = float(hist["Close"].tail(20).median())
                if median_price > 0 and (current_price > median_price * 3 or current_price < median_price / 3):
                    continue

                st_score, st_metrics = _short_term_score(hist)

                # Relative strength vs SPY
                if spy_20d_return is not None:
                    try:
                        close_s = hist["Close"].dropna()
                        if len(close_s) >= 21:
                            stock_20d = (float(close_s.iloc[-1]) - float(close_s.iloc[-21])) / float(close_s.iloc[-21]) * 100
                            st_metrics["rs_vs_spy"] = round(stock_20d - spy_20d_return, 1)
                    except Exception:
                        pass

                # Dollar volume = avg(close × volume) over last 30 days
                avg_dollar_vol = float((hist["Close"] * hist["Volume"]).tail(30).mean())

                all_scored.append({
                    "ticker":         ticker,
                    "current_price":  round(current_price, 2),
                    "score":          st_score,
                    "avg_dollar_vol": avg_dollar_vol,
                    **st_metrics,
                })
            except Exception:
                continue

        # Free this chunk's DataFrames before loading the next
        _raw = None; _gc.collect()
        print(f"[screener] Chunk {_ci}/{n_chunks} done ({len(all_scored)} scored).")

    print(f"[screener] Scored {len(all_scored)} tickers total.")

    # ── Step 3: Build independent ST and LT candidate pools ──────────────────
    # ST pool: highest technical scores (momentum/breakout plays)
    st_pool = sorted(all_scored, key=lambda x: x["score"], reverse=True)[:ST_CANDIDATE_N]

    # LT pool: apply minimum liquidity gate first, then sort by dollar volume.
    # This ensures we never evaluate fundamentals for micro-caps or illiquid names.
    # Fundamental scoring in Step 5 then re-ranks the pool by quality — so the
    # final picks are both liquid AND fundamentally sound.
    lt_liquid = [s for s in all_scored if s["avg_dollar_vol"] >= MIN_LT_DOLLAR_VOL]
    if len(lt_liquid) < 20:
        # Threshold too strict (e.g. market stress day) — fall back to all tickers
        print(f"[screener] LT liquidity gate yielded only {len(lt_liquid)} tickers — "
              "relaxing threshold to all scored tickers.")
        lt_liquid = all_scored
    lt_pool = sorted(lt_liquid, key=lambda x: x["avg_dollar_vol"], reverse=True)[:LT_CANDIDATE_N]
    print(f"[screener] LT pool: {len(lt_pool)} liquid candidates "
          f"(${MIN_LT_DOLLAR_VOL/1e6:.0f}M+ avg daily vol)")

    # Watchlist bypass — ensure watchlist tickers are in both pools
    if watchlist:
        scored_map = {s["ticker"]: s for s in all_scored}
        in_st = {s["ticker"] for s in st_pool}
        in_lt = {s["ticker"] for s in lt_pool}
        for ticker in watchlist:
            if ticker in scored_map:
                if ticker not in in_st:
                    st_pool.append(scored_map[ticker])
                if ticker not in in_lt:
                    lt_pool.append(scored_map[ticker])
        if watchlist:
            print(f"[screener] Watchlist tickers guaranteed in pools: "
                  f"{[t for t in watchlist if t in scored_map]}")

    # ── Step 4: Earnings calendar ─────────────────────────────────────────────
    all_pool_tickers = list({s["ticker"] for s in st_pool + lt_pool})
    upcoming_earnings = get_upcoming_earnings(all_pool_tickers, days_ahead=5)

    # ── Step 4b: Alpaca real-time prices (replaces yfinance close for current price) ──
    # Falls back to bulk-download close price if Alpaca keys absent or call fails.
    alpaca_prices = _get_alpaca_snapshots(all_pool_tickers)

    # ── Step 4c: Congressional trading (one fetch, cached 24h) ───────────────
    congressional_trades = get_all_congressional_trades()
    print(f"[screener] Congressional data: {len(congressional_trades)} tickers tracked.")

    # ── Step 5: Fetch .info for union of both pools (concurrent, deduplicated) ──
    # ThreadPoolExecutor gives ~5-8× speedup over sequential calls.
    # max_workers=8 keeps Finnhub well within 60 req/min free-tier limit.
    import concurrent.futures, threading

    all_candidates = st_pool + [c for c in lt_pool if c["ticker"] not in {s["ticker"] for s in st_pool}]
    # Deduplicate preserving order
    _seen: set[str] = set()
    all_candidates = [c for c in all_candidates if not (_seen.add(c["ticker"]) or c["ticker"] in _seen - {c["ticker"]})]
    # Simpler dedup:
    _deduped: list = []
    _deduped_set: set[str] = set()
    for _c in all_candidates:
        if _c["ticker"] not in _deduped_set:
            _deduped.append(_c)
            _deduped_set.add(_c["ticker"])
    all_candidates = _deduped
    print(f"[screener] Fetching fundamentals for {len(all_candidates)} unique candidates (parallel)...")

    lt_pool_tickers: set[str] = {c["ticker"] for c in lt_pool}
    _finnhub_lock = threading.Semaphore(4)   # max 4 concurrent Finnhub calls → stays under 60/min

    # dynamic_sector_pe is populated AFTER the first enrichment pass (Step 5b below).
    # _enrich_one reads it via closure — first-pass calls see None (→ hardcoded fallback),
    # LT re-score pass sees the live medians.
    _dynamic_sector_pe: dict | None = None

    def _enrich_one(candidate: dict) -> tuple[str, dict | None]:
        """Enrich a single candidate. Returns (ticker, entry_dict | None)."""
        ticker = candidate["ticker"]

        # ── Company name + sector from Finnhub profile (avoids yfinance .info 401s) ──
        with _finnhub_lock:
            profile = _get_finnhub_profile(ticker)
        company = profile["company"]
        sector  = profile["sector"]

        # ── 52-week range from fast_info (lightweight endpoint, not blocked) ──────
        week52_high = None
        week52_low  = None
        try:
            fi = yf.Ticker(ticker).fast_info
            week52_high = getattr(fi, "year_high", None)
            week52_low  = getattr(fi, "year_low",  None)
        except Exception:
            pass

        # Override current price with Alpaca real-time if available
        if ticker in alpaca_prices:
            candidate = {**candidate, "current_price": round(alpaca_prices[ticker], 2)}

        pct_below_52w_high = None
        if week52_high and candidate["current_price"] > 0 and week52_high > 0:
            pct_below_52w_high = round(
                (week52_high - candidate["current_price"]) / week52_high * 100, 1
            )

        # short_float_pct, days_to_cover, inst_own_pct removed — Yahoo paywalled these
        short_float_pct = None
        days_to_cover   = None
        inst_own_pct    = None

        # Minimal info dict for _long_term_score (sector only — rest comes from Finnhub)
        info = {"sector": sector}

        with _finnhub_lock:
            try:
                fh_metrics = _get_finnhub_metrics(ticker)
            except Exception:
                fh_metrics = {}
            try:
                analyst = _get_analyst_target(ticker)
            except Exception:
                analyst = {}

        is_lt_candidate = ticker in lt_pool_tickers
        eps_surprise = {}
        analyst_rec  = {}
        if is_lt_candidate:
            with _finnhub_lock:
                try:
                    eps_surprise = _get_eps_surprises(ticker)
                except Exception:
                    pass
                try:
                    analyst_rec = _get_analyst_recommendations(ticker)
                    time.sleep(0.1)
                except Exception:
                    pass

        congress = get_congressional_signal(ticker, congressional_trades)

        try:
            lt_score, lt_metrics = _long_term_score(info, fh_metrics, _dynamic_sector_pe, ticker=ticker)
        except Exception as exc:
            print(f"[screener] LT score failed for {ticker}: {exc}")
            lt_score, lt_metrics = 0, {}

        if analyst.get("analyst_target_mean") and candidate["current_price"] > 0:
            upside = (analyst["analyst_target_mean"] - candidate["current_price"]) / candidate["current_price"]
            analyst["analyst_upside_pct"] = round(upside * 100, 1)
            if upside > 0.10:
                lt_score = min(100, lt_score + 5)

        if eps_surprise.get("eps_beats", 0) >= 3 and eps_surprise.get("eps_quarters_checked", 0) >= 3:
            lt_score = min(100, lt_score + 5)

        if congress.get("congress_score", 0) >= 6:
            lt_score = min(100, lt_score + 8)
        elif congress.get("congress_score", 0) >= 3:
            lt_score = min(100, lt_score + 4)

        st_metrics_extra = {k: v for k, v in candidate.items()
                            if k not in ("ticker", "current_price", "score", "avg_dollar_vol")}
        if pct_below_52w_high is not None:
            st_metrics_extra["pct_below_52w_high"] = pct_below_52w_high

        entry: dict = {
            "ticker":        ticker,
            "company":       company,
            "sector":        sector,
            "current_price": candidate["current_price"],
            "st_score":      candidate["score"],
            "lt_score":      lt_score,
            "st_metrics":    st_metrics_extra,
            "lt_metrics":    {**lt_metrics, **analyst, **eps_surprise, **analyst_rec,
                              **{k: v for k, v in congress.items() if congress.get("congress_score", 0) > 0}},
        }
        if week52_high is not None:
            entry["week52_high"] = round(week52_high, 2)
        if week52_low is not None:
            entry["week52_low"]  = round(week52_low, 2)
        if pct_below_52w_high is not None:
            entry["pct_below_52w_high"] = pct_below_52w_high

        days_to_earnings = None
        if ticker in upcoming_earnings:
            entry["earnings_date"] = upcoming_earnings[ticker]
            try:
                from datetime import date as _date, datetime as _dt
                ed = upcoming_earnings[ticker]
                ed_parsed = _dt.strptime(str(ed)[:10], "%Y-%m-%d").date() if isinstance(ed, str) else ed
                days_to_earnings = (ed_parsed - _date.today()).days
            except Exception:
                pass

        if short_float_pct  is not None: entry["short_float_pct"]   = short_float_pct
        if days_to_cover    is not None: entry["days_to_cover"]      = days_to_cover
        if inst_own_pct     is not None: entry["inst_own_pct"]       = inst_own_pct
        if days_to_earnings is not None: entry["days_to_earnings"]   = days_to_earnings
        # rs_vs_spy lives in st_metrics_extra — hoist to top level so _opt_extra
        # can include it in LT candidate data (it's a valid LT signal: rs > 0% = holding up vs market)
        rs = st_metrics_extra.get("rs_vs_spy")
        if rs is not None: entry["rs_vs_spy"] = rs

        return ticker, entry

    enriched: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_enrich_one, c): c["ticker"] for c in all_candidates}
        for future in concurrent.futures.as_completed(futures):
            try:
                ticker, entry = future.result(timeout=45)   # 45s cap — prevent yfinance hangs
                if entry is not None:
                    enriched[ticker] = entry
            except concurrent.futures.TimeoutError:
                t = futures[future]
                print(f"[screener] Enrichment timed out for {t} — skipping.")
            except Exception as exc:
                t = futures[future]
                print(f"[screener] Enrichment failed for {t}: {exc}")


    # ── Step 5b: Compute dynamic sector PE medians + re-score LT candidates ─────
    # The hardcoded SECTOR_MEDIAN_PE dict uses historical averages that drift
    # from current market conditions (e.g. Tech was 28× in 2019, 35×+ in 2021).
    # We compute live medians from the enriched universe and re-score LT picks.
    # This re-score only adjusts the PE component (30 pts) — other signals unchanged.

    _sector_pe_data: dict[str, list[float]] = {}
    for _e in enriched.values():
        _s  = _e.get("sector", "Unknown")
        _pe = _e.get("lt_metrics", {}).get("pe_ratio")
        if _pe and 0 < _pe < 200:   # cap at 200× to exclude distorted outliers
            _sector_pe_data.setdefault(_s, []).append(_pe)

    if len(_sector_pe_data) >= 3:
        # Build dynamic table: median PE per sector from the screened universe
        _dynamic_sector_pe = {}
        for _s, _pes in _sector_pe_data.items():
            _sorted = sorted(_pes)
            _dynamic_sector_pe[_s] = round(_sorted[len(_sorted) // 2], 1)
        # Fill any gaps (sectors not in the enriched universe) from the static table
        for _s, _v in SECTOR_MEDIAN_PE.items():
            _dynamic_sector_pe.setdefault(_s, _v)
        print(f"[screener] Dynamic sector PE medians (from {len(enriched)} tickers): "
              f"{_dynamic_sector_pe}")

        # Re-score LT candidates with dynamic medians.
        # Only the PE comparison changes; other components stay as-is.
        for _ticker, _entry in enriched.items():
            if _ticker not in lt_pool_tickers:
                continue
            _sector      = _entry.get("sector", "Unknown")
            _pe          = _entry.get("lt_metrics", {}).get("pe_ratio")
            _static_med  = SECTOR_MEDIAN_PE.get(_sector, SECTOR_MEDIAN_PE["Unknown"])
            _dynamic_med = _dynamic_sector_pe.get(_sector, _static_med)
            if _pe and 0 < _pe and _static_med != _dynamic_med:
                _was_good = 0 < _pe < _static_med
                _now_good = 0 < _pe < _dynamic_med
                if _now_good and not _was_good:
                    _entry["lt_score"] = min(100, _entry["lt_score"] + 30)
                    print(f"[screener] {_ticker}: PE {_pe:.1f} now below dynamic median "
                          f"{_dynamic_med:.1f} (was above {_static_med}) → +30 pts")
                elif _was_good and not _now_good:
                    _entry["lt_score"] = max(0, _entry["lt_score"] - 30)
                    print(f"[screener] {_ticker}: PE {_pe:.1f} now above dynamic median "
                          f"{_dynamic_med:.1f} (was below {_static_med}) → -30 pts")
    else:
        print(f"[screener] Insufficient sector PE data ({len(_sector_pe_data)} sectors) — "
              "using hardcoded SECTOR_MEDIAN_PE.")

    # ── Step 6: Build final top-5 lists ───────────────────────────────────────
    def _opt_earnings(e: dict) -> dict:
        return {"earnings_date": e["earnings_date"]} if "earnings_date" in e else {}

    def _opt_extra(e: dict) -> dict:
        """Include all optional top-level context fields if present."""
        out = {}
        for field in (
            "week52_high", "week52_low", "pct_below_52w_high",
            "short_float_pct", "days_to_cover",
            "inst_own_pct", "days_to_earnings",
            "rs_vs_spy",   # relative strength vs SPY (20d) — LT signal too
        ):
            if e.get(field) is not None:
                out[field] = e[field]
        return out

    def _opt_congress(e: dict) -> dict:
        """Include congressional signal if score > 0."""
        lt = e.get("lt_metrics", {})
        if lt.get("congress_score", 0) > 0:
            return {k: lt[k] for k in
                    ("congress_buys", "congress_members", "congress_score",
                     "is_cluster", "note") if k in lt}
        return {}

    def _flatten_short(e: dict) -> dict:
        return {"ticker": e["ticker"], "company": e["company"], "sector": e["sector"],
                "current_price": e["current_price"], "score": e["st_score"],
                **e["st_metrics"], **_opt_earnings(e), **_opt_extra(e)}

    def _flatten_long(e: dict) -> dict:
        return {"ticker": e["ticker"], "company": e["company"], "sector": e["sector"],
                "current_price": e["current_price"], "score": e["lt_score"],
                **e["lt_metrics"], **_opt_earnings(e), **_opt_extra(e), **_opt_congress(e)}

    # Apply sector exclusions
    if excluded_sectors:
        before = len(enriched)
        enriched = {
            k: v for k, v in enriched.items()
            if v.get("sector", "").lower() not in excluded_sectors
        }
        removed = before - len(enriched)
        if removed:
            print(f"[screener] Removed {removed} tickers from excluded sectors.")

    # ST top-N: from the ST pool only (ranked by technical score)
    short_candidates = [_flatten_short(enriched[s["ticker"]]) for s in st_pool
                        if s["ticker"] in enriched]
    short_candidates = sorted(short_candidates, key=lambda x: x["score"], reverse=True)

    # LT top-N: from the LT pool only (ranked by fundamental score)
    long_candidates = [_flatten_long(enriched[s["ticker"]]) for s in lt_pool
                       if s["ticker"] in enriched]
    long_candidates = sorted(long_candidates, key=lambda x: x["score"], reverse=True)

    # Fetch historical price data for correlation deduplication
    _all_candidate_tickers = list(dict.fromkeys(
        [s["ticker"] for s in short_candidates] +
        [s["ticker"] for s in long_candidates]
    ))
    raw = None
    if _all_candidate_tickers:
        try:
            import yfinance as _yf
            raw = _yf.download(_all_candidate_tickers, period="30d", progress=False)
        except Exception as _raw_exc:
            print(f"[screener] correlation data fetch failed (non-critical): {_raw_exc}")

    # Apply correlation filter to remove redundant picks
    short_top = _deduplicate_by_correlation(short_candidates, raw, max_st)
    long_top  = _deduplicate_by_correlation(long_candidates,  raw, max_lt)

    print(f"[screener] Top short-term: {[s['ticker'] for s in short_top]}")
    print(f"[screener] Top long-term:  {[s['ticker'] for s in long_top]}")
    print(f"[screener] Regime: {regime} — {regime_info['note']}")

    # ── Near-misses: top candidates that didn't make the final cut ────────────
    _top_st_tickers = {s["ticker"] for s in short_top}
    _top_lt_tickers = {s["ticker"] for s in long_top}
    nm_st = [_add_near_miss_reason(c, is_st=True)
             for c in short_candidates if c["ticker"] not in _top_st_tickers][:3]
    nm_lt = [_add_near_miss_reason(c, is_st=False)
             for c in long_candidates  if c["ticker"] not in _top_lt_tickers][:3]

    return {
        "short_term":  short_top,
        "long_term":   long_top,
        "regime":      regime_info,
        "near_misses": {"short_term": nm_st, "long_term": nm_lt},
    }


# ── CLI test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json
    results = run_screener()
    print(json.dumps(results, indent=2, default=str))
