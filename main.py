#!/usr/bin/env python3
"""TillWatch — flags stores that opened late or closed early, based on the
'Tills: Earliest Open / Latest Close' report in PAR Data Central.

For each company in companies/: pull yesterday's report, compare each store's
first till open / last till close against its posted hours (with a grace
period), and email the company's list ONLY if something was flagged.

Safeguard: the Earliest/Latest report has been wrong (2026-09-22, #13959:
it showed a 6:08 PM close while the Till History report had a drawer open
until 12:02 AM). When the company config has a till_history_report_id, Till
History is pulled too and each store's window is widened to the earliest
open / latest close on either report before anything is flagged.
"""

import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.analyze import (WEEKDAY_KEYS, _parse_report_time, attach_managers,
                         condense_flags, cross_check_units, diff_flags,
                         evaluate_company, evaluate_manager_arrivals,
                         fmt_minutes, aggregate_units, store_number)
from src.emailer import (DRY_RUN, GMAIL_USER, GMAIL_APP_PASSWORD,
                         build_cross_check_email,
                         build_cross_check_failure_email,
                         build_failure_email, build_flags_email,
                         build_timecard_failure_email, send_email)
from src.hours import fetch_live_hours
from src.portal import PortalSession

COMPANIES_DIR = Path(__file__).parent / "companies"
FAILURE_RECIPIENT = os.environ.get("FAILURE_RECIPIENT") or os.environ.get("RECIPIENT_EMAIL")

# Stores' POS systems post till data to PAR on their own schedule — a store
# can be absent from the report at 4 AM and present an hour later. When an
# expected-open store is missing, wait and re-pull instead of sending a
# phantom NO TILL DATA flag.
RETRY_WAIT_MINUTES = int(os.environ.get("RETRY_WAIT_MINUTES", "20"))
MAX_DATA_RETRIES = int(os.environ.get("MAX_DATA_RETRIES", "3"))


def expected_store_ids(company: dict, business_date) -> set[str]:
    """Stores that should show till activity that day (not marked closed)."""
    day_key = WEEKDAY_KEYS[business_date.weekday()]
    return {str(s["id"]) for s in company["stores"]
            if (s.get("hours") or {}).get(day_key) not in (None, "closed")}


def pull_till_history(portal, company: dict, business_date):
    """Best-effort Till History pull: (rows, None) or (None, error text).
    Skipped (None, None) when the company has no till_history_report_id."""
    if not company.get("till_history_report_id"):
        return None, None
    try:
        return portal.fetch_till_history_rows(business_date), None
    except Exception as e:
        err = f"{type(e).__name__}: {str(e).splitlines()[0][:300]}"
        print(f"  till history pull failed (first/last report stands alone): {err}")
        return None, err


def notify_operator(subject: str, html: str) -> None:
    if not FAILURE_RECIPIENT:
        return
    try:
        send_email(FAILURE_RECIPIENT, subject, html)
    except Exception as e:
        print(f"  also failed to send operator heads-up: {e}")


def load_companies() -> list[dict]:
    companies = []
    for path in sorted(COMPANIES_DIR.glob("*.json")):
        with open(path, encoding="utf-8-sig") as f:
            company = json.load(f)
        company["_file"] = path.name
        companies.append(company)
    return companies


def credentials_for(company: dict) -> tuple[str, str]:
    prefix = company["credentials_env_prefix"]
    user = os.environ.get(f"{prefix}_DC01_USERNAME")
    password = os.environ.get(f"{prefix}_DC01_PASSWORD")
    if not user or not password:
        raise RuntimeError(
            f"missing {prefix}_DC01_USERNAME / {prefix}_DC01_PASSWORD env vars")
    return user, password


def apply_live_hours(company: dict, business_date) -> None:
    """Overlay current bk.com hours onto the config (config = fallback).

    Hours edits made in the BK/RBI system (holidays, hood-cleaning nights)
    flow to the bk.com locator, so checking live prevents false flags —
    provided the adjusted hours are still posted when this runs (the
    morning after the business day)."""
    if not company.get("live_hours"):
        return
    day_key = WEEKDAY_KEYS[business_date.weekday()]
    try:
        live = fetch_live_hours(company["stores"])
    except Exception as e:
        print(f"  live hours lookup failed entirely ({e}) — using config hours")
        return
    used = 0
    for store in company["stores"]:
        sid = str(store["id"])
        if sid not in live:
            print(f"  #{sid}: not found on bk.com locator — using config hours")
            continue
        config_day = (store.get("hours") or {}).get(day_key)
        live_day = live[sid].get(day_key)
        if live_day != config_day:
            print(f"  #{sid}: live hours differ from config for {day_key}: "
                  f"live {live_day} vs config {config_day}")
        store["hours"] = live[sid]
        used += 1
    company["_live_hours_used"] = used
    print(f"  hours: live from bk.com for {used}/{len(company['stores'])} stores")


