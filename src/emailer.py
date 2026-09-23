"""Email building and sending (Gmail SMTP, same conventions as the lights bot).

Exception-only: an email goes out ONLY when a company has flags. A separate
plain failure email goes to the operator if the scrape/analysis blows up, so
silence always means "all clear", never "the bot died".
"""

import os
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SERVICE_CONTACT = os.environ.get("SERVICE_CONTACT", "ageiger@geigermgt.com")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

ISSUE_COLORS = {
    "LATE OPEN": "#D62300",
    "EARLY CLOSE": "#B54A00",
    "NO TILL DATA": "#7A0000",
    "MANAGER LATE IN": "#8A2BE2",
}


def send_email(to: str, subject: str, html_body: str):
    if DRY_RUN:
        print(f"\n{'=' * 60}")
        print(f"DRY RUN — To: {to}")
        print(f"Subject: {subject}")
        print(html_body)
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = to
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, to, msg.as_string())


def _fmt_date(d: date) -> str:
    return f"{d.strftime('%A, %B')} {d.day}, {d.year}"


def build_flags_email(company: dict, flags: list[dict], business_date: date,
                      region_name: str | None = None) -> tuple[str, str]:
    name = company["name"]
    if region_name:
        name = f"{name} · {region_name}"
    n = len(flags)
    region_part = f"{region_name} — " if region_name else ""
    subject = (f"TillWatch — {n} store{'s' if n != 1 else ''} flagged — "
               f"{region_part}{business_date.strftime('%b')} {business_date.day}")

    rows_html = ""
    for f in flags:
        color = ISSUE_COLORS.get(f["issue"], "#D62300")
        off = ""
        if f.get("minutes_off") is not None:
            mins = f["minutes_off"]
            off = " (under 1 min)" if mins < 1 else f" ({mins:g} min)"
        manager_html = ""
        if f.get("manager"):
            manager_html = f"""
        <tr>
          <td colspan="4" style="padding:2px 10px 10px;border-bottom:1px solid #eee;
              font-size:12px;color:#555;">&#128100; {f['manager']}</td>
        </tr>"""
        border = "none" if manager_html else "1px solid #eee"
        rows_html += f"""
        <tr>
          <td style="padding:10px;border-bottom:{border};">
            <b>#{f['store']}</b><br>
            <span style="font-size:12px;color:#666;">{f['name']}</span></td>
          <td style="padding:10px;border-bottom:{border};text-align:center;
              font-weight:bold;color:{color};">{f['issue']}{off}</td>
          <td style="padding:10px;border-bottom:{border};text-align:center;">{f['expected']}</td>
          <td style="padding:10px;border-bottom:{border};text-align:center;
              font-weight:bold;">{f['actual']}</td>
        </tr>{manager_html}"""

    grace = company.get("grace_minutes", 15)
    close_grace = company.get("close_grace_minutes", 0)
    live_used = company.get("_live_hours_used")
    n_stores = len(company.get("stores", []))
    if live_used:
        hours_src = f"Hours: live from bk.com ({live_used}/{n_stores} stores)"
    else:
        hours_src = "Hours: from config"
    if company.get("_till_history_checked"):
        hours_src += " &bull; Till times cross-checked against Till History"
    html = f"""
<!DOCTYPE html>
<html>
<head>
  <style>
    body {{ font-family: Arial, sans-serif; font-size: 14px; color: #333;
            max-width: 640px; margin: 0 auto; padding: 24px; }}
    .header {{ background: #D62300; color: white; padding: 16px 20px;
               border-radius: 6px 6px 0 0; }}
    .header h1 {{ margin: 0; font-size: 18px; font-weight: bold; }}
    .header p {{ margin: 4px 0 0; font-size: 13px; opacity: 0.85; }}
    table {{ width: 100%; border-collapse: collapse; border: 1px solid #ddd;
             border-top: none; }}
    th {{ background: #222; color: white; padding: 10px; font-size: 12px;
          text-align: center; text-transform: uppercase; letter-spacing: 0.05em; }}
    th:first-child {{ text-align: left; }}
    .footer {{ font-size: 11px; color: #aaa; margin-top: 20px; text-align: center; }}
  </style>
</head>
<body>
  <div class="header">
    <h1>TillWatch &mdash; {name}</h1>
    <p>Business day: {_fmt_date(business_date)} &nbsp;|&nbsp;
       Based on first till open / last till close vs. posted store hours</p>
  </div>
  <table>
    <tr>
      <th>Store</th>
      <th>Issue</th>
      <th>Expected</th>
      <th>Till Activity</th>
    </tr>
    {rows_html}
  </table>
  <div class="footer">
    Grace: {grace} min open / {close_grace} min close &bull; {hours_src} &bull;
    Times are local to each store &bull;
    Questions or unsubscribe: {SERVICE_CONTACT}
  </div>
</body>
</html>
"""
    return subject, html


