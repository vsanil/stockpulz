"""No project file may use a name that is never defined.

🔴 Found 2026-11-15: `agent._download_prices` called `yf.download` but agent.py
only imports yfinance inside other functions. Every call raised NameError into
`except Exception: return {}`, so from 2026-06-08 the midday check, tax-loss
nudges, the Friday wrap's unrealised P&L, target-approach nudges and pick entry
alerts all silently did nothing. Every test stubbed `_download_prices`, so none
of them could see it; pyflakes found it in a second.

A swallowing `except` turns an undefined name into a feature that does nothing
— the same class as the two names called-but-never-imported in agent.py
(2026-08-19) and the /admin drawer's import of a class that never existed.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _project_files():
    for base in (ROOT, os.path.join(ROOT, "scripts")):
        for name in sorted(os.listdir(base)):
            if name.endswith(".py"):
                yield os.path.join(base, name)


def _undefined(path):
    from pyflakes import api, reporter as rep

    class _Collect(rep.Reporter):
        def __init__(self):
            self.found = []

        def flake(self, message):
            if type(message).__name__ == "UndefinedName":
                self.found.append(f"{os.path.relpath(message.filename, ROOT)}:"
                                  f"{message.lineno} {message.message_args[0]}")

        def unexpectedError(self, filename, msg):
            self.found.append(f"{filename}: {msg}")

        def syntaxError(self, filename, msg, lineno, offset, text):
            self.found.append(f"{filename}:{lineno}: {msg}")

    r = _Collect()
    with open(path, encoding="utf-8") as fh:
        api.check(fh.read(), path, r)
    return r.found


def test_no_project_file_uses_an_undefined_name():
    found = [f for p in _project_files() for f in _undefined(p)]
    assert found == [], "undefined names (a swallowing except makes each one a silent outage):\n  " \
        + "\n  ".join(found)


def test_the_scan_can_detect_an_offender(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("def f():\n    try:\n        return yf.download('A')\n    except Exception:\n        return {}\n")
    assert any(" yf" in f for f in _undefined(str(p)))


def test_the_price_helper_actually_reaches_yfinance(monkeypatch):
    """Drives the REAL `_download_prices` — the thing every other test stubbed."""
    import pandas as pd
    import yfinance
    import agent
    asked = []

    def _fake(symbols, **kw):
        asked.append(symbols)
        cols = pd.MultiIndex.from_product([["Close"], ["AAPL", "BTC-USD"]])
        return pd.DataFrame([[190.0, 68000.0]], columns=cols)
    monkeypatch.setattr(yfinance, "download", _fake)
    out = agent._download_prices(["AAPL", "BTC"], period="1d", interval="1d")
    assert asked == ["AAPL BTC-USD"], "crypto must be asked for under its -USD symbol"
    assert out == {"AAPL": 190.0, "BTC": 68000.0}


def test_a_failed_fetch_is_said_out_loud(monkeypatch, capsys):
    import yfinance
    import agent
    monkeypatch.setattr(yfinance, "download",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("blocked")))
    assert agent._download_prices(["AAPL"]) == {}
    assert "_download_prices failed" in capsys.readouterr().out
