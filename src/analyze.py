"""Compare till open/close activity against each store's expected hours.

Times on the report belong to a single business day: a till closed at
12:03 AM on business date 07/02 actually closed after midnight (calendar
07/03). Any time earlier than DAY_ROLLOVER_HOUR is treated as next-day.
"""

import re
from datetime import date, datetime

DAY_ROLLOVER_HOUR = 4  # times before 4 AM belong to the tail of the business day

WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class ConfigError(ValueError):
    pass


def _parse_report_time(text: str) -> float | None:
    """'06:28:27 AM' or '5:05 PM' -> minutes since business-day midnight
    (times before the rollover hour count as next-day).

    Seconds count as fractional minutes — 6:15:08 against a 6:00 open with
    15 min grace IS more than 15 minutes late. Truncating seconds once let
    a store slip under the grace bar by 8 seconds."""
    text = text.strip()
    if not text:
        return None
    fmt = "%I:%M:%S %p" if text.count(":") == 2 else "%I:%M %p"
    dt = datetime.strptime(text, fmt)
    minutes = dt.hour * 60 + dt.minute + dt.second / 60
    if dt.hour < DAY_ROLLOVER_HOUR:
        minutes += 24 * 60
    return minutes


def _parse_config_time(text: str, *, is_close: bool = False) -> int:
    """'06:00' / '23:30' / '00:30' -> minutes; close times past midnight wrap."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if not m:
        raise ConfigError(f"bad time in config: {text!r} (expected HH:MM)")
    minutes = int(m.group(1)) * 60 + int(m.group(2))
    if is_close and int(m.group(1)) < DAY_ROLLOVER_HOUR:
        minutes += 24 * 60
    return minutes


def fmt_minutes(minutes: float) -> str:
    """Minutes since midnight -> '6:28 AM' (or '12:03 AM' for past-midnight)."""
    minutes = int(minutes) % (24 * 60)
    h, m = divmod(minutes, 60)
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}"


def store_number(unit_name: str) -> str | None:
    """'12622 - E Columbia St' -> '12622' (also handles '25546- N 11th Ave')."""
    m = re.match(r"\s*(\d+)", unit_name)
    return m.group(1) if m else None


def aggregate_units(till_rows: list[dict]) -> dict[str, dict]:
    """Raw drawer rows -> {store_number: {earliest_open, latest_close,
    earliest_open_text, latest_close_text, unit_name}}. The *_text fields
    keep the report's exact second-level timestamps for display."""
    units: dict[str, dict] = {}
    for row in till_rows:
        num = store_number(row["unit_name"])
        if num is None:
            continue
        opened = _parse_report_time(row["opened"])
        closed = _parse_report_time(row["closed"])
        u = units.setdefault(num, {"unit_name": row["unit_name"],
                                   "earliest_open": None, "latest_close": None,
                                   "earliest_open_text": "", "latest_close_text": ""})
        if opened is not None and (u["earliest_open"] is None or opened < u["earliest_open"]):
            u["earliest_open"] = opened
            u["earliest_open_text"] = row["opened"].strip().lstrip("0")
        if closed is not None and (u["latest_close"] is None or closed > u["latest_close"]):
            u["latest_close"] = closed
            u["latest_close_text"] = row["closed"].strip().lstrip("0")
    return units


# Till History prints times without seconds ('6:08 PM'), so it can sit up to
# a minute off the Earliest/Latest report's '06:08:14 PM' for the same
# drawer. Only a gap bigger than that is a real disagreement.
HISTORY_TOLERANCE_MINUTES = 1.0


