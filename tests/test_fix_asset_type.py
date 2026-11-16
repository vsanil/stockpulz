"""The one-off coin retype (scripts/fix_asset_type.py) changes only what it should."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("fix_asset_type", ROOT / "scripts" / "fix_asset_type.py")
fat = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fat)

COINS = frozenset({"BTC", "BNB", "XLM"})


def test_a_coin_stored_as_a_stock_is_retyped():
    log = {"open": [{"ticker": "BNB", "asset_type": "stock"}], "closed": []}
    fixed, changes = fat.retype(log, COINS)
    assert fixed["open"][0]["asset_type"] == "crypto"
    assert changes == [("open", "BNB", "stock")]


def test_closed_trades_and_missing_types_too():
    log = {"open": [], "closed": [{"ticker": "btc"}]}
    fixed, changes = fat.retype(log, COINS)
    assert fixed["closed"][0]["asset_type"] == "crypto" and len(changes) == 1


def test_stocks_and_correct_coins_are_untouched():
    log = {"open": [{"ticker": "AAPL", "asset_type": "stock"},
                    {"ticker": "XLM", "asset_type": "crypto"},
                    {"ticker": "SPY", "asset_type": "etf"}], "closed": []}
    fixed, changes = fat.retype(log, COINS)
    assert changes == [] and fixed == log


def test_the_input_is_not_mutated():
    """`mutate_user_trade_log` hands the mutator a record it may compare against;
    editing it in place is the shallow-copy bug that skipped writes (Aug 22)."""
    log = {"open": [{"ticker": "BNB", "asset_type": "stock"}], "closed": []}
    fat.retype(log, COINS)
    assert log["open"][0]["asset_type"] == "stock"


def test_refuses_any_store_but_supabase(monkeypatch):
    import storage

    class _Gist:
        def name(self):
            return "gist"

    monkeypatch.setattr(storage, "get_storage_backend", lambda: _Gist())
    monkeypatch.setattr("sys.argv", ["fix_asset_type.py", "--apply"])
    assert fat.main() == 2
