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

from src.analyze import (WEEKDAY_KEYS, attach_managers, evaluate_company,
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

            # Manager attribution: only worth a (slow) group-wide timecard
            # pull when a late-open/early-close flag needs a name on it.
            if (company.get("timecard_report_id")
                    and any(f["issue"] in ("LATE OPEN", "EARLY CLOSE")
                            for f in flags)):
                print("  pulling timecards for manager attribution...")
                try:
                    shifts = portal.fetch_timecard_shifts(business_date)
                    print(f"  timecard shifts: {len(shifts)}")
                    attach_managers(flags, shifts, company)
                except Exception as e:
                    # attribution is best-effort — never sink the alert itself
                    print(f"  manager attribution failed (alert still sent): {e}")
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
        print(f"    #{f['store']} {f['issue']}: expected {f['expected']}, "
              f"actual {f['actual']}")
        if f.get("manager"):
            print(f"      {f['manager']}")

    # Company-wide digest: every flag, to the company-level list
    subject, html = build_flags_email(company, flags, business_date)
    recipients = company.get("recipients") or ([FAILURE_RECIPIENT] if FAILURE_RECIPIENT else [])
    for recipient in recipients:
        print(f"  sending full digest to {recipient}...")
        send_email(recipient, subject, html)

    # Regional digests: only that region's flags, to that region's list.
    # A region with no flags (or no recipients) gets nothing.
    regions = company.get("regions") or {}
    if regions:
        region_of = {str(s["id"]): s.get("region") for s in company["stores"]}
        for key, region in regions.items():
            region_recipients = region.get("recipients") or []
            if not region_recipients:
                continue
            region_flags = [f for f in flags
                            if region_of.get(str(f["store"])) == key]
            if not region_flags:
                print(f"  region {key}: no flags — no email")
                continue
            subject, html = build_flags_email(
                company, region_flags, business_date,
                region_name=region.get("name", key))
            for recipient in region_recipients:
                print(f"  sending {region.get('name', key)} digest "
                      f"({len(region_flags)} flags) to {recipient}...")
                send_email(recipient, subject, html)
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
