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


# ── Transient-failure taxonomy ───────────────────────────────────────────────
# 🔴 WHY THIS EXISTS: on 2026-09-30 Anthropic returned HTTP 529 "Overloaded" at
# 11:01 UTC and BOTH real users got no picks that day. The run reported success
# with the failure printed inside it — this repo's signature shape.
#
# `analyze_with_claude` already had a Haiku fallback, but it was gated on
# `except (json.JSONDecodeError, KeyError, IndexError)` — PARSE errors only. An
# API error never reached it and raised at once. The only retry was the SDK's
# `max_retries=2`, whose backoff is measured in seconds and is useless against a
# capacity event lasting minutes.
#
# 🔑 THE DISTINCTION THAT MATTERS, and it decides the right fix:
#   a PARSE error  -> we got output and it was malformed -> a stricter prompt on
#                     another model is the cure (STRICT_RETRY_SYSTEM).
#   an API error   -> we got NO output at all            -> retry the SAME model;
#                     the prompt was never the problem.
# Routing a 529 into the existing parse branch would be actively wrong:
# STRICT_RETRY_SYSTEM is only "You are a JSON generator", carrying no
# pick-selection instructions, so the model would be handed no task.
#
# ⚠️ A CREDIT-BALANCE FAILURE MUST STAY LOUD AND IMMEDIATE. "Your credit balance
# is too low" arrives as a 400 invalid_request_error, which is NOT in the
# retryable set below — retrying a billing or auth error only turns a clear
# failure into a slow one. Same rule as SupabaseBackend._read_with_retry, where
# an RLS 42501 is treated as permanent on purpose.

# Retryable NON-5xx statuses. 429 = rate limited, 408/409 = timeout/conflict.
TRANSIENT_STATUS = frozenset({408, 409, 429})

# Application-level backoff BETWEEN whole calls, on top of the SDK's own
# per-call retries. Deliberately short: the morning briefing is time-sensitive
# (7 AM ET) and the run already takes 2-12 min, so this adds at most ~60 s of
# sleep before falling back to a different model rather than stalling delivery.
API_RETRY_SLEEPS = (20.0, 40.0)


def is_transient_api_error(exc: BaseException) -> bool:
    """True when `exc` is an upstream blip that a later identical call may survive.

    Structural, not message-matching: the SDK hands us real status codes, so
    unlike the Supabase retry (which has no error taxonomy and must whitelist
    strings) this can decide from the type and the code.
    """
    # APITimeoutError subclasses APIConnectionError, so this covers both.
    if isinstance(exc, anthropic.APIConnectionError):
        return True
    # RateLimitError subclasses APIStatusError; the code check covers it too.
    if isinstance(exc, anthropic.APIStatusError):
        code = getattr(exc, "status_code", None)
        if isinstance(code, bool) or not isinstance(code, int):
            return False
        return code >= 500 or code in TRANSIENT_STATUS
    return False
