"""Point-in-time fundamentals from SEC EDGAR XBRL — free, and the reason the
long-term score can finally be backtested.

WHY THIS EXISTS
Finnhub serves CURRENT fundamentals only, so scoring a 2024 pick with 2026 P/E
is look-ahead. `backtest_walkforward` therefore EXCLUDES the long-term score
entirely — which leaves the leg carrying ~90 of the LT rubric's ~100 points
(P/E 30, revenue growth 25, net margin 20, debt/equity 15) never validated.

SEC EDGAR's `companyfacts` API returns every reported figure with the date it
was **filed**. Taking only rows where `filed <= as_of` reproduces what the
market actually knew on that date. That is the whole mechanism.

🔴 DEVIATION FROM PRODUCTION — restate this with any number this produces.
Production scores on Finnhub TTM figures. EDGAR gives clean ANNUAL (FY/10-K)
figures; assembling a true TTM from XBRL means stitching quarters and inferring
Q4 from FY minus Q1-Q3, which introduces more error than it removes. So this
uses the LAST ANNUAL FIGURE FILED AS OF THE DATE. For a multi-year thesis that
is defensible, but it is NOT the same input production uses, and a backtest
built on it measures the RUBRIC, not the exact live pipeline.

🔴 IT IS MEASUREMENT, NEVER INPUT. Nothing here may be imported by the screener
or the pick prompt. Feeding backtest-derived fundamentals into selection is the
contamination `evaluate_picks` exists to avoid.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import requests

# SEC requires a descriptive User-Agent with contact info and throttles at
# ~10 req/s. Both are conditions of access, not politeness.
_UA = os.environ.get(
    "SEC_USER_AGENT",
    "StockPulz research vasanth.sanil@gmail.com",
)
_HEADERS = {"User-Agent": _UA, "Accept-Encoding": "gzip, deflate"}
_MAX_PER_SEC = 8          # under SEC's 10/s, with headroom

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

# The concepts the four fundamentals legs need. Revenue and debt are lists
# because filers use different tags by era and industry — the first that
# yields a usable value wins.
_EPS = ["EarningsPerShareDiluted", "EarningsPerShareBasic"]
_REVENUE = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",  # post-ASC 606
    "Revenues",
    "SalesRevenueNet",                                      # pre-2018 filers
    "RevenueFromContractWithCustomerIncludingAssessedTax",
]
_NET_INCOME = ["NetIncomeLoss", "ProfitLoss"]
_EQUITY = [
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
]
_DEBT_LONG = ["LongTermDebtNoncurrent", "LongTermDebt",
              "LongTermDebtAndCapitalLeaseObligations",
              "ConvertibleDebtNoncurrent", "ConvertibleDebt",
              "SecuredDebt", "UnsecuredDebt",
              "DebtLongtermAndShorttermCombinedAmount"]
_DEBT_SHORT = ["LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings",
               "LongTermDebtAndCapitalLeaseObligationsCurrent",
               "LinesOfCreditCurrent", "NotesPayableCurrent"]


class _RateLimiter:
    """A real time-windowed limiter, not a concurrency bound.

    Deliberately the same shape as `screener._RateLimiter`: a semaphore caps
    how many calls run at once, which is not a rate at all — four workers at
    100ms each is ~2,400/min. That misconception cost this project 65% of its
    fundamentals coverage for weeks.
    """

    def __init__(self, max_per_sec: int = _MAX_PER_SEC):
        self.max = max_per_sec
        self._times: list[float] = []

    def acquire(self) -> float:
        while True:
            now = time.monotonic()
            self._times = [t for t in self._times if now - t < 1.0]
            if len(self._times) < self.max:
                self._times.append(now)
                return now
            time.sleep(max(0.02, 1.0 - (now - self._times[0])))


_limiter = _RateLimiter()
_ticker_map: dict[str, int] | None = None


def _get(url: str) -> Any:
    _limiter.acquire()
    r = requests.get(url, headers=_HEADERS, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def ticker_to_cik(ticker: str) -> int | None:
    """SEC spells class shares with a DASH (BRK-B), matching our own tickers —
    unlike Alpaca, which uses a dot and rejects the whole request on a miss."""
    global _ticker_map
    if _ticker_map is None:
        data = _get(_TICKERS_URL) or {}
        _ticker_map = {str(v["ticker"]).upper(): int(v["cik_str"])
                       for v in data.values()}
    return _ticker_map.get(str(ticker).upper())


def company_facts(ticker: str) -> dict | None:
    """Raw companyfacts for a ticker, or None if SEC does not know it."""
    cik = ticker_to_cik(ticker)
    if cik is None:
        return None
    return _get(_FACTS_URL.format(cik=cik))


def _rows(facts: dict, concept: str) -> list[dict]:
    node = ((facts or {}).get("facts", {}).get("us-gaap", {}) or {}).get(concept)
    if not node:
        return []
    out = []
    for unit_rows in (node.get("units") or {}).values():
        out.extend(unit_rows)
    return out


def as_of(facts: dict, concepts: list[str], date: str,
          annual_only: bool = True) -> tuple[float | None, dict]:
    """The value a reader could have known on `date`, plus its provenance.

    🔴 `filed <= date` is the entire point — it is what separates this from
    look-ahead. Among what was filed by then, take the most recent fiscal
    period (`end`); if the same period was filed more than once (a restatement),
    take the LATEST such filing, because that is the figure standing on that
    date.
    """
    for concept in concepts:
        rows = [r for r in _rows(facts, concept)
                if r.get("filed") and r["filed"] <= date and r.get("val") is not None]
        if annual_only:
            rows = [r for r in rows if r.get("fp") == "FY" and r.get("form") == "10-K"]
        if not rows:
            continue
        rows.sort(key=lambda r: (r.get("end") or "", r.get("filed") or ""))
        best = rows[-1]
        return float(best["val"]), {"concept": concept, "end": best.get("end"),
                                    "filed": best.get("filed"),
                                    "fy": best.get("fy")}
    return None, {}


def _prior_year(facts: dict, concepts: list[str], date: str,
                current_end: str | None) -> float | None:
    """The same concept one fiscal year earlier — for a growth rate that
    compares like with like rather than whatever happened to be filed."""
    if not current_end:
        return None
    for concept in concepts:
        rows = [r for r in _rows(facts, concept)
                if r.get("filed") and r["filed"] <= date
                and r.get("fp") == "FY" and r.get("form") == "10-K"
                and r.get("val") is not None and (r.get("end") or "") < current_end]
        if not rows:
            continue
        rows.sort(key=lambda r: (r.get("end") or "", r.get("filed") or ""))
        return float(rows[-1]["val"])
    return None


def fundamentals_as_of(ticker: str, date: str, price: float | None = None,
                       facts: dict | None = None) -> dict | None:
    """Point-in-time inputs for the four fundamentals legs of the LT score.

    Returns None when SEC has no filings for the ticker (ETFs, most ADRs, and
    anything that does not file 10-Ks). A caller must treat None as "cannot
    score", never as zero — a missing fundamental is not a bad one.
    """
    facts = facts if facts is not None else company_facts(ticker)
    if not facts:
        return None

    eps, eps_p = as_of(facts, _EPS, date)
    rev, rev_p = as_of(facts, _REVENUE, date)
    ni, _ = as_of(facts, _NET_INCOME, date)
    eq, _ = as_of(facts, _EQUITY, date)
    dl, _ = as_of(facts, _DEBT_LONG, date)
    ds, _ = as_of(facts, _DEBT_SHORT, date)

    rev_prior = _prior_year(facts, _REVENUE, date, rev_p.get("end"))

    # 🔴 No borrowing tag anywhere, but a balance sheet that was filed, means
    # the company almost certainly has none — XBRL requires debt to be tagged
    # when material. Reading that as "unknown" would skip the leg and PENALISE
    # a debt-free company, which is the opposite of what the rubric intends and
    # would bias the backtest against exactly the balance sheets it prefers.
    # It is an inference, so it is FLAGGED and counted rather than hidden.
    debt_inferred_zero = False
    if dl is None and ds is None and eq is not None:
        dl, debt_inferred_zero = 0.0, True

    out: dict = {
        "ticker": ticker,
        "as_of": date,
        "eps_annual": eps,
        "revenue": rev,
        "revenue_prior": rev_prior,
        "net_income": ni,
        "equity": eq,
        "debt": (dl or 0) + (ds or 0) if (dl is not None or ds is not None) else None,
        "debt_inferred_zero": debt_inferred_zero,
        "source": {"eps": eps_p, "revenue": rev_p},
    }

    # Derived — each guarded, because a zero denominator here silently produces
    # inf and poisons every aggregate downstream (the plausible_price lesson).
    out["pe_ratio"] = (price / eps) if (price and eps and eps > 0) else None
    out["revenue_growth"] = ((rev - rev_prior) / rev_prior
                             if (rev is not None and rev_prior) else None)
    out["net_margin"] = (ni / rev) if (ni is not None and rev) else None
    out["debt_to_equity"] = (out["debt"] / eq
                             if (out["debt"] is not None and eq and eq > 0) else None)
    return out
