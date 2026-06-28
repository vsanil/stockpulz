"""
llm_client.py — Shared Anthropic client singleton.

Import _get_client() from here instead of creating per-module instances.
"""
from __future__ import annotations

import os
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
