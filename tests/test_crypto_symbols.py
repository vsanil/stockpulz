"""
test_crypto_symbols.py — the crypto symbol set is consolidated to ONE source.

Regression: the codebase used to carry 8 divergent hardcoded crypto lists, so a
coin the screener picked (HYPE, TON, …) would work in one feature and break in
another (mispriced as a bare stock ticker, or no CoinGecko fallback id). All
modules now import price_checker.CRYPTO_SYMBOLS; the symbol→CG-id map is the
single source of truth.
"""

from price_checker import CRYPTO_SYMBOLS, _SYMBOL_TO_CG_ID


class TestCanonicalCryptoSet:
    def test_set_derives_from_cg_id_map(self):
        assert CRYPTO_SYMBOLS == frozenset(_SYMBOL_TO_CG_ID)

    def test_previously_broken_coins_present(self):
        # HYPE (the $0.00 alert culprit) and TON (in only 2 of 8 old lists)
        for sym in ("HYPE", "TON", "TRX", "NEAR", "SHIB", "PEPE", "BONK", "WLD", "FLOKI"):
            assert sym in CRYPTO_SYMBOLS, f"{sym} missing from canonical set"

    def test_every_symbol_has_a_coingecko_id(self):
        # No symbol may lack a CG id, else the CoinGecko price fallback is dead.
        missing = [s for s in CRYPTO_SYMBOLS if not _SYMBOL_TO_CG_ID.get(s)]
        assert missing == [], f"symbols without CG id: {missing}"


class TestModulesShareCanonicalSet:
    """Every module that gates -USD suffix / crypto-detection uses the one set."""

    def test_alert_manager_uses_canonical(self):
        import price_alert_manager as pam
        assert pam._CRYPTO_SYMBOLS is CRYPTO_SYMBOLS

    def test_market_data_uses_canonical(self):
        import market_data as md
        assert md._CRYPTO_SYMBOLS is CRYPTO_SYMBOLS

    def test_chart_generator_recognises_new_coins(self):
        import chart_generator as cg
        assert cg.is_crypto("HYPE") is True
        assert cg.is_crypto("TON") is True
        assert cg.is_crypto("AAPL") is False

    def test_webhook_chart_crypto_is_canonical(self):
        import webhook
        assert webhook._CHART_CRYPTO is CRYPTO_SYMBOLS
