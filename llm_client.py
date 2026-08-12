"""
llm_client.py — Shared Anthropic client singleton + LLM response helpers.

Import _get_client() from here instead of creating per-module instances, and
strip_fences() before json.loads() on ANY model output.
"""
from __future__ import annotations

import os
import re
import anthropic

_anthropic_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    """Return a shared Anthropic client, created lazily on first call."""
    global _anthropic_client
    if _anthropic_client is None:
        # Explicit timeout/retries — the SDK default (~600s × 2) can hang the
        # whole morning pick run on a slow API call.
        _anthropic_client = anthropic.Anthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            timeout=60.0,
            max_retries=2,
        )
    return _anthropic_client


# ── Response parsing ─────────────────────────────────────────────────────────
_FENCE_RE = re.compile(r"```[ \t]*[A-Za-z0-9_+-]*[ \t]*\r?\n?(.*?)```", re.S)


def strip_fences(raw: str) -> str:
    """Unwrap markdown code fences around model output.

    🔴 Why this is load-bearing: models routinely return correct JSON wrapped in
    ```json fences, and `json.loads` on that fails at char 0 with the misleading
    "Expecting value: line 1 column 1". Callers swallow the exception, so the
    feature degrades SILENTLY. That was live on 2026-08-11 at five sites — most
    damagingly `cmd_nlp`, where it meant EVERY natural-language command fell
    through to the generic chat fallback and no intent ever executed: "bought 10
    AAPL at 220" returned commentary instead of logging the position.

    Deliberately conservative. Input that is already bare JSON is returned
    unchanged, so the three ai_analyzer callers on the morning-pick critical path
    behave exactly as before; the added tolerance only affects input that would
    otherwise have raised.
    """
    if not raw:
        return raw
    text = raw.strip()
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    # An unterminated fence (truncated response) — take everything after it.
    if text.startswith("```"):
        body = text[3:]
        nl = body.find("\n")
        first = body[:nl] if nl != -1 else ""
        if nl != -1 and (not first.strip() or first.strip().isalnum()):
            body = body[nl + 1:]
        return body.strip()
    return text