def process_company(company: dict) -> bool:
    """Returns True on success (regardless of flags), False on failure."""
    name = company["name"]
    tz = ZoneInfo(company.get("timezone", "America/Los_Angeles"))
    business_date = (datetime.now(tz) - timedelta(days=1)).date()
    print(f"\n--- {name}: business day {business_date.isoformat()} ---")

    try:
        apply_live_hours(company, business_date)
        user, password = credentials_for(company)
        with PortalSession(company, user, password) as portal:
            till_rows = portal.fetch_till_rows(business_date)
            history_rows, history_err = pull_till_history(portal, company, business_date)
            expected = expected_store_ids(company, business_date)
            for attempt in range(1, MAX_DATA_RETRIES + 1):
                # a store on either report has posted; only wait for the rest
                missing = (expected - set(aggregate_units(till_rows))
                           - set(aggregate_units(history_rows or [])))
                if not missing:
                    break
                print(f"  no till data yet for {sorted(missing, key=int)} — "
                      f"waiting {RETRY_WAIT_MINUTES} min for PAR to catch up "
                      f"(retry {attempt}/{MAX_DATA_RETRIES})")
                time.sleep(RETRY_WAIT_MINUTES * 60)
                till_rows = portal.fetch_till_rows(business_date)
                history_rows, history_err = pull_till_history(portal, company, business_date)
            print(f"  report rows: {len(till_rows)}")
            units_primary = aggregate_units(till_rows)
            units, discrepancies = units_primary, []
            if history_rows is not None:
                print(f"  till history rows: {len(history_rows)}")
                units, discrepancies = cross_check_units(
                    units_primary, aggregate_units(history_rows))
                company["_till_history_checked"] = True
            changed = {d["store"] for d in discrepancies}
            for num in sorted(units, key=lambda n: int(n)):
                u = units[num]
                eo = fmt_minutes(u["earliest_open"]) if u["earliest_open"] is not None else "—"
                lc = fmt_minutes(u["latest_close"]) if u["latest_close"] is not None else "—"
                mark = "  * widened by Till History" if num in changed else ""
                print(f"    {num:>6}: open {eo:>9}  close {lc:>9}{mark}")
            for d in discrepancies:
                print(f"  CROSS-CHECK: #{d['store']} {d['field']}: first/last report "
                      f"{d['primary']}, Till History {d['history']}")
            flags, notes = evaluate_company(company, till_rows, business_date,
                                            units=units)
            for note in notes:
                print(f"  NOTE: {note}")

            # Operator heads-up: PAR's first/last report was wrong (and what
            # that changed), or the safeguard couldn't run at all.
            if discrepancies:
                flags_primary, _ = evaluate_company(
                    company, till_rows, business_date, units=units_primary)
                removed, added = diff_flags(flags_primary, flags)
                for f in removed:
                    print(f"  cross-check prevented: #{f['store']} {f['issue']} "
                          f"(first/last said {f['actual']})")
                notify_operator(*build_cross_check_email(
                    name, business_date, discrepancies, removed, added))
            elif history_err:
                notify_operator(*build_cross_check_failure_email(
                    name, business_date, history_err, len(flags)))

            # Timecards are pulled every night: manager arrival is its own
            # check (a manager clocking in after open can't be caught by till
            # activity), plus attribution on till flags. Best-effort — a
            # failed pull never sinks the till alerts.
            if company.get("timecard_report_id"):
                print("  pulling timecards...")
                try:
                    shifts = portal.fetch_timecard_shifts(business_date)
                    print(f"  timecard shifts: {len(shifts)}")
                    attach_managers(flags, shifts, company)
                    flags.extend(evaluate_manager_arrivals(
                        company, shifts, business_date, units))
                    flags = condense_flags(flags)
                except Exception as e:
                    err = f"{type(e).__name__}: {str(e).splitlines()[0][:300]}"
                    print(f"  timecard checks failed (till alerts still sent): {err}")
                    # Heads-up to the operator only: a silent skip here hid a
                    # week of failures in Sep 2026.
                    if FAILURE_RECIPIENT:
                        subject, html = build_timecard_failure_email(
                            name, business_date, err, len(flags))
                        try:
                            send_email(FAILURE_RECIPIENT, subject, html)
                        except Exception as e2:
                            print(f"  also failed to send timecard heads-up: {e2}")
    except Exception:
        err = traceback.format_exc()
        print(f"  FAILED:\n{err}")
        if FAILURE_RECIPIENT:
            subject, html = build_failure_email(name, business_date, err)
            try:
                send_email(FAILURE_RECIPIENT, subject, html)
            except Exception as e:
                print(f"  also failed to send failure email: {e}")
        return False

    if not flags:
        print(f"  ALL CLEAR — no email sent ({len(company['stores'])} stores checked)")
        return True

    print(f"  {len(flags)} flag(s):")
    for f in flags:
        mins = f.get("minutes_off")
        off = "" if mins is None else (" (under 1 min)" if mins < 1 else f" ({mins:g} min)")
        print(f"    #{f['store']} {f['issue']}{off}: expected {f['expected']}, "
              f"actual {f['actual']}")
        if f.get("manager"):
            print(f"      {f['manager']}")

    # Company-wide digest: every flag, to the company-level list
    subject, html = build_flags_email(company, flags, business_date)
    recipients = company.get("recipients") or ([FAILURE_RECIPIENT] if FAILURE_RECIPIENT else [])
    for recipient in recipients:
        print(f"  sending full digest to {recipient}...")
        send_email(recipient, subject, html)

    # Named lists: each list covers an explicit set of stores (a store may
    # appear on several lists). Recipients get only their stores' flags;
    # a list with no flags that day gets nothing.
    for dist in company.get("lists") or []:
        list_recipients = dist.get("recipients") or []
        list_stores = {str(x) for x in dist.get("stores") or []}
        if not list_recipients or not list_stores:
            continue
        list_flags = [f for f in flags if str(f["store"]) in list_stores]
        if not list_flags:
            print(f"  list {dist.get('name')}: no flags — no email")
            continue
        subject, html = build_flags_email(
            company, list_flags, business_date, region_name=dist.get("name"))
        for recipient in list_recipients:
            print(f"  sending {dist.get('name')} digest "
                  f"({len(list_flags)} flags) to {recipient}...")
            send_email(recipient, subject, html)

    # Store alerts: each flagged store's own email (its GM) gets that
    # store's flags, so the store learns the same morning.
    if company.get("send_store_alerts"):
        store_by_id = {str(s["id"]): s for s in company["stores"]}
        flagged_ids = sorted({str(f["store"]) for f in flags}, key=int)
        for sid in flagged_ids:
            store = store_by_id.get(sid)
            store_email = (store or {}).get("email")
            if not store_email:
                continue
            store_flags = [f for f in flags if str(f["store"]) == sid]
            subject, html = build_flags_email(
                company, store_flags, business_date,
                region_name=store.get("name") or f"#{sid}")
            print(f"  sending store alert to {store_email}...")
            send_email(store_email, subject, html)
    return True


