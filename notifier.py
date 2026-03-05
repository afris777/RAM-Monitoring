"""
notifier.py — send the HTML price report via Gmail SMTP.

Credentials are read from environment variables:
    EMAIL_ADDRESS      — Gmail sender address
    EMAIL_APP_PASSWORD — Gmail App Password (not the account password)

Recipient is hardcoded to af@computerbase.de.
"""

import os
import smtplib
import ssl
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from report import get_report_data

RECIPIENT = "af@computerbase.de"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

_ROW_NEUTRAL = "background-color: #f9f9f9;"
_ROW_DOWN = "background-color: #d4edda;"   # light green — price dropped
_ROW_UP = "background-color: #f8d7da;"     # light red   — price rose

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<style>
  body  {{ font-family: Arial, sans-serif; font-size: 13px; color: #333; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th    {{ background-color: #343a40; color: #fff; padding: 8px 12px; text-align: left; }}
  td    {{ padding: 7px 12px; border-bottom: 1px solid #dee2e6; }}
  .num  {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .up   {{ color: #c0392b; font-weight: bold; }}
  .down {{ color: #1a7a3a; font-weight: bold; }}
</style>
</head>
<body>
<h2>RAM Preismonitor — {date}</h2>
<table>
  <thead>
    <tr>
      <th>Modell</th>
      <th class="num">Aktueller Preis</th>
      <th>Günstigster Shop</th>
      <th class="num">Preis vor 7 Tagen</th>
      <th class="num">Änderung</th>
    </tr>
  </thead>
  <tbody>
{rows}
  </tbody>
</table>
<p style="font-size:11px;color:#999;margin-top:16px;">
  Generiert am {date} &mdash; RAM Preismonitor
</p>
</body>
</html>
"""

_ROW_TEMPLATE = """\
    <tr style="{row_style}">
      <td>{model}</td>
      <td class="num">{price}</td>
      <td>{shop}</td>
      <td class="num">{price_7d}</td>
      <td class="num {change_class}">{change}</td>
    </tr>"""


def _fmt_price(value: float | None) -> str:
    if value is None:
        return "—"
    return f"€\u202f{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_change(pct: float | None) -> str:
    if pct is None:
        return "—"
    sign = "+" if pct > 0 else ""
    return f"{sign}{pct:.1f}\u202f%"


def build_html(report_data: list[dict]) -> str:
    today = date.today().strftime("%d.%m.%Y")
    row_parts: list[str] = []

    for row in report_data:
        pct = row.get("change_pct")
        if pct is not None and pct < 0:
            style = _ROW_DOWN
            css_class = "down"
        elif pct is not None and pct > 0:
            style = _ROW_UP
            css_class = "up"
        else:
            style = _ROW_NEUTRAL
            css_class = ""

        row_parts.append(
            _ROW_TEMPLATE.format(
                row_style=style,
                model=row["model_name"],
                price=_fmt_price(row.get("price")),
                shop=row.get("shop") or "—",
                price_7d=_fmt_price(row.get("price_7d")),
                change=_fmt_change(pct),
                change_class=css_class,
            )
        )

    return _HTML_TEMPLATE.format(date=today, rows="\n".join(row_parts))


# ---------------------------------------------------------------------------
# Sender
# ---------------------------------------------------------------------------

def send_report() -> None:
    sender = os.environ["EMAIL_ADDRESS"]
    password = os.environ["EMAIL_APP_PASSWORD"]

    report_data = get_report_data()
    html_body = build_html(report_data)

    today_str = date.today().strftime("%d.%m.%Y")
    subject = f"RAM Preismonitor \u2013 {today_str}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = RECIPIENT
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(sender, password)
        server.sendmail(sender, RECIPIENT, msg.as_string())

    print(f"Report sent to {RECIPIENT}")


if __name__ == "__main__":
    send_report()