def cross_check_units(primary: dict[str, dict], history: dict[str, dict],
                      tolerance: float = HISTORY_TOLERANCE_MINUTES,
                      ) -> tuple[dict[str, dict], list[dict]]:
    """Safeguard against the Earliest Open / Latest Close report being wrong.

    ``primary`` and ``history`` are aggregate_units() results from the two
    reports. A drawer on either report is proof the store was operating at
    that time, so each store's window is widened to the earliest open and
    latest close seen on either — but Till History only overrides when it
    disagrees by more than ``tolerance`` minutes (it drops seconds). A store
    present only on Till History is added, rescuing it from NO TILL DATA.

    Returns (merged_units, discrepancies); each discrepancy is
    {store, unit_name, field ('open'|'close'|'missing'), primary, history}
    with the display texts from each report. Widening can only remove
    flags, never add one (except LATE OPEN / EARLY CLOSE on a rescued store).
    """
    merged = {num: dict(u) for num, u in primary.items()}
    discrepancies: list[dict] = []
    for num, h in history.items():
        u = merged.get(num)
        if u is None:
            merged[num] = dict(h)
            discrepancies.append({
                "store": num, "unit_name": h["unit_name"], "field": "missing",
                "primary": "not on report",
                "history": f"{h['earliest_open_text'] or '—'} – "
                           f"{h['latest_close_text'] or '—'}",
            })
            continue
        if h["earliest_open"] is not None and (
                u["earliest_open"] is None
                or h["earliest_open"] < u["earliest_open"] - tolerance):
            discrepancies.append({
                "store": num, "unit_name": u["unit_name"], "field": "open",
                "primary": u["earliest_open_text"] or "—",
                "history": h["earliest_open_text"],
            })
            u["earliest_open"] = h["earliest_open"]
            u["earliest_open_text"] = h["earliest_open_text"]
        if h["latest_close"] is not None and (
                u["latest_close"] is None
                or h["latest_close"] > u["latest_close"] + tolerance):
            discrepancies.append({
                "store": num, "unit_name": u["unit_name"], "field": "close",
                "primary": u["latest_close_text"] or "—",
                "history": h["latest_close_text"],
            })
            u["latest_close"] = h["latest_close"]
            u["latest_close_text"] = h["latest_close_text"]
    return merged, discrepancies


def diff_flags(before: list[dict], after: list[dict]) -> tuple[list[dict], list[dict]]:
    """(removed, added) between two flag lists, keyed on (store, issue)."""
    key = lambda f: (str(f["store"]), f["issue"])
    before_keys = {key(f) for f in before}
    after_keys = {key(f) for f in after}
    removed = [f for f in before if key(f) not in after_keys]
    added = [f for f in after if key(f) not in before_keys]
    return removed, added


def evaluate_company(company: dict, till_rows: list[dict],
                     business_date: date,
                     units: dict[str, dict] | None = None,
                     ) -> tuple[list[dict], list[str]]:
    """Returns (flags, notes).

    flags: [{store, name, issue, expected, actual, minutes_off}]
      issue in {"LATE OPEN", "EARLY CLOSE", "NO TILL DATA"}
    notes: non-fatal oddities (report units not in config, etc.)

    ``units`` (per-store windows, e.g. after cross_check_units) takes the
    place of aggregating ``till_rows`` when given.
    """
    grace = int(company.get("grace_minutes", 15))
    # Closing gets its own (stricter) grace: tills normally close at or after
    # the posted close during closing procedures, so early is early.
    close_grace = int(company.get("close_grace_minutes", 0))
    day_key = WEEKDAY_KEYS[business_date.weekday()]
    if units is None:
        units = aggregate_units(till_rows)
    flags: list[dict] = []
    notes: list[str] = []

    config_ids = set()
    for store in company["stores"]:
        sid = str(store["id"])
        config_ids.add(sid)
        label = store.get("name") or store.get("address") or sid
        hours = (store.get("hours") or {}).get(day_key)

        if hours in (None, "closed"):
            continue  # store not expected open that day
        if hours == "24h" or store.get("open_24h"):
            continue  # can't open late / close early

        expected_open = _parse_config_time(hours["open"])
        expected_close = _parse_config_time(hours["close"], is_close=True)

        unit = units.get(sid)
        if unit is None or (unit["earliest_open"] is None and unit["latest_close"] is None):
            flags.append({
                "store": sid, "name": label, "issue": "NO TILL DATA",
                "expected": f"{fmt_minutes(expected_open)} – {fmt_minutes(expected_close)}",
                "actual": "no till activity on report",
                "minutes_off": None,
            })
            continue

        if unit["earliest_open"] is not None:
            delta = unit["earliest_open"] - expected_open
            if delta > grace:
                flags.append({
                    "store": sid, "name": label, "issue": "LATE OPEN",
                    "expected": fmt_minutes(expected_open),
                    "actual": unit["earliest_open_text"] or fmt_minutes(unit["earliest_open"]),
                    "minutes_off": round(delta, 1),
                })

        if unit["latest_close"] is not None:
            delta = expected_close - unit["latest_close"]
            if delta > close_grace:
                flags.append({
                    "store": sid, "name": label, "issue": "EARLY CLOSE",
                    "expected": fmt_minutes(expected_close),
                    "actual": unit["latest_close_text"] or fmt_minutes(unit["latest_close"]),
                    "minutes_off": round(delta, 1),
                })

    for num, unit in units.items():
        if num not in config_ids:
            notes.append(f"report unit {unit['unit_name']!r} has no matching "
                         f"store in config (id {num})")

    return flags, notes


