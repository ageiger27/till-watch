# TillWatch

Flags Burger King restaurants that **opened late** or **closed early**, based
on till (cash drawer) activity in PAR Data Central — and stays silent when
everything is fine.

Every morning it pulls the prior business day's **"Tills: Earliest Open /
Latest Close"** report, compares each store's first till open and last till
close against its posted hours, and emails the company's list **only if
something was flagged**. A scrape failure sends a separate FAILED email, so
silence always means "all clear", never "the bot died".

Multi-franchisee: each company (must be a PAR Ops customer) is one JSON file
in `companies/` plus two GitHub secrets.

## How it works

1. GitHub Actions cron fires daily at 11:00 UTC (~4 AM PT; GH cron can run
   late, which is fine for a by-breakfast report)
2. `src/portal.py` logs into the company's portal with Playwright, then
   replicates the report's own API calls (`prepare` → `openReport` →
   `getBuildStatus` → `getPage`) — no viewer scraping, no Excel export. The
   report pages come back as JSON "bricks" that we reconstruct into rows.
3. `src/analyze.py` aggregates drawer rows to per-store earliest open / latest
   close (times before 4 AM count as past-midnight of the same business day)
   and compares against the store's hours for that weekday, with a grace
   period (default 15 min). Stores missing from the report entirely are
   flagged **NO TILL DATA** — the worst case.
4. `src/emailer.py` sends the exception digest via Gmail SMTP. No flags → no
   email (the all-clear is only in the Actions log).

## Flags

| Flag | Meaning |
|---|---|
| LATE OPEN | First till opened more than `grace_minutes` after posted open (default 15) |
| EARLY CLOSE | Last till closed more than `close_grace_minutes` before posted close (default 0 — tills normally close at/after the posted close) |
| NO TILL DATA | Store absent from the report — likely never opened |
| MANAGER LATE IN | First manager (HGM/AM) clock-in later than posted open minus `manager_open_lead_minutes` (default 0: a manager must be punched in by open) — catches mornings the tills can't, since a store can't open without a manager |

### Manager attribution

When a LATE OPEN or EARLY CLOSE is flagged, the bot also pulls the
**"Payroll - Daily Time Card Review w/ Totals"** report (same API flow) and
names the manager on duty: the first clock-in (opening) or last clock-out
(closing) among employees whose job title matches `manager_titles`
(default: Hourly General Manager / Assistant Mgr). It appears as a line under
the flag in the email. Attribution is best-effort — if the timecard pull
fails, the alert still goes out without it, and the operator
(`FAILURE_RECIPIENT`) gets a "timecards FAILED" heads-up.

The timecard report is pulled every night, group-wide first (one build).
Since early Sep 2026 that build often exceeds the 5-minute cap, so on a
timeout the bot falls back to one pull per store using each store's
`unit_id` (~25 s each). The `unit_id` is the portal's `@UnitID` for that
unit — pick the store in the report's Filters panel and read it from the
URL.

### Backfilling timecards

If a night's timecard pull failed, re-run it for that day (print only, no
email):

```powershell
python main.py --timecards 2026-09-09                # manager punches, all stores
python main.py --timecards 2026-09-09 --store 3023   # every shift at one store
```

## Onboarding a franchisee

1. Create `companies/<name>.json` (copy `geiger-management.json`):
   - `portal` — their portal slug (e.g. `portal1234`); must be a PAR Ops customer on dc01.rmdatacentral.com
   - `report_id` / `group_id` — open the "Tills: Earliest Open / Latest Close"
     report in their portal; the report id is in the URL
     (`/feed/allreports/reportdetail/<report_id>`), and the group id appears in
     the `@GroupID` parameter after applying filters (or capture the
     `prepare` request in DevTools)
   - `timecard_report_id` — same idea with "Payroll - Daily Time Card Review
     w/ Totals" (5-Payroll category); omit it to skip manager attribution.
     Their portal login must have payroll viewing rights.
   - `credentials_env_prefix` — e.g. `SMITH`
   - `recipients`, `grace_minutes`, and per-store `hours` (seed from their
     Google listings, then have the franchisee confirm)
2. Add repo secrets `<PREFIX>_DC01_USERNAME` / `<PREFIX>_DC01_PASSWORD` and
   matching `env:` lines in `.github/workflows/daily.yml`.

### Store hours config

```json
"hours": {
  "mon": {"open": "06:00", "close": "23:00"},
  "fri": {"open": "06:00", "close": "00:00"},   // close past midnight is fine
  "sun": "closed",                               // or null — skipped that day
  "sat": "24h"                                   // 24-hour: never flagged
}
```

### Live hours from bk.com

With `"live_hours": true` (and `lat`/`lng` per store), the bot pulls each
store's current hours from the bk.com locator backend (RBI GraphQL, no API
key) every night and uses those instead of config — so holiday hours or a
hood-cleaning early close entered in the BK/RBI system are honored
automatically. Config hours are the fallback for any store the lookup
misses, and the email footer says which source was used.

One timing caveat: the check runs the **morning after** the business day
(~4 AM PT). An hours adjustment must still be posted at that moment to
count — don't revert a holiday's special hours until after the morning run.

### Mailing lists

Company-level `recipients` get the full digest (every flag). `lists` route
subsets: each list names an explicit set of stores and its recipients get an
email with only those stores' flags (list name in the subject). Stores may
appear on any number of lists; a list with no flags that day gets no email.

```json
"lists": [
  {"name": "California", "recipients": ["dm@..."], "stores": ["2319", "2671"]},
  {"name": "Idaho District", "recipients": ["dm2@..."], "stores": ["9787"]}
]
```

With `"send_store_alerts": true`, each flagged store's own `email` also gets
an alert with just that store's flags, so the GM hears the same morning.

A recipient can also be a Google Group address — manage membership in
Workspace admin without touching this config.

## Secrets (GitHub → Settings → Secrets → Actions)

| Secret | Value |
|---|---|
| `GEIGER_DC01_USERNAME` / `GEIGER_DC01_PASSWORD` | PAR portal login |
| `GMAIL_USER` / `GMAIL_APP_PASSWORD` | Sending account (App Password, not the real password) |
| `FAILURE_RECIPIENT` | Who gets FAILED emails (the operator) |

## Local testing

```powershell
$env:GEIGER_DC01_USERNAME = "..."
$env:GEIGER_DC01_PASSWORD = "..."
$env:DRY_RUN = "true"          # print emails instead of sending
python main.py
```

Or run the workflow manually: Actions → Daily TillWatch → Run workflow with
`dry_run = true`.
