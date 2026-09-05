"""Shape-normalising helpers for yfinance results.

🔴 WHY THIS EXISTS. `yf.download()` changed shape under us. Since yfinance
~0.2.51 it returns MULTI-LEVEL columns — `(field, ticker)` — even for a SINGLE
ticker. So the long-standing idiom

    yf.download("^VIX", period="1d")["Close"]

no longer yields a Series. It yields a one-column DataFrame, and the usual
follow-up `float(x.iloc[-1])` then raises:

    TypeError: float() argument must be a string or a real number, not 'Series'

MEASURED in production 2026-09-05: `vix_check` had been failing on exactly this
on every run, printing `vix_check: VIX fetch failed: ...` inside a bare
`except Exception` — so the job exited 0, the workflow went green, and the VIX
alert had silently never fired. Nothing else noticed.

🔑 The reason this is a MODULE and not an inline fix at the broken line: the
same idiom appears at several call sites across the codebase, and this repo has
already been bitten once by fixing the site instead of the class (the `price > 0`
guard was declared "fully closed" and four more live sites surfaced weeks later).
One helper, one place to correct, and `test_yf_utils.py` pins the shapes.

⚠️ Do NOT "simplify" this by passing `multi_level_index=False` to `yf.download`:
that keyword does not exist on older yfinance versions and would trade a wrong
shape for an outright TypeError on the pinned version. Normalising the RESULT
works on every version, which is the property we want from a data provider we
do not control.
"""
from __future__ import annotations


def close_series(raw):
    """Return a 1-D close-price Series from a single-ticker `yf.download` frame.

    Accepts what `yf.download(...)` returns (the whole frame) OR what
    `yf.download(...)["Close"]` returns, under both the flat and the multi-level
    column layouts. Returns None when there is no usable data, so callers can
    branch instead of catching.

    🔎 Deliberately tolerant about its input: call sites historically wrote
    either `download(...)["Close"]` or kept the frame, and making both work is
    cheaper than auditing every caller into one style.
    """
    if raw is None:
        return None

    obj = raw
    # Whole frame → pull the Close field out first, whichever layout it is in.
    cols = getattr(obj, "columns", None)
    if cols is not None:
        if getattr(cols, "nlevels", 1) > 1:
            if "Close" in cols.get_level_values(0):
                obj = obj["Close"]
        elif "Close" in cols:
            obj = obj["Close"]

    # Single-ticker multi-level frames leave a one-column DataFrame behind.
    if getattr(obj, "ndim", 1) > 1:
        if obj.shape[1] == 0:
            return None
        obj = obj.iloc[:, 0]

    if obj is None or getattr(obj, "empty", False):
        return None
    return obj


def latest_close(raw):
    """Last close as a float, or None. Never raises on shape.

    ⚠️ Returns None rather than 0.0 on failure. A zero here would flow into
    price comparisons, and this codebase has a documented history of a bogus
    $0.00 satisfying every "below target" test and cascading into false alerts.
    """
    s = close_series(raw)
    if s is None:
        return None
    try:
        s = s.dropna()
    except Exception:
        pass
    if s is None or len(s) == 0:
        return None
    try:
        return float(s.iloc[-1])
    except (TypeError, ValueError):
        return None
