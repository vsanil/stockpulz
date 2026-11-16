"""One mapping from a ticker to the symbol Yahoo knows it by.

Found 2026-11-15, checking every coin in `price_checker._SYMBOL_TO_CG_ID` against
CoinGecko: `<SYM>-USD` returned NOTHING for UNI, HYPE, SUI, APT and PEPE, and a
DIFFERENT, unrelated coin for ARB ($0.0006 vs $0.13) and TON ($0.002 vs $1.07).
Twenty sites built `f"{t}-USD"` by hand, so every one of them inherited both
faults. And `webhook._backtest_fetch_prices` tried the BARE symbol first — bare
"BTC" is a ~$30 instrument — so a crypto buy logged with a past date was priced
about 2,000x too low.
"""
import ast
import pathlib

import pytest

import price_checker as pc

ROOT = pathlib.Path(__file__).resolve().parent.parent


class TestTheMapping:
    @pytest.mark.parametrize("t,want", [
        ("BTC", "BTC-USD"), ("eth", "ETH-USD"), (" sol ", "SOL-USD"),
        ("SUI", "SUI20947-USD"), ("uni", "UNI7083-USD"), ("HYPE", "HYPE32196-USD"),
        ("APT", "APT21794-USD"), ("PEPE", "PEPE24478-USD"),
    ])
    def test_a_known_coin_gets_its_yahoo_symbol(self, t, want):
        assert pc.yf_symbol(t) == want

    @pytest.mark.parametrize("t", ["ARB", "TON"])
    def test_the_two_wrong_coin_symbols_are_never_used(self, t):
        """`ARB-USD` and `TON-USD` are OTHER coins on Yahoo. Using them is worse
        than no price: a number that looks real."""
        assert pc.yf_symbol(t) != f"{t}-USD"

    @pytest.mark.parametrize("t", ["AAPL", "BRK-B", "GC=F", "^VIX", "SPY"])
    def test_a_stock_is_returned_unchanged(self, t):
        assert pc.yf_symbol(t) == t

    def test_crypto_flag_suffixes_a_coin_outside_the_map(self):
        assert pc.yf_symbol("ZZZCOIN", crypto=True) == "ZZZCOIN-USD"

    def test_an_already_suffixed_symbol_is_not_doubled(self):
        assert pc.yf_symbol("FOO-USD", crypto=True) == "FOO-USD"

    def test_every_override_is_a_coin_the_app_knows(self):
        """An override for a coin outside the canonical map is dead config."""
        assert set(pc.YF_CRYPTO_SYMBOLS) <= set(pc.CRYPTO_SYMBOLS)

    def test_matic_is_not_mapped_to_pol(self):
        """MATIC was renamed POL; CoinGecko's `matic-network` is not the token
        Yahoo's POL symbol prices, so mapping it would invent a price."""
        assert "MATIC" not in pc.YF_CRYPTO_SYMBOLS


class TestEveryCallerUsesIt:
    @staticmethod
    def _project_files():
        for p in ROOT.rglob("*.py"):
            rel = p.relative_to(ROOT).as_posix()
            if rel.startswith((".venv/", "tests/", "graphify-out/")) or "/site-packages/" in rel:
                continue
            yield rel, p

    @staticmethod
    def _offences(src: str):
        """Real code that builds a `-USD` symbol by hand — an f-string ending in
        `-USD` after a placeholder, or `x + "-USD"`. Walks the AST, so comments
        and docstrings that NAME the pattern cannot trip it."""
        out = []
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.JoinedStr) and len(node.values) >= 2:
                last, prev = node.values[-1], node.values[-2]
                if (isinstance(last, ast.Constant) and str(last.value).endswith("-USD")
                        and isinstance(prev, ast.FormattedValue)):
                    out.append(node.lineno)
            if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)
                    and isinstance(node.right, ast.Constant)
                    and str(node.right.value).endswith("-USD")):
                out.append(node.lineno)
            if isinstance(node, ast.Tuple) and any(
                    isinstance(e, ast.Constant) and e.value == "-USD" for e in node.elts):
                out.append(node.lineno)
        return out

    def test_no_one_builds_a_usd_symbol_by_hand(self):
        bad = []
        for rel, p in self._project_files():
            for line in self._offences(p.read_text(encoding="utf-8")):
                if rel == "price_checker.py":
                    continue          # the helper itself is the one allowed place
                bad.append(f"{rel}:{line}")
        assert not bad, ("build Yahoo symbols with price_checker.yf_symbol(), "
                         f"not by hand: {bad}")

    def test_the_scan_can_find_an_offender(self):
        """A guard that cannot fail is not a guard."""
        assert self._offences('t = "SUI"\nsym = f"{t}-USD"\n')
        assert self._offences('t = "SUI"\nsym = t + "-USD"\n')
        assert self._offences('for s in ("", "-USD"):\n    pass\n')
        assert not self._offences('"""f"{t}-USD" in prose"""\nx = "BTC-USD"\n')


class TestTheCallersAskForTheRightSymbol:
    def test_the_evaluator_can_price_sui(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "evaluate_picks_sym", ROOT / "scripts" / "evaluate_picks.py")
        ev = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ev)
        assert ev._yf_symbol("SUI", "crypto") == "SUI20947-USD"
        assert ev._yf_symbol("TON", "crypto") == "TON11419-USD"
        assert ev._yf_symbol("AAPL", "stock") == "AAPL"

    def test_market_data(self):
        import market_data
        assert market_data._yf_sym("ton") == "TON11419-USD"

    def test_agent_map_keeps_the_original_ticker(self):
        import agent
        assert agent._yf_symbol_map(["UNI", "AAPL"]) == {"UNI7083-USD": "UNI", "AAPL": "AAPL"}

    def test_backtest_fetch_never_asks_for_bare_btc(self, monkeypatch):
        """Bare "BTC" is a real Yahoo symbol for an unrelated ~$30 instrument.
        Trying it first is what priced past-dated BTC buys ~2,000x too low."""
        import yfinance
        import webhook
        asked = []

        class _T:
            def __init__(self, sym):
                asked.append(sym)

            def history(self, **k):
                import pandas as pd
                idx = pd.to_datetime(["2026-11-13"])
                return pd.DataFrame({"Close": [90000.0]}, index=idx)

        monkeypatch.setattr(yfinance, "Ticker", _T)
        dates, closes = webhook._backtest_fetch_prices("BTC")
        assert asked == ["BTC-USD"], asked
        assert closes == [90000.0]

    def test_backtest_fetch_still_tries_bare_first_for_a_stock(self, monkeypatch):
        import yfinance
        import webhook
        asked = []

        class _T:
            def __init__(self, sym):
                asked.append(sym)

            def history(self, **k):
                import pandas as pd
                return pd.DataFrame({"Close": []})

        monkeypatch.setattr(yfinance, "Ticker", _T)
        monkeypatch.setattr(webhook.requests, "get",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
        webhook._backtest_fetch_prices("AAPL")
        assert asked[:2] == ["AAPL", "AAPL-USD"]
