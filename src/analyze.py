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


def _parse_report_time(text: str) -> int | None:
    """'06:28:27 AM' -> minutes since business-day midnight (rollover-adjusted)."""
    text = text.strip()
    if not text:
        return None
    dt = datetime.strptime(text, "%I:%M:%S %p")
    minutes = dt.hour * 60 + dt.minute
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


def fmt_minutes(minutes: int) -> str:
    """Minutes since midnight -> '6:28 AM' (or '12:03 AM' for past-midnight)."""
    minutes %= 24 * 60
    h, m = divmod(minutes, 60)
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}"


def store_number(unit_name: str) -> str | None:
    """'12622 - E Columbia St' -> '12622' (also handles '25546- N 11th Ave')."""
    m = re.match(r"\s*(\d+)", unit_name)
    return m.group(1) if m else None


def aggregate_units(till_rows: list[dict]) -> dict[str, dict]:
    """Raw drawer rows -> {store_number: {earliest_open, latest_close, unit_name}}."""
    units: dict[str, dict] = {}
    for row in till_rows:
        num = store_number(row["unit_name"])
        if num is None:
            continue
        opened = _parse_report_time(row["opened"])
        closed = _parse_report_time(row["closed"])
        u = units.setdefault(num, {"unit_name": row["unit_name"],
                                   "earliest_open": None, "latest_close": None})
        if opened is not None and (u["earliest_open"] is None or opened < u["earliest_open"]):
            u["earliest_open"] = opened
        if closed is not None and (u["latest_close"] is None or closed > u["latest_close"]):
            u["latest_close"] = closed
    return units


def evaluate_company(company: dict, till_rows: list[dict],
                     business_date: date) -> tuple[list[dict], list[str]]:
    """Returns (flags, notes).

    flags: [{store, name, issue, expected, actual, minutes_off}]
      issue in {"LATE OPEN", "EARLY CLOSE", "NO TILL DATA"}
    notes: non-fatal oddities (report units not in config, etc.)
    """
    grace = int(company.get("grace_minutes", 15))
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
                    "actual": fmt_minutes(unit["earliest_open"]),
                    "minutes_off": delta,
                })

        if unit["latest_close"] is not None:
            delta = expected_close - unit["latest_close"]
            if delta > grace:
                flags.append({
                    "store": sid, "name": label, "issue": "EARLY CLOSE",
                    "expected": fmt_minutes(expected_close),
                    "actual": fmt_minutes(unit["latest_close"]),
                    "minutes_off": delta,
                })

    for num, unit in units.items():
        if num not in config_ids:
            notes.append(f"report unit {unit['unit_name']!r} has no matching "
                         f"store in config (id {num})")

    return flags, notes
