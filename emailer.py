"""HTML + plain-text email builder and SMTP sender."""
from __future__ import annotations

import logging
import os
import re
import smtplib
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from typing import Any

import parser as p

log = logging.getLogger(__name__)

PAGES_URL_DEFAULT = "https://github.com"  # README will override


def _badge_action(t: str) -> str:
    if t == "Buy":
        return ('<span style="background:#dcfce7;color:#166534;padding:2px 8px;'
                'border-radius:10px;font-size:11px;font-weight:600">▲ BUY</span>')
    return ('<span style="background:#fee2e2;color:#991b1b;padding:2px 8px;'
            'border-radius:10px;font-size:11px;font-weight:600">▼ SELL</span>')


def _party_badge(p: str) -> str:
    colors = {"D": ("#dbeafe", "#1e40af"), "R": ("#fee2e2", "#991b1b"),
              "I": ("#e5e7eb", "#374151"), "?": ("#e5e7eb", "#374151"),
              "—": ("#e5e7eb", "#374151")}
    bg, fg = colors.get(p, ("#e5e7eb", "#374151"))
    return (f'<span style="background:{bg};color:{fg};padding:1px 6px;'
            f'border-radius:8px;font-size:10px;font-weight:600">{escape(p)}</span>')


def _trade_details(t: dict[str, Any]) -> str:
    if t["asset_type"] == "Option" and t.get("option_type"):
        parts = [t["option_type"]]
        if t.get("strike_price"):
            parts.append(f"${t['strike_price']} strike")
        if t.get("expiration_date"):
            parts.append(t["expiration_date"])
        if t.get("contracts"):
            parts.append(f"{t['contracts']} contracts")
        return " · ".join(parts)
    if t.get("quantity"):
        if t.get("thirteen_f_note"):
            return f"{t['quantity']} · {t['thirteen_f_note']}"
        return t["quantity"]
    return "—"


def _card_html(t: dict[str, Any]) -> str:
    border = "#d97706" if t.get("is_new") else "#e5e7eb"
    new_pill = ('<span style="background:#fde68a;color:#92400e;padding:2px 8px;'
                'border-radius:10px;font-size:10px;font-weight:700;'
                'margin-right:6px">🆕 NEW</span>') if t.get("is_new") else ""
    rows: list[tuple[str, str]] = []
    if t["asset_type"] == "Option":
        rows.append(("Asset type", f"Option — {t.get('option_type') or '?'}"))
        if t.get("strike_price"):
            rows.append(("Strike price", f"${t['strike_price']}"))
        if t.get("expiration_date"):
            rows.append(("Expiration", t["expiration_date"]))
        if t.get("contracts"):
            rows.append(("Contracts", t["contracts"]))
    elif t.get("quantity"):
        rows.append(("Shares", t["quantity"]))
    rows.append(("Amount", t["amount"]))
    rows.append(("Trade date", t["transaction_date"] or "—"))
    rows.append(("Filed date", t["disclosure_date"] or "—"))
    src_html = escape(t["source"])
    if t.get("filing_url"):
        src_html += (f' · <a href="{escape(t["filing_url"])}" '
                     'style="color:#2563eb">View filing →</a>')
    rows.append(("Source", src_html))
    row_html = "".join(
        f'<tr><td style="color:#6b7280;font-size:12px;padding:3px 12px 3px 0;'
        f'width:130px">{escape(k)}</td>'
        f'<td style="font-size:13px;padding:3px 0">{v}</td></tr>'
        for k, v in rows
    )
    return f"""
<table cellpadding="0" cellspacing="0" border="0" width="100%"
  style="border:1px solid {border};border-left:4px solid {border};
  border-radius:8px;margin:10px 0;background:#fff">
  <tr><td style="padding:14px 16px">
    <div style="margin-bottom:8px">
      {new_pill}{_badge_action(t['transaction_type'])}
      <span style="float:right;font-family:'DM Mono',monospace;
        font-weight:700;color:#0f1923">{escape(t['ticker'])}</span>
    </div>
    <div style="font-weight:600;color:#0f1923">
      {escape(t['person'])} {_party_badge(t['party_short'])}
      <span style="color:#6b7280;font-weight:400;font-size:12px">
        · {escape(t['role'])} · {escape(t['chamber'])}
      </span>
    </div>
    <div style="color:#374151;font-size:13px;margin:4px 0 10px">
      {escape(t['asset_description'] or '—')}
    </div>
    <hr style="border:none;border-top:1px solid #e5e7eb;margin:6px 0">
    <table cellpadding="0" cellspacing="0" border="0">{row_html}</table>
  </td></tr>
</table>"""


