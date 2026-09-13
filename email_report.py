#!/usr/bin/env python3
"""Email the latest rent snapshot for one bedroom count (default: 2-bedrooms).

Runs in CI right after scrape.py. Sends through Gmail SMTP when these are set
(repo Settings -> Secrets and variables -> Actions):

  MAIL_USERNAME   the Gmail address to send from (e.g. you@gmail.com)
  MAIL_PASSWORD   a Gmail *App Password* for that account (Google Account -> Security ->
                  2-Step Verification -> App passwords), never your real password
  MAIL_TO         where to send it; comma-separated for several addresses
  SMS_TO          optional: a carrier email-to-SMS address (e.g. 5085551234@vtext.com)
                  that gets a short plain-text version
  REPORT_BEDS     optional: bedroom count to report on (default 2)

Without credentials it just writes site/report.html and prints the text version,
so the workflow never fails for lack of email setup.

    python email_report.py            # build + send (if configured)
    python email_report.py --no-send  # build only
"""
from __future__ import annotations

import argparse
import html
import os
import smtplib
import sys
from datetime import date, datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import build_site

ROOT = Path(__file__).resolve().parent
SITE_URL = "https://sharkweekshane.github.io/bell-rent-watch/"
DATA_URL = "https://github.com/sharkweekshane/bell-rent-watch/blob/main/data/units.csv"


def money(n) -> str:
    return "–" if n is None else f"${n:,.0f}"


def signed(n) -> str:
    if n is None:
        return "–"
    if n == 0:
        return "0"
    return f"{'+' if n > 0 else '−'}${abs(n):,.0f}"


def fmt_date(iso: str | None, latest: str) -> str:
    if not iso:
        return "–"
    if iso <= latest:
        return "Now"
    d = date.fromisoformat(iso)
    return d.strftime("%b %-d") + (f", {d.year}" if d.year != date.fromisoformat(latest).year else "")


def status_short(s: str) -> str:
    s = (s or "").lower()
    if "notice" in s:
        return "Notice"
    if "not ready" in s:
        return "Vacant, not ready"
    if "vacant" in s:
        return "Vacant, ready"
    return s.title() or "–"


def build_report(payload: dict, beds: int) -> dict:
    """Everything the email needs, computed from the dashboard payload."""
    latest = payload["latest"]
    label = "Studio" if beds == 0 else f"{beds}-bedroom"
    units = [u for u in payload["units"] if u["beds"] == beds]
    listed = sorted([u for u in units if u["listed"]], key=lambda u: (u["price"] if u["price"] is not None else 10**9, u["plan"], u["unit"]))
    plans = [p for p in payload["plans"] if p["beds"] == beds]
    dates = [r["date"] for r in payload["daily"]]
    prev = dates[-2] if len(dates) > 1 else None
    new = [u for u in listed if u["first_seen"] == latest and prev is not None]
    gone = [u for u in units if not u["listed"] and prev is not None and u["last_seen"] == prev]
    moved = []
    for u in listed:
        if prev is None:
            continue
        before = next((v for d, v in reversed(u["series"]) if d < latest and v is not None), None)
        if before is not None and u["price"] is not None and before != u["price"]:
            moved.append((u, u["price"] - before))
    cheapest = listed[0] if listed else None
    return {"latest": latest, "prev": prev, "label": label, "beds": beds, "listed": listed, "plans": plans,
            "new": new, "gone": gone, "moved": moved, "cheapest": cheapest, "n_days": payload["n_days"]}


