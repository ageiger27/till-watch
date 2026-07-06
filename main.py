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

from src.analyze import (attach_managers, evaluate_company, fmt_minutes,
                         aggregate_units)
from src.emailer import (DRY_RUN, GMAIL_USER, GMAIL_APP_PASSWORD,
                         build_failure_email, build_flags_email, send_email)
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


def process_company(company: dict) -> bool:
    """Returns True on success (regardless of flags), False on failure."""
    name = company["name"]
    tz = ZoneInfo(company.get("timezone", "America/Los_Angeles"))
    business_date = (datetime.now(tz) - timedelta(days=1)).date()
    print(f"\n--- {name}: business day {business_date.isoformat()} ---")

    try:
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
    subject, html = build_flags_email(company, flags, business_date)
    recipients = company.get("recipients") or ([FAILURE_RECIPIENT] if FAILURE_RECIPIENT else [])
    for recipient in recipients:
        print(f"  sending to {recipient}...")
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
