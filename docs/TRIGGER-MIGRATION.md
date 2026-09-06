# StockPulz: move the 17 scheduled triggers off the sleeping app

**Status:** ready to execute. Written 2026-09-05.
**Your part:** create one GitHub token, edit 17 cron-job.org jobs. ~30 minutes.
**Not your part:** no code deploy required for the migration itself.

---

## 1. What is broken, and why the obvious fixes don't work

Since the Render downgrade to Free on **2026-09-02 22:04**, StockPulz sleeps after ~15 idle
minutes. Roughly **12 of its 17 cron-job.org jobs fail every day**, and the app's own canary
reports the consequence:

```
FAIL  picks.fresh          _saved_date=2026-09-03, expected >= 2026-09-04
FAIL  delivery.morning     last_morning_run=2026-09-03
FAIL  endpoint.health      read timed out
```

**No picks and no morning delivery since 2026-09-03.**

The chain is: cron-job.org → `/trigger/<mode>` on Render → app dispatches `daily_run.yml` to
GitHub Actions → Actions does the work. Only the middle link sleeps.

Three fixes were considered and two were ruled out by measurement:

| candidate | verdict | evidence |
|---|---|---|
| Cut the cold start under cron-job.org's 30 s ceiling | ❌ | cold start measured **54.9 s**. Needs a >50% cut, and one new import puts it back over |
| Sacrificial "wake" job 2 min before each real job | ❌ | cron-job.org's requests are **refused in ~500 ms** (342/519/561 ms observed), so they never hold a connection and never trigger a boot. `curl` behaves differently — do not generalise from it |
| Move triggers to GitHub Actions `schedule:` | ❌ | GitHub throttles scheduled runs **1.6–6 h late** on this repo (`canary` "30 12" ran 16:29, +4.0 h). Fatal for market-timed jobs |
| **cron-job.org → GitHub API directly** | ✅ | punctual to the second, zero Render hours, app no longer on the critical path |

> ⚠️ **Do NOT re-enable `keepwarm.yml`.** Its comments instruct you to uncomment the `*/10`
> cron "if the service is ever downgraded to Free" — which happened on Sept 2. That advice is
> stale and the file itself warns two lines earlier that it costs **~744 h/mo, 99% of the
> 750-hour cap shared by all four apps**. Leave it commented out.

---

## 2. What changes

Only **what each job calls**. Every job keeps its existing **schedule and timezone untouched** —
do not retime anything.

**Before** (per job):
```
GET https://stock-agent-enqx.onrender.com/trigger/<mode>?secret=<CRON_SECRET>
```

**After** (per job):
```
Method : POST
URL    : https://api.github.com/repos/vsanil/stockpulz/actions/workflows/daily_run.yml/dispatches
Headers: Authorization: Bearer <PAT>
         Accept: application/vnd.github+json
         Content-Type: application/json
         User-Agent: cron-job.org          <- GitHub rejects requests with no User-Agent
Body   : {"ref":"main","inputs":{"run_mode":"<mode>","force":"false","owner_only":"","mock_data":"false"}}
```

This is byte-for-byte what `webhook.py` already sends (see `trigger_mode`, ~line 344). Success
is **HTTP 204 No Content**, not 200 — see §6.

---

## 3. Create the token

GitHub → Settings → Developer settings → **Fine-grained personal access tokens** → Generate new.

```
Resource owner : vsanil
Repository     : ONLY vsanil/stockpulz          <- not "all repositories"
Permissions    : Actions            → Read and write
                 Metadata           → Read-only  (mandatory, added automatically)
Expiration     : NEVER (owner's choice, confirmed 2026-09-06)
```

Nothing else. `actions:write` permits dispatching workflow runs — it does **not** allow pushing
code.

✅ **THE LIVE TOKEN DOES NOT EXPIRE** — owner confirmed 2026-09-06. So there is no expiry
cliff and no reminder to set; the "90 days" above describes GitHub's default, not what was
created.
🚨 **Do NOT read `2026-12-04` anywhere as a fact about this token.** An earlier version of this
section used it as an EXAMPLE of how to label a job, and a later session read that illustration
as a recorded expiry date and ranked "the PAT lapses in December" as the system's top risk. It
was never true. Same error class as every other derived-value-reported-as-measured in this repo.
⚠️ The real trade, stated once and not re-litigated: a non-expiring token removes the scheduled
outage and replaces it with a permanent credential in two places (17 cron-job.org jobs + the
Render env) that nothing will ever force you to rotate. Owner's call, made knowingly. If it ever
needs revoking, that is 17 edits, so budget for it rather than being surprised by it.

ℹ️ Storing this in cron-job.org is a real exposure, and worth weighing honestly: anyone with
that token can trigger workflow runs in the repo. It is **not** a new class of secret — the app
already holds the same token in its Render env — but it is now in a second place. The repo is
public, so the workflows it can run are already visible.

---

## 4. The 17 jobs

Every one changes identically except `run_mode`. **Keep each job's own schedule and timezone.**

