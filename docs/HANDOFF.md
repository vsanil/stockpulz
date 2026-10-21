# Moving StockPulz to another machine — read this first

Written 2026-10-15 so the project can be picked up on a new laptop with nothing lost.

**What travels in git:** the code, `CLAUDE.md` (the engineering history, auto-loaded by
Claude), and `docs/claude-memory/` — a verbatim copy of the Claude memory directory, which
normally lives OUTSIDE the repo and would otherwise be left behind.

**What does NOT travel, and must not:** `.env`, `.claude/settings.local.json`, and the
local caches. See *Secrets* below. **This repository is PUBLIC.**

---

## 1. Memory and secrets live in a PRIVATE repo: `vsanil/claude-workspace`

**They are not in this repository any more, and that is deliberate.**

An earlier version of this handoff shipped `docs/claude-memory/` here. That was the wrong
shape: the memory is not project-scoped — `git_auth`, `sandbox_and_secrets`, `user_profile`
and the two `feedback_*` files are about the machine and the owner, and several carry
operational detail about OTHER apps. **This repository is PUBLIC.** So memory moved to a
private repo, which also picks up paywise and pricedrop instead of stranding them.

⚠️ **Honest limit:** removing those files from HEAD does not erase them from this repo's
HISTORY. Those commits stay fetchable unless the history is rewritten, which is a separate
and more disruptive decision.

### What to do on the new machine

```powershell
git clone https://github.com/vsanil/claude-workspace.git `
          "$env:USERPROFILE\Documents\Claude\claude-workspace"
