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


def evaluate_company(company: dict, till_rows: list[dict],
                     business_date: date) -> tuple[list[dict], list[str]]:
    """Returns (flags, notes).

    flags: [{store, name, issue, expected, actual, minutes_off}]
      issue in {"LATE OPEN", "EARLY CLOSE", "NO TILL DATA"}
    notes: non-fatal oddities (report units not in config, etc.)
    """
    grace = int(company.get("grace_minutes", 15))
    # Closing gets its own (stricter) grace: tills normally close at or after
    # the posted close during closing procedures, so early is early.
    close_grace = int(company.get("close_grace_minutes", 0))
    day_key = WEEKDAY_KEYS[business_date.weekday()]
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


def attach_managers(flags: list[dict], shifts: list[dict],
                    company: dict) -> None:
    """For each LATE OPEN / EARLY CLOSE flag, name the opening manager
    (first manager clock-in) or closing manager (last manager clock-out).

    Mutates each flag: adds 'manager' (display string) when found.
    """
    manager_titles = [m.lower() for m in
                      company.get("manager_titles", DEFAULT_MANAGER_TITLES)]

    # store number -> manager shifts with parsed minutes
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