def render_text(r: dict) -> str:
    L = r["latest"]
    out = [f"Bell Westford {r['label']} rents — {date.fromisoformat(L).strftime('%b %-d, %Y')}",
           f"{len(r['listed'])} units listed across {sum(1 for p in r['plans'] if p['listed'])} of {len(r['plans'])} {r['label'].lower()} plans."]
    if r["prev"]:
        bits = []
        if r["moved"]:
            bits.append("Price changes since last check: " + "; ".join(f"{u['plan']} #{u['unit']} {signed(d)} to {money(u['price'])}" for u, d in r["moved"]))
        if r["new"]:
            bits.append("New: " + "; ".join(f"{u['plan']} #{u['unit']} at {money(u['price'])}" for u in r["new"]))
        if r["gone"]:
            bits.append("Gone (leased or delisted): " + "; ".join(f"{u['plan']} #{u['unit']} (was {money(u['price'])})" for u in r["gone"]))
        out.append(" ".join(bits) if bits else "No changes since the last check.")
    out += ["", f"{'Plan':10}{'Unit':6}{'Floor':7}{'SqFt':7}{'Rent':9}{'Since listed':14}{'Available':13}Status"]
    for u in r["listed"]:
        out.append(f"{u['plan']:10}{u['unit']:6}{(str(u['floor']) if u['floor'] is not None else '-'):7}{(str(u['sqft']) if u['sqft'] else '-'):7}"
                   f"{money(u['price']):9}{signed(u['d_first']):14}{fmt_date(u['available_date'], L):13}{status_short(u['status'])}")
    out += ["", "By floor plan (lowest – highest across its units):"]
    for p in r["plans"]:
        out.append(f"  {p['name']:16}{(money(p['price']) + ' – ' + money(p['price_max'])) if p['listed'] else 'call for pricing':22}{p['n_units']} unit{'s' if p['n_units'] != 1 else ''}" if p["listed"] else f"  {p['name']:16}call for pricing")
    out += ["", "Rent is the lowest advertised rent at default lease terms; the high end of a plan's range is the lease-term-dependent maximum in the feed.",
            f"Dashboard: {SITE_URL}", f"Data: {DATA_URL}"]
    return "\n".join(out)


def render_sms(r: dict) -> str:
    c = r["cheapest"]
    s = f"Bell Westford {r['label']}: {len(r['listed'])} listed"
    if c:
        s += f", cheapest {money(c['price'])} ({c['plan']} #{c['unit']})"
    if r["moved"]:
        s += "; moved: " + ", ".join(f"#{u['unit']} {signed(d)}" for u, d in r["moved"][:4])
    if r["new"]:
        s += "; new: " + ", ".join(f"#{u['unit']} {money(u['price'])}" for u in r["new"][:3])
    if r["gone"]:
        s += "; gone: " + ", ".join(f"#{u['unit']}" for u in r["gone"][:3])
    return s[:300]


