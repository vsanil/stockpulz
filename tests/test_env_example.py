"""`.env.example` documents every environment variable the code reads.

Two jobs, and the second matters more than the first:

  1. Stay in SYNC — a new `os.environ.get("NEW_THING")` that nobody documents is
     how a deploy fails with an empty string and no error. The app reads 44 env
     vars; nothing else lists them.
  2. Never carry a VALUE. `.env` is git-ignored, `.env.example` is committed, so
     a real secret pasted into the example is a secret published to a public
     repo. The file is worth having only if that cannot happen.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(ROOT, ".env.example")

# A var may be documented as a live key (`NAME=`) or, for platform-injected and
# deliberately-unset ones, mentioned in a comment. Both count as documented.
_ASSIGNED = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.M)
# 🔴 A value is a non-space, non-# character immediately after '='. Matching
# `.+` instead counts the trailing `# comment` as a value and cries wolf on a
# perfectly clean file — which is exactly what happened when this was first run.
_HAS_VALUE = re.compile(r"^[A-Z][A-Z0-9_]*=[ \t]*[^ \t#\n]", re.M)

_READS = re.compile(r"""os\.(?:environ\.get|getenv)\(\s*["']([A-Z][A-Z0-9_]{2,})["']""")


def _example() -> str:
    with open(EXAMPLE) as f:
        return f.read()


def _code_vars() -> set:
    found = set()
    for folder in (ROOT, os.path.join(ROOT, "scripts")):
        for name in os.listdir(folder):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(folder, name), errors="ignore") as f:
                found |= set(_READS.findall(f.read()))
    return found


class TestNoSecretsAreCommitted:
    def test_every_key_is_empty(self):
        leaked = _HAS_VALUE.findall(_example())
        assert not leaked, f".env.example carries real values: {leaked}"

    def test_no_value_from_the_real_env_appears(self):
        """The direct check: nothing from .env may be present as a substring."""
        env = os.path.join(ROOT, ".env")
        if not os.path.exists(env):
            return                      # CI has no .env — nothing to leak
        example = _example()
        with open(env, errors="ignore") as f:
            for line in f:
                if "=" not in line or line.lstrip().startswith("#"):
                    continue
                key, _, value = line.partition("=")
                value = value.strip().strip('"').strip("'")
                if len(value) < 12:
                    continue            # too short to be a meaningful secret
                assert value not in example, f"{key.strip()} value leaked"

    def test_the_example_is_committed_and_the_real_env_is_not(self):
        with open(os.path.join(ROOT, ".gitignore")) as f:
            ignored = f.read().splitlines()
        assert ".env" in ignored, ".env must stay git-ignored"
        assert ".env.example" not in ignored, "the example is meant to be committed"


class TestItStaysInSync:
    def test_every_var_the_code_reads_is_documented(self):
        example = _example()
        missing = sorted(v for v in _code_vars() if not re.search(rf"\b{v}\b", example))
        assert not missing, (
            "these are read by the code but absent from .env.example — an "
            f"undocumented var deploys as an empty string with no error: {missing}")

    def test_the_scan_finds_a_realistic_number_of_vars(self):
        """Guards the guard: if the regex silently stopped matching, the sync
        test above would pass vacuously."""
        assert len(_code_vars()) >= 30, (
            f"only {len(_code_vars())} env reads found — the scan is broken, and "
            "a broken scan makes the sync test meaningless")

    def test_the_supabase_pair_is_documented_as_deliberately_unset(self):
        """Setting these switches ALL storage to Supabase. Once, it silently
        pointed local runs at a different app's project holding stale data."""
        example = _example()
        assert "SUPABASE_URL" in example and "SUPABASE_KEY" in example
        assert re.search(r"#\s*SUPABASE_URL=", example), \
            "SUPABASE_URL must be commented out, not presented as a key to fill in"
