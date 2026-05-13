"""
price_alert_manager.py — Price alert storage, evaluation, and notification.

Alerts are persisted as price_alerts.json in the GitHub Gist.
Each alert fires once when the target price is crossed and is then removed.

Usage via Telegram commands:
  /alert NVDA 1000        → notify when NVDA crosses $1000 (auto: above or below current)
  /alert AAPL below 175   → notify when AAPL drops below $175
  /alerts                 → list all active alerts
  /unalert NVDA           → remove all NVDA alerts
  /unalert NVDA 1000      → remove specific alert
"""

from datetime import datetime
import yfinance as yf

from config_manager import _load_gist_file, _write_gist_file

ALERTS_FILENAME = "price_alerts.json"

# Crypto symbols that need the -USD suffix for yfinance
_CRYPTO_SYMBOLS = {
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "DOT", "MATIC",
    "LINK", "UNI", "ATOM", "LTC", "BCH", "ALGO", "XLM", "VET", "ICP", "FIL",
    "TRX", "NEAR", "OP", "ARB", "SUI", "APT", "INJ", "SEI", "TIA",
}


# ── Internal helpers ───────────────────────────────────────────────────────────

def _load_alerts() -> dict:
    return _load_gist_file(ALERTS_FILENAME) or {}


def _save_alerts(alerts: dict) -> None:
    _write_gist_file(ALERTS_FILENAME, alerts)


def _current_price(ticker: str) -> float | None:
    """Fetch current price. Crypto tickers use TICKER-USD format for yfinance."""
    ticker = ticker.upper()
    yf_symbol = f"{ticker}-USD" if ticker in _CRYPTO_SYMBOLS else ticker
    try:
        price = yf.Ticker(yf_symbol).fast_info.last_price
        if price:
            return float(price)
    except Exception:
        pass
    # Fallback: try CoinGecko for crypto if yfinance failed
    if ticker in _CRYPTO_SYMBOLS:
        try:
            import requests
            _COINGECKO_IDS = {
                "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
                "BNB": "binancecoin", "XRP": "ripple", "ADA": "cardano",
                "DOGE": "dogecoin", "AVAX": "avalanche-2", "DOT": "polkadot",
                "MATIC": "matic-network", "LINK": "chainlink", "UNI": "uniswap",
                "ATOM": "cosmos", "LTC": "litecoin", "BCH": "bitcoin-cash",
                "ALGO": "algorand", "XLM": "stellar", "VET": "vechain",
                "ICP": "internet-computer", "FIL": "filecoin", "TRX": "tron",
                "NEAR": "near", "OP": "optimism", "ARB": "arbitrum",
                "SUI": "sui", "APT": "aptos", "INJ": "injective-protocol",
            }
            cg_id = _COINGECKO_IDS.get(ticker)
            if cg_id:
                resp = requests.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": cg_id, "vs_currencies": "usd"},
                    timeout=8,
                )
                resp.raise_for_status()
                price = resp.json().get(cg_id, {}).get("usd")
                if price:
                    return float(price)
        except Exception:
            pass
    return None


# ── Public API ─────────────────────────────────────────────────────────────────

def add_alert(chat_id: str, ticker: str, target_price: float,
              direction: str = "auto") -> str:
    """
    Set a price alert.

    direction: "above" | "below" | "auto"
      - "auto": alert fires above target if target > current price, below otherwise
    Returns a formatted confirmation string for Telegram.
    """
    ticker  = ticker.upper()
    current = _current_price(ticker)
    if current is None:
        return f"❌ Could not fetch current price for <b>{ticker}</b>. Is the ticker correct?"

    if direction == "auto":
        direction = "above" if target_price > current else "below"

    alerts      = _load_alerts()
    chat_alerts = alerts.setdefault(str(chat_id), [])

    # Prevent duplicate
    for a in chat_alerts:
        if a["ticker"] == ticker and a["target"] == target_price and a["direction"] == direction:
            return f"⚠️ Alert already exists: <b>{ticker}</b> {direction} <b>${target_price:,.2f}</b>"

    chat_alerts.append({
        "ticker":       ticker,
        "target":       target_price,
        "direction":    direction,
        "set_at":       datetime.utcnow().isoformat(),
        "price_at_set": round(current, 2),
    })
    _save_alerts(alerts)

    pct  = abs(target_price - current) / current * 100
    arrow = "📈" if direction == "above" else "📉"
    return (
        f"{arrow} <b>Alert set</b>\n"
        f"Notify when <b>{ticker}</b> goes <b>{direction} ${target_price:,.2f}</b>\n"
        f"Currently <b>${current:,.2f}</b> ({pct:.1f}% away)"
    )


