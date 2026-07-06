"""Portal access: Playwright login + the report API flow.

PAR report-detail pages are DevExpress web reports. Rather than scraping the
rendered viewer (slow, shadow-DOM, paginated), we replicate the API calls the
viewer itself makes — discovered by capturing network traffic:

    POST {portal}/feed/allreports/reportdetail/api/Reports/{report_id}/prepare
        JSON {"@FromDate", "@ThruDate", "@UnitID": "null", "@GroupID", ...}
        -> Data.ResultID
    POST .../api/DXXRDV  actionKey=openReport  arg=<ResultID>
        -> documentId, pageCount
    POST .../api/DXXRDV  actionKey=getBuildStatus  (long-poll until completed)
    GET  .../api/DXXRDV  actionKey=getPage  arg={"pageIndex", "documentId",
                                                 "resolution": 96,
                                                 "includeBricks": true}
        -> result.brick: a tree of positioned "bricks" whose leaf nodes hold
           every table cell's text. We reconstruct rows from coordinates.

Only the login itself touches the UI (the login form is simple and stable).
Everything else is plain HTTP via the logged-in browser context's cookies.

Two reports are used:
  - company["report_id"]           Tills: Earliest Open / Latest Close
  - company["timecard_report_id"]  Payroll - Daily Time Card Review w/ Totals
    (only fetched when a flag needs manager attribution)

NOTE: the timecard report's prepare rejects a specific @UnitID with
"You do not have right to view payroll data" but accepts the group-wide
request — so we always pull group-wide and filter locally.
"""

import json
import re
import urllib.parse
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

DXVERSIONS = json.dumps(
    {"analytics": "24.1.6", "devextreme": "24.1.6", "reporting": "24.1.6"}
)
LOGIN_TIMEOUT_MS = 120_000
PREPARE_TIMEOUT_MS = 300_000   # group-wide payroll builds are slow
BUILD_POLL_SECONDS = 105       # server-side long-poll timeout the real viewer uses
BUILD_POLL_ATTEMPTS = 4


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


# ---------------------------------------------------------------------------
# Brick geometry -> rows
# ---------------------------------------------------------------------------

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


