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

### Syntax check before declaring done
- After editing any Python file, run `python -m py_compile <file>` mentally or literally — never skip this
- After editing index.html JS, check for unclosed braces, missing `async`, and undefined variable references
- A change is NOT done until syntax is verified

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