def remove_alert(chat_id: str, ticker: str, target_price: float | None = None) -> str:
    """Remove alert(s) for a ticker. If target_price given, removes only that one."""
    ticker      = ticker.upper()
    alerts      = _load_alerts()
    chat_alerts = alerts.get(str(chat_id), [])
    before      = len(chat_alerts)

    if target_price is not None:
        chat_alerts = [
            a for a in chat_alerts
            if not (a["ticker"] == ticker and a["target"] == target_price)
        ]
    else:
        chat_alerts = [a for a in chat_alerts if a["ticker"] != ticker]

    removed = before - len(chat_alerts)
    if removed:
        alerts[str(chat_id)] = chat_alerts
        _save_alerts(alerts)
        return f"✅ Removed {removed} alert(s) for <b>{ticker}</b>."
    return f"⚠️ No active alerts found for <b>{ticker}</b>."


def list_alerts(chat_id: str) -> str:
    """Return formatted Telegram HTML list of active alerts for this chat."""
    chat_alerts = _load_alerts().get(str(chat_id), [])
    if not chat_alerts:
        return "📋 No active price alerts.\n\nSet one with /alert — e.g. <code>NVDA 1000</code>"

    lines = [f"📋 <b>PRICE ALERTS</b> ({len(chat_alerts)} active)\n"]
    for a in chat_alerts:
        arrow = "📈" if a["direction"] == "above" else "📉"
        lines.append(
            f"{arrow} <b>{a['ticker']}</b> — "
            f"{a['direction']} <b>${a['target']:,.2f}</b> "
            f"<i>(set at ${a['price_at_set']:,.2f})</i>"
        )
    return "\n".join(lines)


def check_alerts(chat_id: str, send_fn=None) -> list[str]:
    """
    Evaluate all alerts for a chat against current prices.
    Triggered alerts are removed and returned as formatted Telegram strings.

    Optionally pass send_fn(msg) to fire notifications immediately.
    """
    alerts      = _load_alerts()
    chat_alerts = alerts.get(str(chat_id), [])
    if not chat_alerts:
        return []

    # Batch price fetch
    tickers = list({a["ticker"] for a in chat_alerts})
    prices  = {}
    for t in tickers:
        p = _current_price(t)
        if p is not None:
            prices[t] = p

    triggered = []
    remaining = []

    for a in chat_alerts:
        t       = a["ticker"]
        current = prices.get(t)
        if current is None:
            remaining.append(a)
            continue

        hit = (
            (a["direction"] == "above" and current >= a["target"]) or
            (a["direction"] == "below" and current <= a["target"])
        )

        if hit:
            arrow  = "📈" if a["direction"] == "above" else "📉"
            change = (current - a["price_at_set"]) / a["price_at_set"] * 100
            msg    = (
                f"🔔 <b>PRICE ALERT TRIGGERED</b> {arrow}\n\n"
                f"<b>{t}</b> is now <b>${current:,.2f}</b>\n"
                f"Target: {a['direction']} ${a['target']:,.2f} ✅\n"
                f"<i>Change since alert set: {change:+.1f}%</i>"
            )
            triggered.append(msg)
            if send_fn:
                send_fn(msg)
        else:
            remaining.append(a)

    if triggered:
        alerts[str(chat_id)] = remaining
        _save_alerts(alerts)

    return triggered


def check_all_alerts(send_fn=None) -> int:
    """
    Check alerts for ALL chat IDs (called from cron runs).
    Returns total number of alerts triggered.
    """
    alerts  = _load_alerts()
    total   = 0
    for chat_id in list(alerts.keys()):
        fired = check_alerts(chat_id, send_fn=send_fn)
        total += len(fired)
    return total