def backfill_timecards(business_date, store_id: str | None) -> bool:
    """`python main.py --timecards YYYY-MM-DD [--store N]`: re-pull the
    timecards for a past business day and print what the nightly run would
    have reported — every shift for a single store, or the manager punches
    and any MANAGER LATE IN flags for the whole company. Print only, never
    emails. Uses config hours (bk.com only serves current hours)."""
    ok = True
    for company in load_companies():
        if not company.get("timecard_report_id"):
            continue
        name = company["name"]
        ids = {str(s["id"]) for s in company["stores"]}
        if store_id and store_id not in ids:
            continue
        print(f"\n--- {name}: timecards for {business_date.isoformat()}"
              f"{' — store #' + store_id if store_id else ''} ---")
        try:
            user, password = credentials_for(company)
            with PortalSession(company, user, password) as portal:
                shifts = portal.fetch_timecard_shifts(
                    business_date, {store_id} if store_id else None)
        except Exception:
            print(f"  FAILED:\n{traceback.format_exc()}")
            ok = False
            continue
        print(f"  timecard shifts: {len(shifts)}")

        manager_titles = [m.lower() for m in company.get("manager_titles", [])]
        by_store: dict[str, list[dict]] = {}
        for s in shifts:
            by_store.setdefault(store_number(s["unit_name"]) or "?", []).append(s)
        for sid in sorted(by_store, key=lambda n: int(n) if n.isdigit() else 0):
            rows = by_store[sid]
            if not store_id:  # company-wide: managers only, keep it readable
                rows = [r for r in rows
                        if any(m in r["title"].lower() for m in manager_titles)]
            print(f"  #{sid}:")
            for r in sorted(rows, key=lambda r: _parse_report_time(r["clock_in"]) or 0):
                print(f"    {r['clock_in']:>9} - {r['clock_out'] or '—':>9}  "
                      f"{r['title']:<26} {r['employee']}")

        late = evaluate_manager_arrivals(company, shifts, business_date)
        if store_id:
            late = [f for f in late if str(f["store"]) == store_id]
        if late:
            print(f"  {len(late)} MANAGER LATE IN flag(s):")
            for f in late:
                print(f"    #{f['store']} expected {f['expected']}: {f['manager']}")
        else:
            print("  no MANAGER LATE IN flags")
    return ok


