"""Live store hours from the bk.com store locator backend (RBI GraphQL).

Andrew's team maintains hours in the BK/RBI system (holidays, hood-cleaning
early closes, etc.), and that system feeds the bk.com locator. Pulling hours
live each night means those adjustments are honored automatically instead of
producing false flags against a stale config.

The endpoint is the same one the bk.com website calls — public, no API key:
    POST https://use1-prod-bk-gateway.rbictg.com/graphql
        ?operationName=GetNearbyRestaurants
queried by each store's coordinates and matched on storeId. The expected
open/close span for a day is the widest across dining room and drive-thru
(a store is "open" if any channel is).

Config hours remain the fallback whenever a store can't be fetched/matched.
"""

import json
import urllib.request

ENDPOINT = ("https://use1-prod-bk-gateway.rbictg.com/graphql"
            "?operationName=GetNearbyRestaurants")

_QUERY = """query GetNearbyRestaurants($input: NearbyRestaurantsInput!) {
  restaurantsV2 {
    nearby(input: $input) {
      nodes {
        storeId
        diningRoomHours { ...OperatingHoursFragment }
        driveThruHours { ...OperatingHoursFragment }
      }
    }
  }
}
fragment OperatingHoursFragment on OperatingHours {
  friClose friOpen monClose monOpen satClose satOpen sunClose sunOpen
  thrClose thrOpen tueClose tueOpen wedClose wedOpen
}"""

# RBI abbreviates Thursday as "thr"; our config uses "thu"
_DAY_KEYS = [("mon", "mon"), ("tue", "tue"), ("wed", "wed"), ("thu", "thr"),
             ("fri", "fri"), ("sat", "sat"), ("sun", "sun")]

_ROLLOVER_HOUR = 4  # close times before 4 AM sort as next-day (matches analyze)


def _hhmm(value: str | None) -> str | None:
    """'06:00:00' -> '06:00'."""
    if not value:
        return None
    return value[:5]


def _close_sort_key(value: str) -> int:
    h, m = int(value[:2]), int(value[3:5])
    minutes = h * 60 + m
    return minutes + 24 * 60 if h < _ROLLOVER_HOUR else minutes


def _open_sort_key(value: str) -> int:
    return int(value[:2]) * 60 + int(value[3:5])


def _effective_day(node: dict, rbi_day: str) -> dict | None:
    """Widest open/close across dining room + drive-thru for one weekday."""
    opens, closes = [], []
    for svc in ("diningRoomHours", "driveThruHours"):
        hours = node.get(svc) or {}
        o = _hhmm(hours.get(f"{rbi_day}Open"))
        c = _hhmm(hours.get(f"{rbi_day}Close"))
        if o and c:
            opens.append(o)
            closes.append(c)
    if not opens:
        return None  # closed (or no data) that day
    return {"open": min(opens, key=_open_sort_key),
            "close": max(closes, key=_close_sort_key)}


def _query_nearby(lat: float, lng: float, timeout: int = 30) -> list[dict]:
    body = json.dumps({
        "operationName": "GetNearbyRestaurants",
        "variables": {"input": {
            "pagination": {"first": 8},
            "radiusStrictMode": False,
            "status": "OPEN",
            "coordinates": {"searchRadius": 15, "userLat": lat, "userLng": lng},
        }},
        "query": _QUERY,
    }).encode()
    req = urllib.request.Request(ENDPOINT, data=body, headers={
        "content-type": "application/json",
        "x-ui-language": "en",
        "x-ui-region": "US",
        "x-ui-platform": "web",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    if data.get("errors"):
        raise RuntimeError(f"GraphQL errors: {json.dumps(data['errors'])[:300]}")
    return data["data"]["restaurantsV2"]["nearby"]["nodes"]


def fetch_live_hours(stores: list[dict]) -> dict[str, dict]:
    """{store_id: {mon: {open, close} | None, ...}} for stores found on
    bk.com. Stores without lat/lng in config, not returned by the locator,
    or hit by a network error are simply absent — caller falls back to
    config hours."""
    live: dict[str, dict] = {}
    for store in stores:
        sid = str(store["id"])
        lat, lng = store.get("lat"), store.get("lng")
        if lat is None or lng is None or sid in live:
            continue
        try:
            nodes = _query_nearby(lat, lng)
        except Exception:
            continue  # this store falls back to config; others still try
        for node in nodes:
            nid = str(node.get("storeId") or "")
            # a nearby query often returns sibling stores — keep any that
            # belong to this company so one request can satisfy several
            for s in stores:
                if nid == str(s["id"]) and nid not in live:
                    live[nid] = {cfg_day: _effective_day(node, rbi_day)
                                 for cfg_day, rbi_day in _DAY_KEYS}
    return live