def _table_row(t: dict[str, Any], i: int) -> str:
    bg = "#f9f9f9" if i % 2 else "#ffffff"
    new_prefix = "🆕 " if t.get("is_new") else ""
    return f"""
<tr style="background:{bg}">
  <td style="padding:8px 10px;font-size:12px">
    {new_prefix}<b>{escape(t['person'])}</b> {_party_badge(t['party_short'])}
    <div style="color:#6b7280;font-size:10px">{escape(t['role'])}</div>
  </td>
  <td style="padding:8px 10px;font-family:'DM Mono',monospace;font-weight:700;
    font-size:12px">{escape(t['ticker'])}</td>
  <td style="padding:8px 10px">{_badge_action(t['transaction_type'])}</td>
  <td style="padding:8px 10px;font-size:12px">
    {escape((t['asset_description'] or '')[:50])}
    {'<span style="background:#fef3c7;color:#92400e;padding:1px 6px;border-radius:6px;font-size:10px;margin-left:4px">OPT</span>' if t['asset_type'] == 'Option' else ''}
  </td>
  <td style="padding:8px 10px;font-size:12px;color:#374151">{escape(_trade_details(t))}</td>
  <td style="padding:8px 10px;font-size:12px;color:#374151">{escape(t['amount'])}</td>
  <td style="padding:8px 10px;font-size:12px;color:#6b7280">{escape(t['transaction_date'] or '—')}</td>
  <td style="padding:8px 10px;font-size:12px;color:#6b7280">{escape(t['disclosure_date'] or '—')}</td>
</tr>"""


def build_email_html(trades: list[dict[str, Any]],
                     asch: dict[str, Any], pages_url: str = PAGES_URL_DEFAULT) -> tuple[str, str]:
    today = date.today().isoformat()
    new_trades = [t for t in trades if t.get("is_new")]
    new_trades.sort(key=lambda t: p.sort_key(t, prefer_new=False))
    total = len(trades)
    subject_n = len(new_trades)

    if subject_n > 0:
        subject = (f"🚨 {subject_n} New Whale Trade"
                   f"{'s' if subject_n != 1 else ''} Detected — {today}")
    else:
        subject = f"📊 Weekly Whale Trade Summary — {today}"

    header = f"""
<div style="background:#0f1923;color:#fff;padding:24px 28px">
  <div style="font-size:22px;font-weight:800">🐋 WHALE TRADE ALERTS</div>
  <div style="opacity:0.75;font-size:13px;margin-top:4px">
    {today} · {subject_n} new trade{'s' if subject_n != 1 else ''} ·
    {total} tracked in last 12 months
  </div>
</div>"""

    new_section = ""
    if new_trades:
        # Cap cards so a backfill run (e.g. first successful run with thousands
        # of unseen trades) doesn't produce a multi-megabyte email.
        CARD_CAP = 25
        cards = "".join(_card_html(t) for t in new_trades[:CARD_CAP])
        overflow = ""
        if len(new_trades) > CARD_CAP:
            overflow = (
                f'<div style="padding:8px 0;color:#6b7280;font-size:12px;'
                f'text-align:center">Showing top {CARD_CAP} of {len(new_trades)} '
                f'new trades by amount — see the table below or '
                f'<a href="{escape(pages_url)}" style="color:#2563eb">'
                f'the dashboard</a> for the rest.</div>'
            )
        new_section = f"""
<div style="padding:20px 28px;background:#fff">
  <div style="border-left:4px solid #dc2626;padding-left:10px;margin-bottom:12px">
    <h2 style="margin:0;color:#0f1923;font-size:18px">🚨 New Trades Just Filed</h2>
  </div>
  {cards}{overflow}
</div>"""

    if asch.get("has_new_filing"):
        asch_cards = "".join(_card_html(t) for t in trades
                             if t["source"] == "13F" and t.get("is_new"))
        asch_section = f"""
<div style="padding:0 28px 20px;background:#fff">
  <h2 style="color:#0f1923;font-size:16px;margin:18px 0 8px">
    Aschenbrenner Fund — New 13F filing
  </h2>{asch_cards}
</div>"""
    else:
        last = asch.get("last_filing_date") or "unknown"
        n = asch.get("positions_count", 0)
        tickers = ", ".join(asch.get("top_tickers", [])[:6]) or "—"
        asch_section = f"""
<div style="padding:0 28px 20px;background:#fff">
  <div style="background:#f3f4f6;border-radius:8px;padding:14px 16px;
    color:#374151;font-size:13px">
    📁 <b>Aschenbrenner Fund</b> — No new 13F filing today.<br>
    Last filing: <b>{escape(last)}</b>. Tracking <b>{n}</b> positions
    across {escape(tickers)}.
  </div>
</div>"""

    sorted_trades = sorted(trades, key=p.sort_key)
    shown = sorted_trades[:50]
    rows = "".join(_table_row(t, i) for i, t in enumerate(shown))
    overflow_note = ""
    if len(sorted_trades) > 50:
        overflow_note = (f'<div style="text-align:center;padding:8px;color:#6b7280;'
                         f'font-size:12px">Showing 50 of {len(sorted_trades)} trades. '
                         f'<a href="{escape(pages_url)}" style="color:#2563eb">'
                         f'View full dashboard →</a></div>')

    table_section = f"""
<div style="padding:0 28px 24px;background:#fff">
  <h2 style="color:#0f1923;font-size:16px;margin:18px 0 8px">All Recent Trades</h2>
  <table cellpadding="0" cellspacing="0" border="0" width="100%"
    style="border-collapse:collapse;border:1px solid #e5e7eb;border-radius:8px;
    overflow:hidden">
    <thead><tr style="background:#0f1923;color:#fff">
      <th align="left" style="padding:10px;font-size:11px">Actor</th>
      <th align="left" style="padding:10px;font-size:11px">Ticker</th>
      <th align="left" style="padding:10px;font-size:11px">Action</th>
      <th align="left" style="padding:10px;font-size:11px">Asset</th>
      <th align="left" style="padding:10px;font-size:11px">Trade Details</th>
      <th align="left" style="padding:10px;font-size:11px">Amount</th>
      <th align="left" style="padding:10px;font-size:11px">Trade Date</th>
      <th align="left" style="padding:10px;font-size:11px">Filed Date</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
  {overflow_note}
</div>"""

    footer = f"""
<div style="padding:18px 28px;background:#f3f4f6;color:#6b7280;
  font-size:11px;line-height:1.5">
  Data sourced from STOCK Act disclosures (House/Senate PTRs), OGE Form 278-T
  (Executive), and SEC EDGAR 13F filings (Aschenbrenner Fund). Trades must be
  reported within 45 days. Presidential trades via OGE are less frequent and
  may lag. This is not financial advice.<br><br>
  <a href="{escape(pages_url)}" style="color:#2563eb">
    View live dashboard →</a>
</div>"""

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>{escape(subject)}</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,
  Inter,sans-serif;margin:0;background:#f3f4f6;color:#0f1923">