def build_timecard_failure_email(company_name: str, business_date: date,
                                 error: str, flag_count: int) -> tuple[str, str]:
    """Operator heads-up when the timecard pull fails. The till alerts still
    went out (or all-clear stood), but manager attribution and the
    MANAGER LATE IN check were skipped for the day."""
    subject = (f"TillWatch timecards FAILED — {company_name} — "
               f"{business_date.strftime('%b')} {business_date.day}")
    till_line = (f"{flag_count} till flag(s) were sent without manager names."
                 if flag_count else
                 "Till data was all clear, so no flag email was sent.")
    html = (f"<p>TillWatch could not pull the timecard report for "
            f"<b>{company_name}</b> (business day {business_date.isoformat()}), "
            f"so the MANAGER LATE IN check did not run. {till_line}</p>"
            f"<p>Backfill later with "
            f"<code>python main.py --timecards {business_date.isoformat()}</code>.</p>"
            f"<pre>{error}</pre>")
    return subject, html


def _flag_line(f: dict) -> str:
    mins = f.get("minutes_off")
    off = "" if mins is None else (" (under 1 min)" if mins < 1 else f" ({mins:g} min)")
    return (f"#{f['store']} {f['issue']}{off} — expected {f['expected']}, "
            f"till activity {f['actual']}")


def build_cross_check_email(company_name: str, business_date: date,
                            discrepancies: list[dict], removed: list[dict],
                            added: list[dict]) -> tuple[str, str]:
    """Operator heads-up when the Till History report disagrees with the
    Earliest Open / Latest Close report. The alert already used the wider
    window (a drawer on either report proves the store was operating), so
    this is a record of PAR's report being wrong and of which flags that
    changed — nobody on the store lists sees it."""
    n = len({d["store"] for d in discrepancies})
    subject = (f"TillWatch cross-check — {company_name} — "
               f"{business_date.strftime('%b')} {business_date.day} — "
               f"Till History disagrees for {n} store{'s' if n != 1 else ''}")
    field_label = {"open": "First open", "close": "Last close",
                   "missing": "Whole store"}
    rows = "".join(
        f"<tr><td style='padding:4px 10px;'><b>#{d['store']}</b> "
        f"<span style='color:#666;'>{d['unit_name']}</span></td>"
        f"<td style='padding:4px 10px;'>{field_label.get(d['field'], d['field'])}</td>"
        f"<td style='padding:4px 10px;'>{d['primary']}</td>"
        f"<td style='padding:4px 10px;'><b>{d['history']}</b></td></tr>"
        for d in discrepancies)
    outcome = ""
    if removed:
        outcome += ("<p>Flags that would have gone out on the first/last report "
                    "alone, and were <b>not sent</b>:</p><ul>"
                    + "".join(f"<li>{_flag_line(f)}</li>" for f in removed)
                    + "</ul>")
    if added:
        outcome += ("<p>Flags that exist <b>only because of</b> Till History "
                    "(store was missing from the first/last report):</p><ul>"
                    + "".join(f"<li>{_flag_line(f)}</li>" for f in added)
                    + "</ul>")
    if not removed and not added:
        outcome = "<p>No flag changed as a result.</p>"
    html = (f"<p>For <b>{company_name}</b>, business day "
            f"{business_date.isoformat()}, the <i>Tills: Earliest Open / Latest "
            f"Close</i> report disagreed with <i>Till History</i>. TillWatch "
            f"used the wider window (Till History value in bold).</p>"
            f"<table style='border-collapse:collapse;font-size:13px;'>"
            f"<tr><th style='text-align:left;padding:4px 10px;'>Store</th>"
            f"<th style='text-align:left;padding:4px 10px;'>Field</th>"
            f"<th style='text-align:left;padding:4px 10px;'>Earliest/Latest report</th>"
            f"<th style='text-align:left;padding:4px 10px;'>Till History</th></tr>"
            f"{rows}</table>{outcome}")
    return subject, html


def build_cross_check_failure_email(company_name: str, business_date: date,
                                    error: str, flag_count: int) -> tuple[str, str]:
    """Operator heads-up when the Till History pull fails: the till alerts
    went out on the Earliest/Latest report alone, unverified."""
    subject = (f"TillWatch cross-check FAILED — {company_name} — "
               f"{business_date.strftime('%b')} {business_date.day}")
    till_line = (f"{flag_count} till flag(s) were sent on the first/last "
                 f"report alone — worth eyeballing against Till History."
                 if flag_count else
                 "The first/last report was all clear, so no flag email was sent.")
    html = (f"<p>TillWatch could not pull the Till History report for "
            f"<b>{company_name}</b> (business day {business_date.isoformat()}), "
            f"so the cross-check did not run. {till_line}</p>"
            f"<p>Compare later with "
            f"<code>python main.py --tills {business_date.isoformat()}</code>.</p>"
            f"<pre>{error}</pre>")
    return subject, html


def build_failure_email(company_name: str, business_date: date,
                        error: str) -> tuple[str, str]:
    subject = (f"TillWatch FAILED — {company_name} — "
               f"{business_date.strftime('%b')} {business_date.day}")
    html = (f"<p>TillWatch could not produce the till report for "
            f"<b>{company_name}</b> (business day {business_date.isoformat()}).</p>"
            f"<p>No late-open/early-close check was performed — do not assume "
            f"all clear.</p><pre>{error}</pre>")
    return subject, html
