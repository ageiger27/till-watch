"""Portal access: Playwright login + the report API flow.

The 'Tills: Earliest Open / Latest Close' report is a DevExpress web report.
Rather than scraping the rendered viewer (slow, shadow-DOM, paginated), we
replicate the API calls the viewer itself makes — discovered by capturing
network traffic while the report loaded:

    POST {portal}/feed/allreports/reportdetail/api/Reports/{report_id}/prepare
        JSON {"@FromDate", "@ThruDate", "@UnitID": "null", "@GroupID", ...}
        -> Data.ResultID
    POST .../api/DXXRDV  actionKey=openReport  arg=<ResultID>
        -> documentId, pageCount
    POST .../api/DXXRDV  actionKey=getBuildStatus  (poll until completed)
    GET  .../api/DXXRDV  actionKey=getPage  arg={"pageIndex", "documentId",
                                                 "resolution": 96,
                                                 "includeBricks": true}
        -> result.brick: a tree of positioned "bricks" whose leaf nodes hold
           every table cell's text. We reconstruct rows from coordinates.

Only the login itself touches the UI (the login form is simple and stable).
Everything else is plain HTTP via the logged-in browser context's cookies.
"""

import json
import urllib.parse
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

DXVERSIONS = json.dumps(
    {"analytics": "24.1.6", "devextreme": "24.1.6", "reporting": "24.1.6"}
)
LOGIN_TIMEOUT_MS = 120_000
BUILD_POLL_SECONDS = 105  # server-side long-poll timeout used by the real viewer


class PortalError(RuntimeError):
    pass


