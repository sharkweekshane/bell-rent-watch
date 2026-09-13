"""email_report: change detection and rendering, on synthetic history."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_site  # noqa: E402
import email_report  # noqa: E402
from synthetic import make_history  # noqa: E402
from test_build import rows  # noqa: E402


def payload(n=12):
    p, u = make_history(n)
    return build_site.build_payload(rows(p), rows(u)), p, u


def test_report_lists_two_bed_units_cheapest_first_and_detects_changes():
    pay, p, u = payload(12)
    # force a price move, a new unit and a gone unit on the last day
    last = pay["latest"]
    u2 = []
    for r in u:
        if r.date == last and r.unit == "3420":
            r.rent_min += 75          # moved
        if r.date == last and r.unit == "5422":
            continue                  # gone today
        u2.append(r)
    u2.append(next(r for r in u if r.date == last and r.unit == "3210").__class__(**{**vars(next(r for r in u if r.date == last and r.unit == "3210")), "unit": "7777", "apartment_id": "7777"}))  # new
    pay = build_site.build_payload(rows(p), rows(u2))
    r = email_report.build_report(pay, 2)
    assert r["label"] == "2-bedroom" and all(x["beds"] == 2 and x["listed"] for x in r["listed"])
    prices = [x["price"] for x in r["listed"]]
    assert prices == sorted(prices)
    assert [x["unit"] for x, d in r["moved"] if x["unit"] == "3420"] == ["3420"] and next(d for x, d in r["moved"] if x["unit"] == "3420") == 75
    assert [x["unit"] for x in r["new"]] == ["7777"]
    assert [x["unit"] for x in r["gone"]] == ["5422"]
    text = email_report.render_text(r)
    assert "#3420 +$75" in text and "New: Aspen 2 #7777" in text and "Gone (leased or delisted): Ames 2 #5422" in text
    html = email_report.render_html(r)
    assert "<table" in html and "7777" in html and "Since the last check" in html
    sms = email_report.render_sms(r)
    assert sms.startswith("Bell Westford 2-bedroom:") and len(sms) <= 300 and "#3420 +$75" in sms


def test_report_single_day_has_no_change_section():
    pay, _, _ = payload(1)
    r = email_report.build_report(pay, 0)
    assert r["prev"] is None and r["label"] == "Studio" and r["moved"] == [] and r["new"] == [] and r["gone"] == []
    assert "Since the last check" not in email_report.render_html(r)
    assert "since last check" not in email_report.render_text(r).lower()


def test_main_without_credentials_writes_html_only(tmp_path, monkeypatch, capsys):
    for k in ("MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    from synthetic import write
    write(tmp_path / "data", 5)
    assert email_report.main(["--data-dir", str(tmp_path / "data"), "--out", str(tmp_path / "r.html")]) == 0
    assert (tmp_path / "r.html").read_text().startswith("<div")
    assert "email not sent" in capsys.readouterr().out