```

Then follow that repo's `README.md`. In short: install `age`, put the private key in place
(it is the ONE thing that moves by hand — AirDrop or your password manager), decrypt this
project's `.env`, and point `autoMemoryDirectory` at
`.../claude-workspace/memory/stock-agent`.

🔴 **The age key is never in either repo.** It lives at
`~/Documents/Claude/claude-workspace.agekey`, mode 600. Without it the encrypted `env/`
files are unreadable — which is the point.

⚠️ **Do NOT sync `.claude/settings.local.json`.** Claude Code stores the literal text of
approved `Bash(curl …)` commands, so it can hold live secrets. Let it regenerate; the only
thing it needs is the memory path.

### Working across both machines

**Commit and push BOTH repos before you switch. Pull both after.** Memory is in a working
tree now, so an uncommitted memory change is an unsynced one.

**Chat transcripts never travel** — they are per-machine under `~/.claude/projects/`. You
resume the PROJECT (memory + `CLAUDE.md` + the open items), not the conversation. That is
exactly what the memory apparatus exists for.

## 2. Secrets — carry these by hand, never through git

`.env` holds live credentials and is gitignored. **Copy it across directly** (AirDrop, a USB
stick, or your password manager) — do not commit it, do not paste it into a chat.

`.env.example` is committed and is the authoritative list of every variable the app reads
(44 of them, names and comments only, every value blank). Use it as the checklist.

The ones without which nothing works:

| variable | what breaks without it |
|---|---|
| `ANTHROPIC_API_KEY` | no picks at all — Claude selects them |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | no delivery, no alerts |
| `SUPABASE_URL` / `SUPABASE_KEY` | storage falls back to the Gist (the rollback copy) |
| `GH_GIST_TOKEN` / `GIST_ID` | the ledger and `picks.json` are unreachable |
| `FINNHUB_API_KEY`, `ALPACA_*`, `POLYGON_API_KEY` | screener inputs degrade |
| `RENDER_API_KEY`, `CRONJOB_API_KEY` | ops visibility only |

⚠️ Use the `sb_secret_` Supabase key, never `sb_publishable_` — the publishable key sees
`user_records` with **0 rows** rather than erroring, so storage looks healthy while reading
empty and writing nothing.

🔴 **`.claude/settings.local.json` stores the LITERAL command text of approved `Bash(curl …)`
calls**, so approving one curl carrying a secret writes that secret to disk permanently. Do
not commit it, and approve `-H "Authorization: Bearer $VAR"` rather than a literal token.

## 3. Tooling on the new machine (Windows)

Install **Python 3.11**, **Node** (the JS checker shells out to it), **Git**, and the
**GitHub CLI**. Then, from the repo root in PowerShell:

```powershell
pip install -r requirements.txt pytest   # pytest is NOT a runtime dep — install it explicitly
python -m pytest tests/ -q               # expect ~2507 passed, 8 skipped
python scripts\check_js.py               # after ANY admin-page or mini-app edit — needs node
gh auth status                           # must show: account vsanil, Active account: true
```

⚠️ **`python`, not `python3`.** The `python3` alias does not exist on a normal Windows
install and will open the Microsoft Store instead of failing usefully.

**Two Windows gotchas, already handled (2026-10-21):** set `PYTHONUTF8=1` as a user environment variable (Windows Python defaults to cp1252 and some tests and scripts break), and never use `strftime("%-d")` — it is Linux-only; call `datefmt.strftime_portable`. See the matching CLAUDE.md entry.

**What does NOT work natively**, and does not need to: `scripts/precheck.sh` and
`scripts/dev.sh` are bash. Run them under **Git Bash** (ships with Git for Windows) or WSL if
you want them. Nothing in the daily loop depends on them — the real work runs on GitHub
Actions and Render, which are Linux regardless of your laptop.

**Line endings are pinned.** `.gitattributes` forces LF for all text, because this repo is
full of source SCANS — guards asserting on exact markup, declarations and workflow text —
plus a node pass over the JS. Without it a Windows checkout gets CRLF and a test can fail on
your machine while passing on CI, for an invisible reason. A fresh clone gets this right
automatically; if you cloned BEFORE this commit, run `git add --renormalize .` once.

🚫 **Never push as `vsanil_costco`** (the office account). `gh` hands git whichever account is
ACTIVE and it has flipped on its own at least twice. Check before every push; the symptoms
never say "wrong account" — they say `Repository not found`, `denied to vsanil_costco`, or
`403 admin rights`.

Use this form for anything touching the network — the empty first value is load-bearing,
because `credential.helper` is multi-valued and a bare `-c` would still try the Windows
Credential Manager:

```powershell
git -c credential.helper= -c credential.helper='!gh auth git-credential' push origin main
```

### Mac-only notes in CLAUDE.md that NO LONGER APPLY

`CLAUDE.md` is five months of history written on a Mac. These entries are about that machine,
not about the project — do not chase them on Windows:

- **"yfinance fails TLS on this Mac (curl_cffi)"** — a TLS-intercepting proxy on the old
  laptop. A local `DEAD` row in `input_audit` meant nothing there. On Windows, re-measure
  before assuming either way; CI remains the authority.
- **The macOS Seatbelt sandbox** and its deny rules (`~/.ssh`, `~/.config/gh`, `.env`) — a
  Claude Code feature on that machine. `gh` failing "because the sandbox denies
  `~/.config/gh`" is not a thing here; on Windows the config lives in
  `%APPDATA%\GitHub CLI\`.
- **`preview_start` cannot run the app under `~/Documents`** — a macOS Files-and-Folders
  permission issue, irrelevant here.
- **BSD vs GNU tools** — several recorded one-liners use BSD `date -j -f` and `sed -i ''`,
  which are macOS-only syntax. Rewrite rather than copy-paste.

## 4. Where the project actually stands

**Live and healthy.** Picks deliver every trading day at 7 AM ET. Flask/Gunicorn on Render
FREE + GitHub Actions (7 GB) + Supabase rows (the Gist is the rollback copy). ~2 real users.

**🏁 THE TOURNAMENT is the program**, owner-approved 2026-09-19, and it has a stopping rule.
Four arms (`breakout`, `pullback`, `quality`, `spy_hold`) trade a $10k paper book each
through one shared trader, with 0% pick overlap by construction. Only the live arm calls
Claude (Option B).

- **75 ledger rows per arm** as of 2026-10-10. First picks mature 2026-10-21.
- **The read lands ~2026-10-28** — `_MIN_N=30` matured picks per arm over a 30-day horizon.
  The equity-curve read (30 trading days) is ~2026-11-02.
- 🛑 **After that read the program ENDS**, with one of two outcomes: a strategy earned the
  recommendation, or none did and the honest product is the index plus levels and alerts.
  **There is no Phase 4.** Until the read: no UI polish, no coverage passes, no new monitors,
  and nothing tuned on a partial curve.

**The product claim is the open question, not the engineering.** All four engine stages have
been measured and none shows a detectable edge (ST −1.6 n=510, LT −2.2 n=90, pool vs SPY
−3.6, selection −4.74 n=46/41). *"These picks beat the market"* is not supportable. What is:
*"daily picks with real entry/stop/target levels, position sizing, and alerts that fire —
measured against SPY."*

**The owner is on a break.** They asked to stop building. The standing instruction is: **hold
findings until asked, and interrupt only for something a user would actually feel.** A false
canary alarm or a lost monitoring stamp does not qualify; picks not arriving does.

### The one thing that needs a human

**Fund the Anthropic balance.** It is the only dependency that can silently stop the product,
and it has four times (09-05, 09-07, 09-17, 10-02), each costing a full day of picks to both
real users. Auto-reload is OFF by the owner's dated decision (2026-09-18) — **surface it, do
not re-argue it**. The $20/$35 alerts watch SPEND, not balance, so nothing warns you. Measured
~$13/week, so roughly **$200–250 covers three months**.

### Known and deliberately NOT fixed

- **`config_manager.update_config` is a racy whole-file read-modify-write**, and 8 UTC cron
  slots have two jobs firing in the same minute (`price_alerts` is in 7 of them). So
  `cron_last_*` stamps get erased and the canary occasionally reports a false
  `TRIGGER(S) DEAD`. The same path also writes `last_morning_run`, `allowed_users` and
  `banned_users`. Real, small footprint, **deferred by agreement until after the read.**
- **Cold start ~45–55 s** on Render FREE. Both keep-warm jobs are DISABLED pending a launch
  day. The mechanism is solved (dispatch, not schedule) — see CLAUDE.md.
- **Options section withheld** — the feed is blocked from GitHub Actions IPs. Self-healing by
  design: it returns the moment strikes populate, with no code change.
- **Credential rotation WAIVED** by the owner on 2026-09-25. Do not re-raise on a schedule.

## 5. The session ritual

1. Read `analysis/ENGINE_FINDINGS.md`. If `## Open` > 0, present those findings with evidence
   and the file to change, then ask which to implement. If 0, say so in one line and move on.
   **Never act on the synthetic bot's win rate.**
