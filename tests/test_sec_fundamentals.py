"""Point-in-time fundamentals from SEC EDGAR.

The long-term score carries ~90 of its ~100 points on four fundamentals legs
and has NEVER been backtested, because Finnhub serves current figures only and
scoring a 2024 pick with 2026 data is look-ahead. EDGAR stamps every figure
with the date it was FILED, which is what makes point-in-time possible.

`filed <= as_of` is therefore the load-bearing rule in this whole module, and
most of these tests exist to make a look-ahead regression impossible to miss.
"""
import sec_fundamentals as sf


def _facts(rows_by_concept):
    return {"facts": {"us-gaap": {
        c: {"units": {"USD": rows}} for c, rows in rows_by_concept.items()}}}


def _row(end, filed, val, fp="FY", form="10-K"):
    return {"end": end, "filed": filed, "val": val, "fp": fp, "form": form}


class TestPointInTime:
    def test_a_figure_filed_after_the_date_is_INVISIBLE(self):
        """The whole mechanism. FY2024 was not public in June 2024."""
        f = _facts({"Revenues": [_row("2023-12-31", "2024-02-01", 100),
                                 _row("2024-12-31", "2025-02-01", 200)]})
        val, prov = sf.as_of(f, ["Revenues"], "2024-06-30")
        assert val == 100 and prov["filed"] == "2024-02-01"

    def test_the_same_date_later_sees_the_newer_filing(self):
        f = _facts({"Revenues": [_row("2023-12-31", "2024-02-01", 100),
                                 _row("2024-12-31", "2025-02-01", 200)]})
        assert sf.as_of(f, ["Revenues"], "2025-06-30")[0] == 200

    def test_a_restatement_uses_the_figure_STANDING_on_that_date(self):
        """Same fiscal period filed twice. On a date after the restatement, the
        restated number is what a reader would have seen."""
        f = _facts({"Revenues": [_row("2023-12-31", "2024-02-01", 100),
                                 _row("2023-12-31", "2024-08-01", 95)]})
        assert sf.as_of(f, ["Revenues"], "2024-05-01")[0] == 100   # before restatement
        assert sf.as_of(f, ["Revenues"], "2024-09-01")[0] == 95    # after

    def test_nothing_filed_yet_returns_None_not_zero(self):
        """A missing fundamental is not a bad one. Zero would score as terrible
        debt-free equity and silently flatter the rubric."""
        f = _facts({"Revenues": [_row("2024-12-31", "2025-02-01", 200)]})
        assert sf.as_of(f, ["Revenues"], "2020-01-01") == (None, {})

    def test_it_falls_through_the_concept_list(self):
        """Filers changed revenue tags at ASC 606; pre-2018 names must still
        resolve or every older backtest date scores blind."""
        f = _facts({"SalesRevenueNet": [_row("2015-12-31", "2016-02-01", 50)]})
        val, prov = sf.as_of(f, sf._REVENUE, "2017-01-01")
        assert val == 50 and prov["concept"] == "SalesRevenueNet"

    def test_quarterly_rows_are_excluded_from_annual_reads(self):
        f = _facts({"Revenues": [_row("2024-03-31", "2024-04-30", 25, fp="Q1", form="10-Q"),
                                 _row("2023-12-31", "2024-02-01", 100)]})
        assert sf.as_of(f, ["Revenues"], "2024-06-30")[0] == 100