# ---------------------------------------------------------------------------
# Manager attribution (from the Daily Time Card Review report)
# ---------------------------------------------------------------------------

DEFAULT_MANAGER_TITLES = ["hourly general manager", "assistant mgr",
                          "assistant manager"]


def _is_manager(title: str, manager_titles: list[str]) -> bool:
    t = title.strip().lower()
    return any(m in t or t in m for m in manager_titles if m)


def _display_name(employee: str) -> str:
    """Report's 'CHANTHAVONG, SING (2075)' -> 'Sing Chanthavong'.

    Drops the trailing employee id, reorders 'Last, First' to 'First Last',
    and title-cases names the POS stored in all caps (mixed-case names are
    left untouched to preserve spellings like 'De La Cruz' or 'Jr.')."""
    name = re.sub(r"\s*\([^)]*\)\s*$", "", employee.strip())
    if "," in name:
        last, first = name.split(",", 1)
        name = f"{first.strip()} {last.strip()}"
    if name.isupper():
        name = name.title()
    return name


def _manager_shifts_by_store(shifts: list[dict],
                             manager_titles: list[str]) -> dict[str, list[dict]]:
    by_store: dict[str, list[dict]] = {}
    for s in shifts:
        num = store_number(s["unit_name"])
        if num is None or not _is_manager(s.get("title", ""), manager_titles):
            continue
        by_store.setdefault(num, []).append({
            **s,
            "in_min": _parse_report_time(s.get("clock_in", "")),
            "out_min": _parse_report_time(s.get("clock_out", "")),
        })
    return by_store