def _tzoffset_hours(tz_name: str, on_date: date) -> int:
    """The portal sends the client's UTC offset in hours (e.g. -7 for PDT)."""
    tz = ZoneInfo(tz_name)
    offset = datetime(on_date.year, on_date.month, on_date.day, 12,
                      tzinfo=tz).utcoffset()
    return int(offset.total_seconds() // 3600)


def _result(resp, what: str) -> dict:
    if resp.status != 200:
        raise PortalError(f"{what} failed: HTTP {resp.status}: {resp.text()[:300]}")
    body = resp.json()
    if body.get("error"):
        raise PortalError(f"{what} returned error: {body['error']}")
    result = body.get("result", body.get("Data"))
    if isinstance(result, str):
        result = json.loads(result)
    if result is None:
        raise PortalError(f"{what} returned no result: {str(body)[:300]}")
    return result


def _walk_bricks(brick, out, oy=0, ox=0):
    """Flatten the brick tree into leaf text cells with absolute positions."""
    if brick is None:
        return
    top = oy + brick.get("top", 0)
    left = ox + brick.get("left", 0)
    kids = brick.get("bricks")
    if kids:
        for k in kids:
            _walk_bricks(k, out, top, left)
        return
    for kv in brick.get("content") or []:
        if kv.get("Key") == "text":
            out.append({"top": top, "left": left, "text": kv.get("Value") or ""})


def _cells_to_rows(cells: list[dict]) -> list[list[dict]]:
    """Group positioned cells into visual rows (same top coordinate ±3px)."""
    cells = sorted(cells, key=lambda c: (c["top"], c["left"]))
    rows, cur, last_top = [], [], None
    for c in cells:
        if last_top is not None and abs(c["top"] - last_top) > 3:
            rows.append(cur)
            cur = []
        cur.append(c)
        last_top = c["top"]
    if cur:
        rows.append(cur)
    return rows


HEADER_COLUMNS = ["Unit Name", "Business Date", "Opened Time", "Closed Time",
                  "Drawer Name", "Employee Name"]


def _parse_pages(pages: list[dict]) -> list[dict]:
    """Turn brick pages into till rows:
    {unit_name, business_date, opened, closed, drawer, employee}.

    Layout rules (observed): the header row repeats on every page; a data row
    carrying a Unit Name cell starts (or, after a page break, continues) that
    unit's block; rows without one belong to the current unit. Footer rows
    contain 'Page N of M'.
    """
    till_rows: list[dict] = []
    current_unit = None
    for page in pages:
        cells: list[dict] = []
        _walk_bricks(page.get("brick"), cells)
        col_lefts: list[int] | None = None
        for row in _cells_to_rows(cells):
            texts = [c["text"].strip() for c in row]
            if texts[:1] == ["Unit Name"]:
                col_lefts = [c["left"] for c in row]
                continue
            if col_lefts is None:
                continue  # title block above the header
            if any("Page " in t and " of " in t for t in texts):
                continue  # footer
            # Map each cell to the nearest header column
            mapped: dict[str, str] = {}
            for c in row:
                idx = min(range(len(col_lefts)),
                          key=lambda i: abs(c["left"] - col_lefts[i]))
                mapped[HEADER_COLUMNS[idx]] = c["text"].strip()
            unit = mapped.get("Unit Name")
            if unit:
                current_unit = unit
            opened = mapped.get("Opened Time", "")
            closed = mapped.get("Closed Time", "")
            if not (opened or closed) or current_unit is None:
                continue
            till_rows.append({
                "unit_name": current_unit,
                "business_date": mapped.get("Business Date", ""),
                "opened": opened,
                "closed": closed,
                "drawer": mapped.get("Drawer Name", ""),
                "employee": mapped.get("Employee Name", ""),
            })
    return till_rows


def fetch_till_rows(company: dict, username: str, password: str,
                    report_date: date, headless: bool = True) -> list[dict]:
    """Log in and pull the Tills report for one business day.

    Returns raw drawer rows (one per till session); aggregation to
    per-store earliest/latest happens in analyze.py.
    """
    base = f"https://dc01.rmdatacentral.com/{company['portal']}"
    api = f"{base}/feed/allreports/reportdetail/api"
    report_id = company["report_id"]
    d = report_date.strftime("%Y-%m-%dT00:00:00")
    tzoffset = _tzoffset_hours(company.get("timezone", "America/Los_Angeles"),
                               report_date)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.set_default_timeout(LOGIN_TIMEOUT_MS)
        page.set_default_navigation_timeout(LOGIN_TIMEOUT_MS)

        page.goto(f"{base}/login")
        page.get_by_role("combobox", name="Enter username").fill(username)
        page.get_by_role("textbox", name="Enter password").fill(password)
        page.get_by_role("button", name="Sign In").click()
        page.wait_for_url(lambda url: "login" not in url)

        rq = ctx.request  # shares the logged-in cookies

        prep = rq.post(f"{api}/Reports/{report_id}/prepare", data={
            "@FromDate": d, "@ThruDate": d, "@UnitID": "null",
            "@GroupID": company["group_id"], "phone": "false",
            "tzoffset": tzoffset,
        })
        prep_data = _result(prep, "prepare")
        result_id = prep_data["ResultID"]

        opened = rq.post(f"{api}/DXXRDV", form={
            "actionKey": "openReport", "arg": result_id,
            "dxversions": DXVERSIONS,
        })
        open_data = _result(opened, "openReport")
        doc_id = open_data["documentId"]

        bs_arg = json.dumps({
            "documentId": doc_id,
            "firstPageRequest": {"pageIndex": 0, "documentId": doc_id,
                                 "resolution": 96, "includeBricks": True},
            "isFirstRequest": True,
            "timeOut": BUILD_POLL_SECONDS * 1000,
        })
        status = rq.post(f"{api}/DXXRDV", form={
            "actionKey": "getBuildStatus",
            "arg": urllib.parse.quote(bs_arg),
            "dxversions": DXVERSIONS,
        }, timeout=(BUILD_POLL_SECONDS + 30) * 1000)
        bs_data = _result(status, "getBuildStatus")
        if not bs_data.get("completed"):
            raise PortalError(
                f"report build not completed: progress={bs_data.get('progress')}")

        page_count = bs_data.get("pageCount") or open_data.get("pageCount") or 1
        pages = []
        first = bs_data.get("firstPageResponse")
        if first and first.get("brick"):
            pages.append(first)
        start = 1 if pages else 0
        for i in range(start, page_count):
            arg = json.dumps({"pageIndex": i, "documentId": doc_id,
                              "resolution": 96, "includeBricks": True})
            resp = rq.get(f"{api}/DXXRDV", params={
                "actionKey": "getPage", "unifier": str(uuid.uuid4()),
                "arg": arg,
            })
            page_data = _result(resp, f"getPage {i}")
            if page_data.get("brick") is None:
                raise PortalError(f"page {i} returned no bricks")
            pages.append(page_data)

        browser.close()

    return _parse_pages(pages)
