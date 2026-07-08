#!/usr/bin/env python3
"""TillWatch — flags stores that opened late or closed early, based on the
'Tills: Earliest Open / Latest Close' report in PAR Data Central.

For each company in companies/: pull yesterday's report, compare each store's
first till open / last till close against its posted hours (with a grace
period), and email the company's list ONLY if something was flagged.
"""

import json
import os
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.analyze import (WEEKDAY_KEYS, attach_managers, condense_flags,
                         evaluate_company, evaluate_manager_arrivals,
                         fmt_minutes, aggregate_units)
from src.emailer import (DRY_RUN, GMAIL_USER, GMAIL_APP_PASSWORD,
                         build_failure_email, build_flags_email, send_email)
from src.hours import fetch_live_hours
from src.portal import PortalSession

COMPANIES_DIR = Path(__file__).parent / "companies"
FAILURE_RECIPIENT = os.environ.get("FAILURE_RECIPIENT") or os.environ.get("RECIPIENT_EMAIL")


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
            print(f"  report rows: {len(till_rows)}")
            units = aggregate_units(till_rows)
            for num in sorted(units, key=lambda n: int(n)):
                u = units[num]
                eo = fmt_minutes(u["earliest_open"]) if u["earliest_open"] is not None else "—"
                lc = fmt_minutes(u["latest_close"]) if u["latest_close"] is not None else "—"
                print(f"    {num:>6}: open {eo:>9}  close {lc:>9}")
            flags, notes = evaluate_company(company, till_rows, business_date)
            for note in notes:
                print(f"  NOTE: {note}")

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
                    print(f"  timecard checks failed (till alerts still sent): {e}")
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


def main():
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
