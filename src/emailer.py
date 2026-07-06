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


def build_flags_email(company: dict, flags: list[dict],
                      business_date: date) -> tuple[str, str]:
    name = company["name"]
    n = len(flags)
    subject = (f"TillWatch — {n} store{'s' if n != 1 else ''} flagged — "
               f"{business_date.strftime('%b')} {business_date.day}")

    rows_html = ""
    for f in flags:
        color = ISSUE_COLORS.get(f["issue"], "#D62300")
        off = f" ({f['minutes_off']} min)" if f.get("minutes_off") else ""
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
    Grace: {grace} min open / {close_grace} min close &bull; Times are local to each store &bull;
    Questions or unsubscribe: {SERVICE_CONTACT}
  </div>
</body>
</html>
"""
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
