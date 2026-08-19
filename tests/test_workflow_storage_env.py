"""Every workflow that touches storage must point at the SAME backend as Render.

🔴 The split-brain of 2026-08-19. SUPABASE_URL/KEY were set on Render but not in
any workflow, so the web service wrote to Supabase while GitHub Actions kept
writing to the Gist. The same file then diverged in two stores — traffic_hours
held 807 hits in one and 154 in the other, and usage_counts.json existed ONLY in
Supabase while I reported the counter dead because I was reading the Gist.
"""
import os
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"

# Runs the suite with deliberately fake credentials so tests can never reach
# production storage. Real Supabase creds here would let 1200+ tests write to
# the live database.
NEVER_REAL = {"test.yml"}


def _workflows():
    return sorted(WF.glob("*.yml"))


def _touches_storage(text: str) -> bool:
    return "GH_GIST_TOKEN" in text


class TestStorageBackendIsConsistent:
    @pytest.mark.parametrize("wf", _workflows(), ids=lambda p: p.name)
    def test_storage_workflows_declare_supabase(self, wf):
        text = wf.read_text()
        if wf.name in NEVER_REAL or not _touches_storage(text):
            pytest.skip(f"{wf.name} does not use production storage")
        assert "SUPABASE_URL" in text and "SUPABASE_KEY" in text, (
            f"{wf.name} reads storage but declares no SUPABASE_* — it will use the "
            f"Gist while Render uses Supabase, and the two stores will diverge")

    def test_the_test_workflow_never_gets_real_credentials(self):
        """The one workflow that must NOT be wired."""
        text = (WF / "test.yml").read_text()
        assert "secrets.SUPABASE" not in text, \
            "test.yml would run the suite against the LIVE database"
        assert "fake_gist_token" in text, \
            "test.yml must keep fake storage creds so tests cannot reach production"

    def test_secrets_are_referenced_not_hardcoded(self):
        for wf in _workflows():
            for m in re.finditer(r"SUPABASE_(URL|KEY):\s*(\S+)", wf.read_text()):
                assert m.group(2).startswith("${{"), \
                    f"{wf.name} hardcodes a Supabase value instead of using secrets"

    def test_every_wired_workflow_still_has_the_gist_creds(self):
        """The Gist stays the rollback path — unsetting SUPABASE_* must fall back
        to it, which only works if the Gist creds are still present."""
        for wf in _workflows():
            text = wf.read_text()
            if "SUPABASE_URL" in text:
                assert "GH_GIST_TOKEN" in text and "GIST_ID" in text, \
                    f"{wf.name} has Supabase but no Gist fallback"
