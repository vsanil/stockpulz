"""
dividends_checker.py — Fetch dividend info for a list of tickers using yfinance.

Used by the /dividends command to show:
  - Annual dividend yield %
  - Next ex-dividend date
  - Last dividend amount
"""

import yfinance as yf
from datetime import date, datetime


def get_dividend_info(tickers: list[str]) -> list[dict]:
    """
    Fetch dividend info for a list of stock tickers.

    Args:
        tickers: List of stock symbols (e.g. ['AAPL', 'MSFT', 'KO'])

    Returns:
        List of dicts with keys:
          ticker, yield_pct, ex_date, amount, pays_dividend
        Sorted by ex_date ascending (soonest first), non-dividend payers last.
    """
    results = []
    for ticker in tickers:
        try:
            info = yf.Ticker(ticker).info

            # Dividend yield
            raw_yield = info.get("dividendYield") or info.get("trailingAnnualDividendYield")
            yield_pct = round(float(raw_yield) * 100, 2) if raw_yield else None

            # Ex-dividend date — yfinance returns as Unix timestamp
            ex_ts  = info.get("exDividendDate")
            ex_date = None
            if ex_ts:
                try:
                    ex_date = datetime.utcfromtimestamp(int(ex_ts)).date()
                except Exception:
                    ex_date = None

            # Last dividend per share
            amount = info.get("lastDividendValue") or info.get("trailingAnnualDividendRate")
            amount = round(float(amount), 4) if amount else None

            pays = bool(yield_pct and yield_pct > 0)

            results.append({
                "ticker":       ticker.upper(),
                "yield_pct":    yield_pct,
                "ex_date":      ex_date,
                "amount":       amount,
                "pays_dividend": pays,
            })
        except Exception as exc:
            print(f"[dividends] Error fetching {ticker}: {exc}")
            results.append({
                "ticker":       ticker.upper(),
                "yield_pct":    None,
                "ex_date":      None,
                "amount":       None,
                "pays_dividend": False,
            })

    # Sort: payers first (soonest ex-date), non-payers last
    today = date.today()
    def _sort_key(r):
        if not r["pays_dividend"]:
            return (1, date.max)
        if r["ex_date"]:
            # Future ex-dates first, then past ones
            return (0, r["ex_date"] if r["ex_date"] >= today else date.max)
        return (0, date.max)

    results.sort(key=_sort_key)
    return results


def format_dividends_message(dividend_info: list[dict]) -> str:
    """Format dividend info list into a Telegram HTML message."""
    if not dividend_info:
        return "📭 No positions to check dividends for."

    payers     = [r for r in dividend_info if r["pays_dividend"]]
    non_payers = [r for r in dividend_info if not r["pays_dividend"]]
    today      = date.today()

    lines = ["<b>💰 Dividends — Your Positions</b>\n"]

    if payers:
        for r in payers:
            ticker    = r["ticker"]
            yld       = f"{r['yield_pct']:.2f}%" if r["yield_pct"] else "—"
            amt       = f"${r['amount']:.4f}/share" if r["amount"] else ""
            ex        = r["ex_date"]
            if ex:
                days_away = (ex - today).days
                if days_away < 0:
                    ex_str = f"{ex.strftime('%b %d')} <i>(past)</i>"
                elif days_away == 0:
                    ex_str = f"{ex.strftime('%b %d')} <b>TODAY</b> ⚠️"
                elif days_away <= 7:
                    ex_str = f"{ex.strftime('%b %d')} <i>({days_away}d away)</i>"
                else:
                    ex_str = ex.strftime("%b %d, %Y")
            else:
                ex_str = "unknown"

            lines.append(
                f"<b>{ticker}</b>  yield <code>{yld}</code>  {amt}\n"
                f"  Ex-div: {ex_str}"
            )
    else:
        lines.append("<i>None of your open positions pay dividends.</i>")

    if non_payers:
        tickers_str = ", ".join(r["ticker"] for r in non_payers)
        lines.append(f"\n<i>No dividend: {tickers_str}</i>")

    lines.append(
        "\n<i>Ex-dividend date = last day to own shares to receive the next payout.</i>"
    )

    return "\n".join(lines)