def _page_rows(pages: list[dict]) -> list[list[dict]]:
    rows = []
    for page in pages:
        cells: list[dict] = []
        _walk_bricks(page.get("brick"), cells)
        rows.extend(_cells_to_rows(cells))
    return rows


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class PortalSession:
    """One logged-in portal session; fetch any report as brick pages."""

    def __init__(self, company: dict, username: str, password: str,
                 headless: bool = True):
        self.company = company
        self.username = username
        self.password = password
        self.headless = headless
        self.base = f"https://dc01.rmdatacentral.com/{company['portal']}"
        self.api = f"{self.base}/feed/allreports/reportdetail/api"

    def __enter__(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self._ctx = self._browser.new_context()
        page = self._ctx.new_page()
        page.set_default_timeout(LOGIN_TIMEOUT_MS)
        page.set_default_navigation_timeout(LOGIN_TIMEOUT_MS)
        page.goto(f"{self.base}/login")
        page.get_by_role("combobox", name="Enter username").fill(self.username)
        page.get_by_role("textbox", name="Enter password").fill(self.password)
        page.get_by_role("button", name="Sign In").click()
        page.wait_for_url(lambda url: "login" not in url)
        self._rq = self._ctx.request
        return self

    def __exit__(self, *exc):
        self._browser.close()
        self._pw.stop()
        return False

    def fetch_report_pages(self, report_id: str, report_date: date) -> list[dict]:
        """prepare -> openReport -> getBuildStatus -> getPage*: brick pages."""
        d = report_date.strftime("%Y-%m-%dT00:00:00")
        tzoffset = _tzoffset_hours(
            self.company.get("timezone", "America/Los_Angeles"), report_date)

        prep = self._rq.post(f"{self.api}/Reports/{report_id}/prepare", data={
            "@FromDate": d, "@ThruDate": d, "@UnitID": "null",
            "@GroupID": self.company["group_id"], "phone": "false",
            "tzoffset": tzoffset,
        }, timeout=PREPARE_TIMEOUT_MS)
        result_id = _result(prep, "prepare")["ResultID"]

        opened = self._rq.post(f"{self.api}/DXXRDV", form={
            "actionKey": "openReport", "arg": result_id,
            "dxversions": DXVERSIONS,
        }, timeout=PREPARE_TIMEOUT_MS)
        open_data = _result(opened, "openReport")
        doc_id = open_data["documentId"]

        bs_data = None
        first_request = True
        for _ in range(BUILD_POLL_ATTEMPTS):
            bs_arg = json.dumps({
                "documentId": doc_id,
                "firstPageRequest": {"pageIndex": 0, "documentId": doc_id,
                                     "resolution": 96, "includeBricks": True},
                "isFirstRequest": first_request,
                "timeOut": BUILD_POLL_SECONDS * 1000,
            })
            status = self._rq.post(f"{self.api}/DXXRDV", form={
                "actionKey": "getBuildStatus",
                "arg": urllib.parse.quote(bs_arg),
                "dxversions": DXVERSIONS,
            }, timeout=(BUILD_POLL_SECONDS + 30) * 1000)
            bs_data = _result(status, "getBuildStatus")
            if bs_data.get("completed"):
                break
            first_request = False
        if not bs_data or not bs_data.get("completed"):
            raise PortalError(
                f"report {report_id} build not completed: "
                f"progress={bs_data.get('progress') if bs_data else '?'}")

        page_count = bs_data.get("pageCount") or open_data.get("pageCount") or 1
        pages = []
        first = bs_data.get("firstPageResponse")
        if first and first.get("brick"):
            pages.append(first)
        start = 1 if pages else 0
        for i in range(start, page_count):
            arg = json.dumps({"pageIndex": i, "documentId": doc_id,
                              "resolution": 96, "includeBricks": True})
            resp = self._rq.get(f"{self.api}/DXXRDV", params={
                "actionKey": "getPage", "unifier": str(uuid.uuid4()),
                "arg": arg,
            }, timeout=PREPARE_TIMEOUT_MS)
            page_data = _result(resp, f"getPage {i}")
            if page_data.get("brick") is None:
                raise PortalError(f"page {i} returned no bricks")
            pages.append(page_data)
        return pages

    # -- Tills: Earliest Open / Latest Close --------------------------------

    def fetch_till_rows(self, report_date: date) -> list[dict]:
        """Raw drawer rows: {unit_name, business_date, opened, closed,
        drawer, employee}. Aggregation happens in analyze.py."""
        pages = self.fetch_report_pages(self.company["report_id"], report_date)
        return _parse_till_pages(pages)

    # -- Payroll - Daily Time Card Review w/ Totals --------------------------

    def fetch_timecard_shifts(self, report_date: date) -> list[dict]:
        """Raw shift rows: {unit_name, employee, title, clock_in, clock_out}."""
        pages = self.fetch_report_pages(
            self.company["timecard_report_id"], report_date)
        return _parse_timecard_pages(pages)


# ---------------------------------------------------------------------------
# Tills report parsing
# ---------------------------------------------------------------------------

TILL_COLUMNS = ["Unit Name", "Business Date", "Opened Time", "Closed Time",
                "Drawer Name", "Employee Name"]


def _parse_till_pages(pages: list[dict]) -> list[dict]:
    """Layout rules (observed): the header row repeats on every page; a data
    row carrying a Unit Name cell starts (or, after a page break, continues)
    that unit's block; rows without one belong to the current unit. Footer
    rows contain 'Page N of M'."""
    till_rows: list[dict] = []
    current_unit = None
    col_lefts: list[int] | None = None
    for page in pages:
        cells: list[dict] = []
        _walk_bricks(page.get("brick"), cells)
        col_lefts = None
        for row in _cells_to_rows(cells):
            texts = [c["text"].strip() for c in row]
            if texts[:1] == ["Unit Name"]:
                col_lefts = [c["left"] for c in row]
                continue
            if col_lefts is None:
                continue  # title block above the header
            if any("Page " in t and " of " in t for t in texts):
                continue  # footer
            mapped: dict[str, str] = {}
            for c in row:
                idx = min(range(len(col_lefts)),
                          key=lambda i: abs(c["left"] - col_lefts[i]))
                mapped[TILL_COLUMNS[idx]] = c["text"].strip()
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


# ---------------------------------------------------------------------------
# Timecard report parsing
# ---------------------------------------------------------------------------

_TIME_RE = re.compile(r"^\d{1,2}:\d{2} [AP]M$")
_WEEKDAYS = {"Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"}

# x-coordinates observed on the report (96 dpi): title ~169, clock in ~334,
# clock out ~416. Employee-name rows sit at left ~47; unit headers carry a
# "From:" label cell.
_TITLE_LEFT_MIN, _TITLE_LEFT_MAX = 150, 320


def _parse_timecard_pages(pages: list[dict]) -> list[dict]:
    """Layout rules (observed):
      - unit header:   '<unit name>' + 'From:' + date + 'Through:' + date
      - employee row:  single cell at far left, 'Name, First (id)'
      - shift row:     weekday, date, ?, job title, clock in, clock out, hours…
      - totals rows:   contain a 'Totals' cell — skipped
      - footer:        'Prepared by … Page N of M'
    """
    shifts: list[dict] = []
    current_unit = None
    current_employee = None
    for row in _page_rows(pages):
        texts = [c["text"].strip() for c in row]
        if any(t == "From:" for t in texts):
            current_unit = texts[0]
            continue
        if any(t == "Totals" for t in texts):
            continue
        if any(t.startswith("Prepared by") or ("Page " in t and " of " in t)
               for t in texts):
            continue
        if len(row) == 1 and row[0]["left"] < 100 and texts[0]:
            current_employee = texts[0]
            continue
        if texts and texts[0] in _WEEKDAYS and current_unit and current_employee:
            times = [c for c in row if _TIME_RE.match(c["text"].strip())]
            title = next((c["text"].strip() for c in row
                          if _TITLE_LEFT_MIN <= c["left"] < _TITLE_LEFT_MAX
                          and not _TIME_RE.match(c["text"].strip())
                          and not c["text"].strip().isdigit()
                          and "/" not in c["text"]), "")
            clock_in = times[0]["text"].strip() if times else ""
            clock_out = times[1]["text"].strip() if len(times) > 1 else ""
            if clock_in:
                shifts.append({
                    "unit_name": current_unit,
                    "employee": current_employee,
                    "title": title,
                    "clock_in": clock_in,
                    "clock_out": clock_out,
                })
    return shifts
