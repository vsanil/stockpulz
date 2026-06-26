"""
chart_generator.py — Generate candlestick chart images for picks.
Uses yfinance for OHLCV data and mplfinance for rendering.
Returns PNG bytes ready for Telegram sendPhoto.

Chart includes:
  - 30-day candlesticks (dark theme)
  - Volume bars
  - MA20 (amber)
  - Entry  — solid green horizontal line  (price panel only)
  - Target — dashed cyan horizontal line  (price panel only)
  - Stop   — dashed red horizontal line   (price panel only)
  - Right-side price labels for each level
"""
from __future__ import annotations

import io
import matplotlib
matplotlib.use('Agg')   # non-interactive backend — must be set before pyplot import

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import yfinance as yf
import mplfinance as mpf


# ── Dark chart style ──────────────────────────────────────────────────────────

_DARK_BG  = '#0f0f1a'
_GRID_COL = '#1e1e2e'

_STYLE = mpf.make_mpf_style(
    base_mpf_style = 'nightclouds',
    gridcolor      = _GRID_COL,
    gridstyle      = ':',
    facecolor      = _DARK_BG,
    figcolor       = _DARK_BG,
    edgecolor      = _DARK_BG,
    rc = {
        'axes.labelcolor':  '#888888',
        'xtick.color':      '#666666',
        'ytick.color':      '#888888',
        'font.size':        9,
        'axes.titlecolor':  '#dddddd',
        'axes.titlesize':   11,
    },
)

# ── Known crypto symbols (for yfinance -USD suffix) ───────────────────────────

# Canonical crypto set — single source of truth (was a local hardcoded list).
from price_checker import CRYPTO_SYMBOLS as _CRYPTO_SYMBOLS


def is_crypto(ticker: str) -> bool:
    return ticker.upper() in _CRYPTO_SYMBOLS


def _fmt_price(p: float) -> str:
    """Format price compactly — no trailing zeros."""
    if p >= 1000:
        return f"${p:,.0f}"
    if p >= 10:
        return f"${p:.2f}"
    return f"${p:.4f}"


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
        target:     Target price — dashed cyan horizontal line
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

    # ── Moving average ────────────────────────────────────────────────────────
    add_plots = []
    closes = hist["Close"]
    if len(closes) >= 20:
        ma20 = closes.rolling(20).mean()
        add_plots.append(mpf.make_addplot(ma20, color='#ffab40', width=1.2, label='MA20'))

    # ── Render — returnfig=True so we can draw level lines on price axis only ─
    buf = io.BytesIO()
    try:
        fig, axes = mpf.plot(
            hist,
            type         = 'candle',
            style        = _STYLE,
            volume       = True,
            figsize      = (10, 6),
            addplot      = add_plots if add_plots else [],
            returnfig    = True,
            warn_too_much_data = 100,
        )

        price_ax = axes[0]   # top panel — price
        n_bars   = len(hist)
        x_right  = n_bars - 0.3   # x position for right-side labels

        # ── Draw level lines on price axis only ───────────────────────────────
        levels = []
        if entry  is not None: levels.append((float(entry),  '#00e676', '-',  'Entry'))
        if target is not None: levels.append((float(target), '#40c4ff', '--', 'Target'))
        if stop   is not None: levels.append((float(stop),   '#ff5252', '--', 'Stop'))

        for price_val, color, style, label in levels:
            price_ax.axhline(
                y         = price_val,
                color     = color,
                linestyle = style,
                linewidth = 1.4,
                alpha     = 0.85,
                zorder    = 5,
            )
            # Right-side label with price
            price_ax.text(
                x_right, price_val,
                f" {label} {_fmt_price(price_val)}",
                color     = color,
                fontsize  = 8.5,
                fontweight= 'bold',
                va        = 'center',
                ha        = 'left',
                zorder    = 6,
                bbox      = dict(
                    boxstyle  = 'round,pad=0.2',
                    facecolor = _DARK_BG,
                    edgecolor = color,
                    alpha     = 0.75,
                    linewidth = 0.8,
                ),
            )

        # ── Current price marker on right y-axis ──────────────────────────────
        last_close = float(closes.iloc[-1])
        price_ax.annotate(
            f"  {_fmt_price(last_close)}",
            xy        = (1, last_close),
            xycoords  = ('axes fraction', 'data'),
            fontsize  = 8.5,
            color     = '#ffffff',
            va        = 'center',
            ha        = 'left',
        )

        # ── Title ──────────────────────────────────────────────────────────────
        price_ax.set_title(
            f"{ticker}  ·  {days}-day",
            color    = '#dddddd',
            fontsize = 11,
            pad      = 8,
        )

        # ── Save ───────────────────────────────────────────────────────────────
        fig.savefig(
            buf,
            dpi         = 140,
            bbox_inches = 'tight',
            facecolor   = _DARK_BG,
        )
        plt.close(fig)
        buf.seek(0)
        data = buf.getvalue()
        print(f"[chart] Generated {len(data)//1024}KB chart for {ticker}")
        return data

    except Exception as exc:
        print(f"[chart] Render error for {ticker}: {exc}")
        plt.close('all')
        return None
