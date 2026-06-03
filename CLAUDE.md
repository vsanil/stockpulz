## Self-evolving rules

- After every session, if a new bug pattern, recurring mistake, or important constraint was discovered, add it to this file
- If a rule was repeatedly violated despite being listed, rewrite it more explicitly
- If a new API, library, or architectural decision was made, document it here so future sessions start with full context
- This file is the source of truth for how this project should be built — keep it current

## graphify

This project has a graphify knowledge graph at graphify-out/.

Rules:
- Before answering architecture or codebase questions, read graphify-out/GRAPH_REPORT.md for god nodes and community structure
- If graphify-out/wiki/index.md exists, navigate it instead of reading raw files
- For cross-module "how does X relate to Y" questions, prefer `graphify query "<question>"`, `graphify path "<A>" "<B>"`, or `graphify explain "<concept>"` over grep — these traverse the graph's EXTRACTED + INFERRED edges instead of scanning files
- After modifying code files in this session, run `graphify update .` to keep the graph current (AST-only, no API cost)

## Development rules

### Scope — always apply changes everywhere
- When making any change, apply it in ALL relevant places — not just the most obvious one
- For any frontend change, check webhook.py for the matching API endpoint and update it too
- For any API change, check miniapp/index.html for all call sites and update them too
- For any data a page needs on load, ensure it is included in the API response
- After fixing a bug, scan for the same pattern in adjacent code paths and fix those too

### Auto-save / UX
- All form inputs must auto-save on blur — never require a manual Save button as the only save path
- Never call `tg.enableClosingConfirmation()` — always use `tg.disableClosingConfirmation()` + `window.onbeforeunload = null`

### State persistence
- Never use sessionStorage as the sole source of truth for user state — sessionStorage is wiped on app close
- For state that must survive app restarts, piggyback it onto an existing API response (e.g. `_meta.bought_tickers` in picks)
- Live price always trumps stale stored price — use `get_live_price()` for any current price display

### Timing / schedule references
- Morning run cron: `0 11 * * 1-5` = 7:00 AM ET
- All user-facing strings must say 7 AM ET — never 8 AM ET
- **Primary scheduler**: cron-job.org (free) → POST `https://<render-url>/trigger/morning?secret=CRON_SECRET` at 11:00 AM UTC on weekdays. Fires within seconds, reliable.
- **Backup scheduler**: GitHub Actions (`.github/workflows/daily_run.yml`) — still runs all other jobs (confirmation, EOD, alerts, etc.). Morning cron kept as fallback but duplicate-run guard in `/trigger/morning` prevents double-sending.
- GitHub Actions free tier can delay cron jobs by hours during peak load — this is why cron-job.org was added for the morning run
- `CRON_SECRET` env var must be set on Render — include as `?secret=CRON_SECRET` in cron-job.org URL
- Late-delivery guard was removed — picks now always send since cron-job.org fires on time

### Performance stats — median not mean
- `get_recent_stats()` in `performance_tracker.py` now returns both `avg_return` and `median_return`
- The morning message perf bar uses `median_return` (outlier-resistant) — a single large crypto return can make `avg_return` misleading (e.g. +441%)
- All other callers (cmd_market.py, cmd_trades.py, cmd_admin.py) still use `avg_return` — do not change those without checking the display context

### Commit discipline
- Always `git add` ALL changed files together — never leave related changes uncommitted
- Provide the full git command (add + commit + push) at the end of every change set

### Read before editing
- Always read the full function/block before modifying it — never edit blindly from grep results alone
- When a bug is in function X, also read all callers of X before deciding the fix
- When adding a field to an API response, read the frontend render function to confirm it actually uses it

### Full data flow verification
- For every change, trace the complete chain: user action → JS handler → API call → Python endpoint → storage → response → JS render
- If any link in that chain is missing or broken, fix the whole chain — not just the link you noticed
- When a feature "doesn't work", check all 5 layers before concluding where the bug is

### Error handling — every async path needs it
- Every `api(...)` call in the frontend must have a try/catch with visible user feedback (toast or inline message)
- Every Python endpoint must handle exceptions and return a meaningful error response, not a 500
- Loading states: every async section must show a spinner/skeleton while fetching and hide it on completion or error

### UI/UX standards
- Every action must give immediate feedback — button state change, haptic, or toast within 200ms
- Never leave a button enabled while its async action is in-flight — always disable + show loading text
- Empty states must be handled: if a list/section has no data, show a helpful message, not a blank space
- All overlays/sheets must close cleanly — reset all inputs, clear error states, re-enable buttons
- Mobile-first: all UI must work on a narrow 375px viewport with touch targets ≥ 44px

### Cache invalidation
- When server data changes (position updated, pick bought, watchlist changed), always call `_invalidatePortfolioCache()` or equivalent and set `loadedTabs[tab] = false` for affected tabs
- Never show stale data after a write — reload the affected tab/section immediately after a successful save

