"""
chart_generator.py — Generate candlestick chart images for picks.
Uses yfinance for OHLCV data and mplfinance for rendering.
Returns PNG bytes ready for Telegram sendPhoto.

Chart includes:
  - 30-day candlesticks (dark theme)
  - Volume bars
  - MA20 (amber) + MA50 (purple) if enough history
  - Entry price  — solid green horizontal line
  - Target price — dashed blue horizontal line
  - Stop loss    — dashed red horizontal line
"""

import io
import matplotlib
matplotlib.use('Agg')   # non-interactive backend — must be set before pyplot import

import yfinance as yf
import mplfinance as mpf


# ── Dark chart style ──────────────────────────────────────────────────────────

_DARK_BG   = '#0f0f1a'
_GRID_COL  = '#1e1e2e'

_STYLE = mpf.make_mpf_style(
    base_mpf_style = 'nightclouds',
    gridcolor      = _GRID_COL,
    gridstyle      = ':',
    facecolor      = _DARK_BG,
    figcolor       = _DARK_BG,
    edgecolor      = _DARK_BG,
    rc = {
        'axes.labelcolor':  '#aaaaaa',
        'xtick.color':      '#777777',
        'ytick.color':      '#aaaaaa',
        'font.size':        9,
        'axes.titlecolor':  '#dddddd',
        'axes.titlesize':   11,
    },
)

# ── Known crypto symbols (for yfinance -USD suffix) ───────────────────────────

_CRYPTO_SYMBOLS = {
    'BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'ADA', 'DOGE', 'AVAX', 'DOT',
    'MATIC', 'LINK', 'UNI', 'ATOM', 'LTC', 'BCH', 'ALGO', 'XLM', 'VET',
    'ICP', 'FIL', 'HYPE', 'SUI', 'APT', 'ARB', 'OP', 'INJ', 'TIA', 'SEI',
}


def is_crypto(ticker: str) -> bool:
    return ticker.upper() in _CRYPTO_SYMBOLS


# ── Main chart function ───────────────────────────────────────────────────────

def generate_chart(
    ticker:     str,
    entry:      float | None = None,
    target:     float | None = None,
    stop:       float | None = None,
    asset_type: str          = "stock",
    days:       int          = 30,
) -> bytes | None:
    """
    Generate a candlestick chart PNG for a ticker.

    Args:
        ticker:     Stock symbol (e.g. 'AAPL') or crypto symbol (e.g. 'BTC')
        entry:      Entry price — solid green horizontal line
        target:     Target price — dashed blue horizontal line
        stop:       Stop-loss price — dashed red horizontal line
        asset_type: 'stock' or 'crypto' (crypto fetches TICKER-USD from yfinance)
        days:       Calendar days of history to show (default 30)

    Returns:
        PNG image as bytes, or None on failure.
    """
    ticker = ticker.upper()

    # Crypto uses TICKER-USD on yfinance
    yf_ticker = f"{ticker}-USD" if asset_type == "crypto" or is_crypto(ticker) else ticker

    # ── Fetch OHLCV ───────────────────────────────────────────────────────────
    try:
        hist = yf.Ticker(yf_ticker).history(period=f"{max(days + 20, 60)}d", interval="1d")
        if hist.empty or len(hist) < 5:
            print(f"[chart] No data for {yf_ticker}")
            return None
        hist = hist.tail(days)[["Open", "High", "Low", "Close", "Volume"]]
        hist.index = hist.index.tz_localize(None)   # strip timezone for mplfinance
    except Exception as exc:
        print(f"[chart] Data fetch error for {yf_ticker}: {exc}")
        return None

    # ── Pick level lines ──────────────────────────────────────────────────────
    hline_vals   = []
    hline_colors = []
    hline_styles = []

    if entry is not None:
        hline_vals.append(float(entry))
        hline_colors.append('#00e676')   # bright green — entry
        hline_styles.append('-')
    if target is not None:
        hline_vals.append(float(target))
        hline_colors.append('#40c4ff')   # bright blue — target
        hline_styles.append('--')
    if stop is not None:
        hline_vals.append(float(stop))
        hline_colors.append('#ff5252')   # bright red — stop
        hline_styles.append('--')

    hlines_kwargs = {}
    if hline_vals:
        hlines_kwargs = dict(
            hlines    = hline_vals,
            colors    = hline_colors,
            linestyle = hline_styles,
            linewidths = 1.3,
        )

    # ── Moving averages ───────────────────────────────────────────────────────
    add_plots = []
    closes = hist["Close"]
    if len(closes) >= 20:
        ma20 = closes.rolling(20).mean()
        add_plots.append(mpf.make_addplot(ma20, color='#ffab40', width=1.1, label='MA20'))
    if len(closes) >= 50:
        ma50 = closes.rolling(50).mean()
        add_plots.append(mpf.make_addplot(ma50, color='#ce93d8', width=1.1, label='MA50'))

    # ── Render to bytes ───────────────────────────────────────────────────────
    buf = io.BytesIO()
    try:
        plot_kwargs: dict = dict(
            type         = 'candle',
            style        = _STYLE,
            volume       = True,
            figsize      = (10, 6),
            title        = f"\n{ticker}  ·  {days}-day",
            tight_layout = True,
            warn_too_much_data = 100,
            savefig      = dict(
                fname        = buf,
                dpi          = 140,
                bbox_inches  = 'tight',
                facecolor    = _DARK_BG,
            ),
        )
        if hlines_kwargs:
            plot_kwargs['hlines'] = hlines_kwargs
        if add_plots:
            plot_kwargs['addplot'] = add_plots

        mpf.plot(hist, **plot_kwargs)
        buf.seek(0)
        data = buf.getvalue()
        print(f"[chart] Generated {len(data)//1024}KB chart for {ticker}")
        return data

    except Exception as exc:
        print(f"[chart] Render error for {ticker}: {exc}")
        return None