def evaluate_manager_arrivals(company: dict, shifts: list[dict],
                              business_date: date,
                              units: dict[str, dict] | None = None) -> list[dict]:
    """Flag stores whose first manager (HGM/AM) clock-in is after the time
    they must be in by — posted open minus manager_open_lead_minutes
    (default 0: a manager must be clocked in by open). A store can't open
    on time without a manager in the building, even if the first till still
    lands inside the till grace period."""
    manager_titles = [m.lower() for m in
                      company.get("manager_titles", DEFAULT_MANAGER_TITLES)]
    lead = int(company.get("manager_open_lead_minutes", 0))
    day_key = WEEKDAY_KEYS[business_date.weekday()]
    by_store = _manager_shifts_by_store(shifts, manager_titles)

    flags: list[dict] = []
    for store in company["stores"]:
        sid = str(store["id"])
        label = store.get("name") or store.get("address") or sid
        hours = (store.get("hours") or {}).get(day_key)
        if hours in (None, "closed") or hours == "24h" or store.get("open_24h"):
            continue
        expected_open = _parse_config_time(hours["open"])
        must_be_in_by = expected_open - lead

        punches = [s for s in by_store.get(sid, []) if s["in_min"] is not None]
        if not punches:
            continue  # no manager punch at all — surfaced as a note upstream
        first = min(punches, key=lambda x: x["in_min"])
        delta = first["in_min"] - must_be_in_by
        if delta > 0:
            clock_in = first.get("clock_in") or fmt_minutes(first["in_min"])
            # Informational: when the till still opened inside its own grace,
            # show that first till time anyway so the row isn't blank
            till_open = ((units or {}).get(sid) or {}).get("earliest_open_text")
            flags.append({
                "store": sid, "name": label, "issue": "MANAGER LATE IN",
                "expected": f"in by {fmt_minutes(must_be_in_by)}",
                "actual": f"till on {till_open}" if till_open else "—",
                "minutes_off": round(delta, 1),
                "manager": (f"Opening: {_display_name(first['employee'])} — "
                            f"clocked in {clock_in}"),
            })
    return flags


def _off_text(minutes: float) -> str:
    return "under 1 min" if minutes < 1 else f"{minutes:g} min"


def condense_flags(flags: list[dict]) -> list[dict]:
    """Merge a store's LATE OPEN + MANAGER LATE IN into a single row —
    they're one story: the manager arrived late, so the till opened late.
    The till columns keep the LATE OPEN facts; the combined label carries
    both deltas; the manager line carries the punch and the in-by bar."""
    mgr_late = {f["store"]: f for f in flags if f["issue"] == "MANAGER LATE IN"}
    merged_stores = set()
    for f in flags:
        if f["issue"] != "LATE OPEN" or f["store"] not in mgr_late:
            continue
        m = mgr_late[f["store"]]
        f["issue"] = (f"LATE OPEN ({_off_text(f['minutes_off'])}) "
                      f"+ MGR LATE IN ({_off_text(m['minutes_off'])})")
        f["minutes_off"] = None  # deltas are already in the label
        f["manager"] = f"{m['manager']}, needed {m['expected']}"
        merged_stores.add(f["store"])
    return [f for f in flags
            if not (f["issue"] == "MANAGER LATE IN"
                    and f["store"] in merged_stores)]


def attach_managers(flags: list[dict], shifts: list[dict],
                    company: dict) -> None:
    """For each LATE OPEN / EARLY CLOSE flag, name the opening manager
    (first manager clock-in) or closing manager (last manager clock-out).

    Mutates each flag: adds 'manager' (display string) when found.
    """
    manager_titles = [m.lower() for m in
                      company.get("manager_titles", DEFAULT_MANAGER_TITLES)]
    by_store = _manager_shifts_by_store(shifts, manager_titles)

    for flag in flags:
        mgr_shifts = by_store.get(str(flag["store"]), [])
        if flag["issue"] == "LATE OPEN":
            candidates = [s for s in mgr_shifts if s["in_min"] is not None]
            if candidates:
                s = min(candidates, key=lambda x: x["in_min"])
                flag["manager"] = (f"Opening: {_display_name(s['employee'])} — "
                                   f"clocked in {fmt_minutes(s['in_min'])}")
            else:
                flag["manager"] = "Opening: no HGM/AM punch found"
        elif flag["issue"] == "EARLY CLOSE":
            candidates = [s for s in mgr_shifts if s["out_min"] is not None]
            if candidates:
                s = max(candidates, key=lambda x: x["out_min"])
                flag["manager"] = (f"Closing: {_display_name(s['employee'])} — "
                                   f"clocked out {fmt_minutes(s['out_min'])}")
            else:
                flag["manager"] = "Closing: no HGM/AM punch found"