<div style="max-width:980px;margin:0 auto;background:#fff">
  {header}{new_section}{asch_section}{table_section}{footer}
</div></body></html>"""

    return subject, html


def build_email_text(trades: list[dict[str, Any]], asch: dict[str, Any]) -> str:
    today = date.today().isoformat()
    new = [t for t in trades if t.get("is_new")]
    lines = [f"WHALE TRADE ALERTS — {today}",
             f"{len(new)} new trade(s) · {len(trades)} tracked in last 12 months",
             "=" * 70, ""]
    if new:
        lines.append("NEW TRADES")
        lines.append("-" * 70)
        for t in sorted(new, key=lambda x: p.sort_key(x, prefer_new=False)):
            lines.append(
                f"[{t['transaction_type']:4}] {t['ticker']:6} "
                f"{t['person']} ({t['party_short']}) — {t['amount']}"
            )
            lines.append(f"    {t['asset_description']}")
            lines.append(f"    {_trade_details(t)}")
            lines.append(f"    Trade: {t['transaction_date']}  Filed: {t['disclosure_date']}")
            lines.append(f"    Source: {t['source']}  {t.get('filing_url','')}")
            lines.append("")
    if not asch.get("has_new_filing"):
        lines.append(f"Aschenbrenner Fund — No new 13F. Last filing: "
                     f"{asch.get('last_filing_date') or 'unknown'}. "
                     f"{asch.get('positions_count', 0)} positions tracked.")
        lines.append("")
    lines.append("All recent trades (top 30 by amount):")
    lines.append("-" * 70)
    for t in sorted(trades, key=p.sort_key)[:30]:
        prefix = "NEW " if t.get("is_new") else "    "
        lines.append(f"{prefix}{t['transaction_date']:10} {t['ticker']:6} "
                     f"{t['transaction_type']:4} {t['amount']:24} {t['person']}")
    return "\n".join(lines)


def should_send_email(new_count: int) -> bool:
    if new_count > 0:
        return True
    if date.today().weekday() == 0:  # Monday → weekly summary
        return True
    return False


def _parse_recipients(raw: str | None) -> list[str]:
    if not raw:
        return []
    # Accept commas, semicolons, or whitespace as separators
    parts = re.split(r"[,;\s]+", raw.strip())
    return [p for p in (s.strip() for s in parts) if p]


def send_email(subject: str, html: str, text: str) -> bool:
    user = os.environ.get("GMAIL_USER")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    recipients = _parse_recipients(os.environ.get("ALERT_EMAIL"))
    if not (user and pw and recipients):
        log.warning("Email creds missing or no recipients; skipping send")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
            s.starttls()
            s.login(user, pw)
            s.sendmail(user, recipients, msg.as_string())
        log.info("Email sent to %d recipient(s) (subject=%r)", len(recipients), subject)
        return True
    except Exception as exc:
        log.exception("SMTP send failed: %s", exc)
        return False


def send_failure_alert(traceback_text: str) -> None:
    today = date.today().isoformat()
    subject = f"⚠️ Whale Trade Alerts — Agent Failed {today}"
    body = (f"The Whale Trade Alerts agent failed on {today}.\n\n"
            f"Traceback:\n{traceback_text}\n")
    html = (f"<h2>Agent Failure — {today}</h2>"
            f"<pre style='background:#f3f4f6;padding:12px;font-size:12px'>"
            f"{escape(traceback_text)}</pre>")
    send_email(subject, html, body)