class TestDerivedRatios:
    def _f(self, **rows):
        return _facts(rows)

    def test_growth_compares_consecutive_fiscal_years(self):
        f = self._f(Revenues=[_row("2022-12-31", "2023-02-01", 100),
                              _row("2023-12-31", "2024-02-01", 110)])
        out = sf.fundamentals_as_of("X", "2024-06-30", facts=f)
        assert abs(out["revenue_growth"] - 0.10) < 1e-9

    def test_the_prior_year_is_also_point_in_time(self):
        """A prior-year figure filed later than as_of must not sneak in."""
        f = self._f(Revenues=[_row("2022-12-31", "2025-02-01", 100),
                              _row("2023-12-31", "2024-02-01", 110)])
        assert sf.fundamentals_as_of("X", "2024-06-30", facts=f)["revenue_growth"] is None

    def test_a_zero_denominator_yields_None_not_inf(self):
        """An inf poisons every aggregate it touches instead of failing loudly —
        the same family as the plausible_price and NaN-in-score bugs."""
        f = self._f(Revenues=[_row("2023-12-31", "2024-02-01", 0)],
                    NetIncomeLoss=[_row("2023-12-31", "2024-02-01", 5)],
                    StockholdersEquity=[_row("2023-12-31", "2024-02-01", 0)],
                    LongTermDebtNoncurrent=[_row("2023-12-31", "2024-02-01", 10)])
        out = sf.fundamentals_as_of("X", "2024-06-30", facts=f)
        assert out["net_margin"] is None and out["debt_to_equity"] is None

    def test_a_negative_eps_yields_no_pe(self):
        """A loss-making company has no meaningful P/E; a negative one would
        score as a bargain."""
        f = self._f(EarningsPerShareDiluted=[_row("2023-12-31", "2024-02-01", -2.0)])
        assert sf.fundamentals_as_of("X", "2024-06-30", price=50,
                                     facts=f)["pe_ratio"] is None

    def test_debt_sums_long_and_short(self):
        f = self._f(LongTermDebtNoncurrent=[_row("2023-12-31", "2024-02-01", 80)],
                    LongTermDebtCurrent=[_row("2023-12-31", "2024-02-01", 20)],
                    StockholdersEquity=[_row("2023-12-31", "2024-02-01", 50)])
        out = sf.fundamentals_as_of("X", "2024-06-30", facts=f)
        assert out["debt"] == 100 and out["debt_to_equity"] == 2.0

    def test_a_debt_free_filer_scores_zero_not_unknown(self):
        """🔴 Measured on real data: VRTX and PRAX tag no borrowings at all.
        Reading that as "unknown" skips the leg and PENALISES a debt-free
        balance sheet — the opposite of what the rubric rewards, and a bias
        against exactly the companies it prefers. XBRL requires material debt
        to be tagged, so absence on a filed balance sheet means none."""
        f = _facts({"StockholdersEquity": [_row("2023-12-31", "2024-02-01", 500)]})
        out = sf.fundamentals_as_of("PRAX", "2024-06-30", facts=f)
        assert out["debt_to_equity"] == 0.0
        assert out["debt_inferred_zero"] is True, "an inference must be flagged"

    def test_the_inference_needs_a_filed_balance_sheet(self):
        """No equity either means we know nothing, not that debt is zero."""
        f = _facts({"Revenues": [_row("2023-12-31", "2024-02-01", 100)]})
        out = sf.fundamentals_as_of("X", "2024-06-30", facts=f)
        assert out["debt_to_equity"] is None and out["debt_inferred_zero"] is False

    def test_a_real_debt_tag_is_never_overridden_by_the_inference(self):
        f = _facts({"StockholdersEquity": [_row("2023-12-31", "2024-02-01", 100)],
                    "LongTermDebtNoncurrent": [_row("2023-12-31", "2024-02-01", 50)]})
        out = sf.fundamentals_as_of("X", "2024-06-30", facts=f)
        assert out["debt_to_equity"] == 0.5 and out["debt_inferred_zero"] is False

    def test_an_unknown_ticker_returns_None(self, monkeypatch):
        """ETFs and most ADRs file no 10-K. None means 'cannot score', and a
        caller must never read it as a bad score."""
        monkeypatch.setattr(sf, "company_facts", lambda t: None)
        assert sf.fundamentals_as_of("SPY", "2024-06-30") is None


class TestAccessDiscipline:
    def test_the_rate_limiter_is_time_windowed_not_a_semaphore(self):
        """SEC throttles at ~10 req/s. A concurrency bound is not a rate bound —
        that misconception cost this project 65% of its Finnhub coverage."""
        lim = sf._RateLimiter(max_per_sec=3)
        import time
        t0 = time.monotonic()
        for _ in range(5):
            lim.acquire()
        assert time.monotonic() - t0 >= 0.5, "5 calls at 3/s cannot finish instantly"

    def test_the_user_agent_carries_contact_info(self):
        """SEC blocks requests without one — it is a condition of access."""
        assert "@" in sf._HEADERS["User-Agent"]

    def test_it_is_MEASUREMENT_and_the_engine_never_imports_it(self):
        """Feeding backtest-derived fundamentals into selection is exactly the
        contamination evaluate_picks exists to avoid."""
        import pathlib
        for f in ("screener.py", "ai_analyzer.py", "agent.py"):
            assert "sec_fundamentals" not in pathlib.Path(f).read_text(), \
                f"{f} imports the backtest fundamentals source"