def render_html(r: dict) -> str:
    L = r["latest"]
    e = html.escape
    td = 'style="padding:6px 8px;border-bottom:1px solid #E2E8E4;white-space:nowrap"'
    tdr = 'style="padding:6px 8px;border-bottom:1px solid #E2E8E4;text-align:right;white-space:nowrap"'
    th = 'style="padding:6px 8px;border-bottom:1px solid #CFD8D3;text-align:left;color:#5B6A63;font-size:12px;font-weight:500"'
    thr = th.replace("text-align:left", "text-align:right")
    cls = lambda n: "color:#B23B2A" if (n or 0) > 0 else ("color:#1E7A4C" if (n or 0) < 0 else "color:#5B6A63")  # noqa: E731
    rows = "".join(
        f"<tr><td {td}><b>{e(u['plan'])}</b></td><td {td}>{e(u['unit'])}</td><td {td}>{u['floor'] if u['floor'] is not None else '–'}</td>"
        f"<td {tdr}>{u['sqft']:,}</td><td {tdr}><b>{money(u['price'])}</b></td><td {tdr.replace('white-space', cls(u['d_first']) + ';white-space')}>{signed(u['d_first'])}</td>"
        f"<td {td}>{fmt_date(u['available_date'], L)}</td><td {td}>{e(status_short(u['status']))}</td></tr>"
        for u in r["listed"])
    changes = ""
    if r["prev"]:
        items = [f"<li><b>{e(u['plan'])} #{e(u['unit'])}</b> <span style=\"{cls(d)}\">{signed(d)}</span> to {money(u['price'])}</li>" for u, d in r["moved"]]
        items += [f"<li>New: <b>{e(u['plan'])} #{e(u['unit'])}</b> at {money(u['price'])}</li>" for u in r["new"]]
        items += [f"<li>Gone: <b>{e(u['plan'])} #{e(u['unit'])}</b> (was {money(u['price'])})</li>" for u in r["gone"]]
        changes = f"<h3 style='font-size:14px;margin:16px 0 4px'>Since the last check</h3>" + (f"<ul style='margin:0;padding-left:18px'>{''.join(items)}</ul>" if items else "<p style='margin:0;color:#5B6A63'>No changes.</p>")
    plans = "".join(
        f"<tr><td style='padding:3px 16px 3px 0'>{e(p['name'])}</td><td style='padding:3px 16px 3px 0'>{(money(p['price']) + ' – ' + money(p['price_max'])) if p['listed'] else 'call for pricing'}</td>"
        f"<td style='color:#5B6A63'>{(str(p['n_units']) + ' unit' + ('s' if p['n_units'] != 1 else '')) if p['listed'] else ''}</td></tr>" for p in r["plans"])
    return f"""<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;color:#17231E;max-width:680px">
<h2 style="font-weight:600;margin:0 0 4px">Bell Westford {e(r['label'])} rents — {date.fromisoformat(L).strftime('%b %-d, %Y')}</h2>
<p style="margin:0 0 12px;color:#5B6A63">{len(r['listed'])} units listed across {sum(1 for p in r['plans'] if p['listed'])} of {len(r['plans'])} {e(r['label'].lower())} plans, cheapest first.</p>
{changes}
<table style="border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;margin-top:12px">
<thead><tr><th {th}>Plan</th><th {th}>Unit</th><th {th}>Floor</th><th {thr}>Sq ft</th><th {thr}>Rent</th><th {thr}>Since listed</th><th {th}>Available</th><th {th}>Status</th></tr></thead>
<tbody>{rows}</tbody></table>
<h3 style="font-size:14px;margin:18px 0 6px">By floor plan</h3>
<table style="border-collapse:collapse;font-variant-numeric:tabular-nums">{plans}</table>
<p style="color:#5B6A63;font-size:12px;margin:10px 0 16px">Rent is the lowest advertised rent at default lease terms; the high end of a plan's range is the lease-term-dependent maximum in the feed. “Since listed” compares with the unit's first appearance in this tracker.</p>
<p><a href="{SITE_URL}">Live dashboard</a> · <a href="{DATA_URL}">Full data (units.csv)</a></p>
</div>"""


def send(subject: str, text: str, html_body: str, to: list[str], user: str, password: str, sms_to: str | None, sms: str) -> None:
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, password)
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = user, ", ".join(to), subject
        msg.set_content(text)
        msg.add_alternative(html_body, subtype="html")
        s.send_message(msg)
        if sms_to:
            m2 = EmailMessage()
            m2["From"], m2["To"], m2["Subject"] = user, sms_to, ""
            m2.set_content(sms)
            s.send_message(m2)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-send", action="store_true", help="build the report but don't email it")
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data")
    ap.add_argument("--out", type=Path, default=ROOT / "site" / "report.html")
    ap.add_argument("--beds", type=int, default=int(os.environ.get("REPORT_BEDS", "2")))
    args = ap.parse_args(argv)

    payload = build_site.build_payload(build_site.read_csv(args.data_dir / "prices.csv"), build_site.read_csv(args.data_dir / "units.csv"))
    if not payload["latest"]:
        print("no data yet; nothing to report")
        return 0
    r = build_report(payload, args.beds)
    text, html_body, sms = render_text(r), render_html(r), render_sms(r)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html_body, encoding="utf-8")
    print(text)

    user, password, to = os.environ.get("MAIL_USERNAME"), os.environ.get("MAIL_PASSWORD"), os.environ.get("MAIL_TO")
    if args.no_send or not (user and password and to):
        print("\n(email not sent: " + ("--no-send" if args.no_send else "MAIL_USERNAME / MAIL_PASSWORD / MAIL_TO not set") + ")")
        return 0
    now_et = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
    subject = f"Bell Westford {r['label']} rents — {now_et.strftime('%a %b %-d, %-I:%M %p')} · {len(r['listed'])} listed" + (f", cheapest {money(r['cheapest']['price'])}" if r["cheapest"] else "")
    send(subject, text, html_body, [a.strip() for a in to.split(",") if a.strip()], user, password, os.environ.get("SMS_TO") or None, sms)
    print(f"\nemailed {to}" + (f" and texted {os.environ.get('SMS_TO')}" if os.environ.get("SMS_TO") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