def _window(u: dict | None) -> str:
    if not u:
        return "—".center(25)
    eo = u["earliest_open_text"] or "—"
    lc = u["latest_close_text"] or "—"
    return f"{eo:>11} – {lc:<11}"


def backfill_tills(business_date, store_id: str | None) -> bool:
    """`python main.py --tills YYYY-MM-DD [--store N]`: pull both till
    reports for a past business day and print them side by side, marking
    every store where Till History disagrees with the Earliest/Latest
    report. With --store, also lists that store's drawers from each report.
    Print only, never emails."""
    ok = True
    for company in load_companies():
        name = company["name"]
        ids = {str(s["id"]) for s in company["stores"]}
        if store_id and store_id not in ids:
            continue
        print(f"\n--- {name}: tills for {business_date.isoformat()}"
              f"{' — store #' + store_id if store_id else ''} ---")
        try:
            user, password = credentials_for(company)
            with PortalSession(company, user, password) as portal:
                till_rows = portal.fetch_till_rows(business_date)
                history_rows, history_err = pull_till_history(
                    portal, company, business_date)
        except Exception:
            print(f"  FAILED:\n{traceback.format_exc()}")
            ok = False
            continue
        if history_rows is None:
            print("  no Till History: " + (history_err or
                  "add till_history_report_id to the company config"))
            ok = ok and not history_err
        primary = aggregate_units(till_rows)
        history = aggregate_units(history_rows or [])
        _, discrepancies = cross_check_units(primary, history)
        changed = {d["store"] for d in discrepancies}
        print(f"  {'store':>7}  {'Earliest/Latest report':^25}  {'Till History':^25}")
        for num in sorted(set(primary) | set(history), key=int):
            if store_id and num != store_id:
                continue
            mark = "  <-- disagree" if num in changed else ""
            print(f"  {num:>7}  {_window(primary.get(num))}  "
                  f"{_window(history.get(num))}{mark}")
        if store_id:
            for label, rows in (("Earliest/Latest report", till_rows),
                                ("Till History", history_rows or [])):
                print(f"  {label} drawers:")
                for r in rows:
                    if store_number(r["unit_name"]) != store_id:
                        continue
                    print(f"    {r['opened']:>11} – {r['closed'] or '—':<11}  "
                          f"{r['drawer']:<8} {r['employee']}")
        for d in discrepancies:
            if store_id and d["store"] != store_id:
                continue
            print(f"  CROSS-CHECK: #{d['store']} {d['field']}: first/last report "
                  f"{d['primary']}, Till History {d['history']}")
    return ok


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--tills":
        if len(sys.argv) < 3:
            print("usage: python main.py --tills YYYY-MM-DD [--store N]")
            sys.exit(2)
        business_date = datetime.strptime(sys.argv[2], "%Y-%m-%d").date()
        store_id = None
        if "--store" in sys.argv:
            store_id = sys.argv[sys.argv.index("--store") + 1]
        sys.exit(0 if backfill_tills(business_date, store_id) else 1)

    if len(sys.argv) > 1 and sys.argv[1] == "--timecards":
        if len(sys.argv) < 3:
            print("usage: python main.py --timecards YYYY-MM-DD [--store N]")
            sys.exit(2)
        business_date = datetime.strptime(sys.argv[2], "%Y-%m-%d").date()
        store_id = None
        if "--store" in sys.argv:
            store_id = sys.argv[sys.argv.index("--store") + 1]
        sys.exit(0 if backfill_timecards(business_date, store_id) else 1)

    if not DRY_RUN and (not GMAIL_USER or not GMAIL_APP_PASSWORD):
        print("ERROR: GMAIL_USER and GMAIL_APP_PASSWORD must be set (or DRY_RUN=true).")
        sys.exit(1)

    companies = load_companies()
    if not companies:
        print(f"ERROR: no company configs found in {COMPANIES_DIR}/")
        sys.exit(1)

    ok = True
    for company in companies:
        ok = process_company(company) and ok

    if not ok:
        sys.exit(1)
    print("\nDone.")


if __name__ == "__main__":
    main()