### pandas 2.x compatibility — always use StringIO
- `pd.read_html(string)` and `pd.read_csv(string)` no longer accept raw HTML/CSV strings in pandas 2.x — they try to open the string as a file path and raise `[Errno 2] No such file or directory`
- Always wrap response text: `pd.read_html(StringIO(resp.text))` / `pd.read_csv(StringIO(resp.text))`
- When touching any file that calls `pd.read_html` or `pd.read_csv` with response content, grep all call sites and verify they all use `StringIO`
- Every module that makes external HTTP calls and parses the response must have at least one mocked response test

### Syntax check before declaring done
- After editing any Python file, run `python -m py_compile <file>` mentally or literally — never skip this
- After editing index.html JS, run `python scripts/check_js.py` (or `pytest tests/test_frontend_js.py`) — no exceptions
  - This catches: Node.js syntax errors, TDZ bugs (let/const used in IIFEs before declaration), blocked Telegram APIs
  - The TDZ bug class is SILENT at parse time — `node --check` does not catch it, only this checker does
  - Root cause: a `let`/`const` variable assigned inside a top-level IIFE before its declaration line crashes the entire script at runtime (ReferenceError = blank page). Always move such assignments inside the `_appReady` listener
- A change is NOT done until syntax is verified

### Test suite — mandatory before every commit
- Run `python -m pytest tests/ -q` before every commit, no exceptions
- A commit is NOT done until all tests pass
- When fixing a bug in a critical path (agent delivery, picks sending, etc.), write a regression test FIRST that reproduces the failure, then fix it
- When adding a function that handles user-facing delivery or state, add a test that asserts the output lands in the right place
- Scope isolation bugs (function referencing caller-scope variables) are NOT caught by py_compile — only tests catch them

### Tests must travel with every code change
- Every code change must be accompanied by a test update in the same commit — no exceptions
- Adding a new function → add tests for the happy path, error path, and at least one edge case
- Changing a function signature or behaviour → update every test that calls it
- Fixing a bug → add a regression test that would have caught it BEFORE applying the fix
- Deleting a function → delete its tests in the same commit
- If a change touches config_manager, formatters, trade_logger, telegram_api, or position_sizer,
  open the corresponding test file and verify coverage still reflects the updated behaviour
- Never commit "I'll add tests later" — later never comes

### Proactive bug hunting
- After fixing any bug, grep for the same pattern across the whole codebase and fix all occurrences
- When a feature is reported broken, check if the same feature has a parallel implementation (e.g. Telegram bot command + mini app UI) and fix both
- When adding a new field/feature, check if any existing tests need updating

### Always ask before coding
- Before making any code change, present the plan and wait for approval
- State exactly which files will change and why — never just start editing
- If the fix requires touching more than 2 files, list all of them upfront

### Verification before declaring done
- After every change, mentally walk through the complete user flow end-to-end
- Explicitly state what was checked: "verified: X works, Y works, Z edge case handled"
- Never say "done" without confirming syntax is clean and the chain is unbroken

### Feature parity — bot + mini app
- Any feature that exists in both the Telegram bot commands AND the mini app must be kept in sync
- When fixing a bug in the mini app, check if the same logic exists in bot_commands.py and fix it there too
- When adding a bot command, check if the mini app needs a matching UI element

### Telegram WebView constraints
- `window.confirm()` and `window.alert()` are blocked — always use `tg.showPopup()` for confirmations
- `window.open()` may be blocked — use `tg.openLink()` for external URLs
- Keyboard appearance changes viewport height — UI must not break when soft keyboard opens

### Never introduce regressions
- Before changing shared utility functions (api(), showToast(), openOverlay(), etc.), grep all call sites first
- When refactoring, verify every existing call site still works with the new signature
- If changing a Python function signature, update all callers in the same commit

### Completeness check before closing a task
- Does the feature work on first load (cold start)?
- Does it work after app close and reopen (state persistence)?
- Does it handle the empty state (no data)?
- Does it handle the error state (API failure)?
- Does it work if the user does it twice (idempotency)?

### Loyal users first — every change must be zero-impact on UX
- This app has active users who trust it for real trading decisions. Any change that degrades their experience — slower loads, spinners where there were none, stale data, layout shifts — is unacceptable regardless of how clean the code is.
- Before any backend change: verify the API response shape is identical. Never rename, remove, or reorder fields that the frontend consumes.
- Before any frontend change: ask "does this cause a flash, a re-render, or a spinner that didn't exist before?" If yes, find a way to make it silent (in-place DOM update, background fetch, stale-while-revalidate).
- Performance improvements must not compromise data freshness. Users watching live positions expect prices to be real-time. A cache that makes the tab feel faster but shows a stale price is worse than a slow load with a correct price.
- Decouple structure from data: load stored data (positions, picks, watchlist) instantly, then overlay live prices asynchronously. Never block the render on a live API call.
- Auto-refresh timers must be silent: no spinners, no full re-renders, no innerHTML swaps that reset scroll position. Use targeted DOM updates (getElementById, textContent, style.width).
- If a background job (refresh, sync, price update) fails, swallow the error silently. Never surface a background failure to the user as an error state.
- When in doubt: do less. A conservative change that improves load time by 20% with zero UX risk beats an aggressive change that saves 60% but has a 5% chance of a visible flash.