2. Check its regenerated timestamp. More than ~36 h old means the nightly job did not run —
   say the agenda may be stale rather than presenting it as current.
   ⚠️ The markdown lags the store by up to a day; **if they disagree, the store wins.**
3. Read `docs/claude-memory/open_items.md` and **lead with what is DUE**.
4. ⚠️ **`git pull` before trusting any local file.** On 2026-10-11 a local checkout was 8 days
   behind and the agenda looked stale when it was not.

## 6. The traps that have cost the most time

Full detail is in `CLAUDE.md` and `docs/claude-memory/feedback_verification_discipline.md`.
The short list:

- **Read the store the WRITER used.** `save_picks`/`load_picks` bypass the backend and hit the
  Gist directly. User-keyed files (`pending_users`, `price_alerts`, `user_configs`,
  `user_trades`, `user_paper`, `backtest_trades`) live as ROWS in `user_records`;
  `read_strict()` queries `documents` and finds nothing. That exact bug made a monitor blind
  for a month while a real person waited 14 days for access.
- **A stub narrower than production certifies bugs.** Four recorded instances. Model every
  method the caller might reach.
- **An assertion that matches text still present elsewhere is not a guard.** Anchor on
  structure (AST, declarations), never on prose or byte offsets. Mutation-test every guard,
  assert the MUTANT is present, and confirm it parses.
- **A hardcoded date in a test fixture feeding a rolling window is a time bomb.** One went off
  on day 31 and took the suite red with no code change. Stamp on the clock the code reads and
  FORCE the condition.
- **`yaml.safe_load` silently keeps the LAST of a duplicate key; GitHub rejects the file.**
  Validate workflows with a duplicate-refusing loader.
- **Committed ≠ deployed.** Compare `/health`'s `commit` to the last commit WITHOUT the
  skip-render tag.
- **Never infer an absence from a command whose failure you hid.** Run the control first.