| # | cron-job.org job | `run_mode` |
|---|---|---|
| 1 | StockPulz-morning | `morning` |
| 2 | StockPulz-premarket | `premarket` |
| 3 | StockPulz-watchdog | `watchdog` |
| 4 | StockPulz-vix_check | `vix_check` |
| 5 | StockPulz-confirmation | `confirmation` |
| 6 | StockPulz-digest | `digest` |
| 7 | StockPulz-news_check | `news_check` |
| 8 | StockPulz-midday_check | `midday_check` |
| 9 | StockPulz-close_check | `close_check` |
| 10 | StockPulz-price_alerts | `price_alerts` |
| 11 | StockPulz-pre_earnings | `pre_earnings` |
| 12 | StockPulz-eod_summary | `eod_summary` |
| 13 | StockPulz-macro_alert | `macro_alert` |
| 14 | StockPulz-friday_wrap | `friday_wrap` |
| 15 | StockPulz-prescreener | `prescreener` |
| 16 | StockPulz-weekly | `weekly` |
| 17 | StockPulz-week_ahead | `week_ahead` |

Leave `StockPulz-keepalive` **Inactive**.

🔎 Two modes the app supports that nothing ever calls: **`monthly_commentary`** and
**`tax_harvest`**. Not part of this migration, but worth deciding on — `tax_harvest` is
seasonal and year-end is approaching.

---

## 5. Do ONE first, and prove it

Do not convert all 17 in one sitting.

1. Convert **`StockPulz-watchdog`** only. Low stakes, and it exercises the whole path.
2. In cron-job.org press **TEST RUN** — it fires immediately, no waiting for the schedule.
3. Expect **204**. Then check `gh run list --repo vsanil/stockpulz --workflow=daily_run.yml
   --limit 3` — a new run should appear within seconds, `event: workflow_dispatch`.
4. Only when that works, convert the other 16.

If the test run returns 401 the token is wrong or expired; 403 means the permission is missing
or not scoped to this repo; 404 usually means the token cannot see the repo at all.

---

## 6. Two settings that will bite

**`204` is success.** cron-job.org treats non-2xx as failure, and 204 *is* 2xx, so the default
is fine. But if any job has a "treat only 200 as success" style setting, it will report red on
every successful dispatch.

**Turn OFF "Save responses in job history"** if it is on — the request body contains no secret,
but the Authorization header may be captured.

**Keep the failure alarms on** (already enabled on all 17 as of 2026-09-04, notify after 1
failure, plus "will be disabled because of too many failures"). Those are the only warning you
get, and cron-job.org silently disables a job after ~26 consecutive failures with nothing to
re-enable it.

---

## 7. The duplicate guard you are giving up

`trigger_mode` currently keeps a per-mode marker (`cron_last_<mode>`) and skips a mode that
already ran today (ET). Dispatching directly bypasses it, so a double-fire would run the mode
twice — and for modes that message users, that means **duplicate Telegram messages**.

Cheapest replacement, in `daily_run.yml`:

```yaml
concurrency:
  group: daily-run-${{ github.event.inputs.run_mode }}
  cancel-in-progress: false
```

That prevents two runs of the *same mode* overlapping, which is the harmful case. It does not
prevent a repeat hours later — if you want that, the workflow needs its own same-day check.

Start with the concurrency group. Add more only if you actually observe a duplicate.

---

## 8. Verifying it worked

The morning after the full cutover:

```bash
gh run list --repo vsanil/stockpulz --workflow=daily_run.yml --limit 20 \
  --json createdAt,conclusion,event
gh run list --repo vsanil/stockpulz --workflow=canary.yml --limit 3 \
  --json createdAt,conclusion
```

The canary is the real verdict — it independently checks the things that are broken today:

```
picks.fresh          should go PASS
delivery.morning     should go PASS
endpoint.health      will still FAIL — the app is asleep, and that is now CORRECT
```

⚠️ `endpoint.health` failing is expected after this change and is no longer a problem: nothing
scheduled depends on the app being awake. Consider changing that check to wake-and-measure, or
dropping it, so the canary stops crying wolf. Also note `storage.surfaces` currently reports
**PASS** with the text "NOT VERIFIED this run — ReadTimeout" — a check that passes when it
could not verify is worse than one that fails.

---

## 9. What this does to the free-tier bill

StockPulz's Render hours **go down**, because nothing scheduled wakes it any more.

Remaining wake sources: `canary.yml` and `daily_run.yml` still curl the app (1 + 2 crons/day),
plus **Auto-Deploy, which is `On Commit` on this service** — the only one of the four with it
on. Every deploy wakes the instance ~0.28 h; there were five deploys on Sept 3 alone. That
becomes the largest controllable line.

🚨 **A browser tab left open on `/admin` polls `/admin/data` every 60 seconds and pins the
service awake 24/7 (~720 h/mo against a 750 h cap shared by four apps).** This was found live
on 2026-09-04 and is the single easiest way to blow the budget. Close the tab when done.

---

## 10. Rolling back

Change the job's Method back to `GET`, URL back to
`https://stock-agent-enqx.onrender.com/trigger/<mode>?secret=<CRON_SECRET>`, and clear the
headers and body. Nothing else was touched — no code was deployed, no schedule changed — so
rollback is per-job and instant.
