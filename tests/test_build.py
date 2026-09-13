"""Dashboard payload derivations (build_site.build_payload)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_site  # noqa: E402
import scrape  # noqa: E402
from synthetic import make_history  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"


def rows(dcs):
    return [{k: scrape._fmt(v) for k, v in scrape.asdict(r).items()} for r in dcs]


def test_single_day_payload():
    plans = scrape.parse_catalog((FIX / "floor-plans_2026-09-13.html").read_text())
    units = scrape.parse_feed(json.loads((FIX / "feed_2026-09-13.json").read_text()))
    p, u = scrape.build_rows(plans, units, "2026-09-13", "t")
    pay = build_site.build_payload(rows(p), rows(u), generated_at="2026-09-13T15:00:00+00:00")
    assert pay["latest"] == pay["first"] == "2026-09-13" and pay["n_days"] == 1
    assert len(pay["plans"]) == 21 and len(pay["units"]) == 30
    d2 = next(x for x in pay["plans"] if x["slug"] == "denver-2")
    assert d2["listed"] and d2["price"] == 2275 and d2["n_units"] == 3 and d2["d1"] is None and d2["d7"] is None
    assert d2["series"] == [["2026-09-13", 2275, 3]]
    d1 = next(x for x in pay["plans"] if x["slug"] == "denver-1")
    assert not d1["listed"] and d1["price"] is None and d1["series"] == [["2026-09-13", None, 0]]
    u = next(x for x in pay["units"] if x["unit"] == "5120")
    assert u["listed"] and u["censored"] and u["days_listed"] == 1 and u["price"] == 2275 and u["d_first"] == 0
    assert u["floor"] is None and u["amenities"]
    assert next(x for x in pay["units"] if x["unit"] == "3420")["floor"] == 4
    day = pay["daily"][0]
    assert day["total"] == 30 and day["counts"] == {"0": 3, "1": 9, "2": 13, "3": 5}
    assert day["cheapest"] == {"0": 2275, "1": 2520, "2": 3001, "3": 4205}
    # stable colour index within a bedroom group, in sqft order
    studios = [x for x in pay["plans"] if x["beds"] == 0]
    assert [x["k"] for x in studios] == [0, 1]


def test_multi_day_deltas_gone_and_censoring():
    p, u = make_history(45)
    pay = build_site.build_payload(rows(p), rows(u))
    assert pay["n_days"] == 45 and pay["first"] == "2026-07-31" and pay["latest"] == "2026-09-13"
    # deltas are today's price minus the last priced observation on/before latest-k days
    plan = next(x for x in pay["plans"] if x["slug"] == "denver-2")
    series = {d: v for d, v, _ in plan["series"]}
    def price_at(t):
        best = None
        for d in sorted(series):
            if d > t: break
            if series[d] is not None: best = series[d]
        return best
    assert plan["d1"] == plan["price"] - price_at("2026-09-12")
    assert plan["d7"] == plan["price"] - price_at("2026-09-06")
    assert plan["d30"] == plan["price"] - price_at("2026-08-14")
    # Eaton 1 dropped out on days 9-10 -> its series has None gaps and n_units 0 those days
    e1 = next(x for x in pay["plans"] if x["slug"] == "eaton-1")
    gap = [s for s in e1["series"] if s[0] in ("2026-08-09", "2026-08-10")]
    assert gap and all(s[1] is None and s[2] == 0 for s in gap)
    # ghosts leased: not listed today, last_seen before latest, days_listed spans their run
    gone = [x for x in pay["units"] if not x["listed"]]
    assert len(gone) == 4 and all(x["last_seen"] < pay["latest"] for x in gone)
    g = max(gone, key=lambda x: x["last_seen"])
    assert g["unit"] == "9000" and g["last_seen"] == "2026-09-05" and g["days_listed"] == 37 and g["censored"]
    # a unit that appeared after tracking started is not censored and counts its own days
    late = [x for x in pay["units"] if x["listed"] and not x["censored"]]
    assert late and all(x["first_seen"] > pay["first"] and x["days_listed"] == len(x["series"]) for x in late)
    # unit-level delta since first listing equals last minus first observed price
    for x in pay["units"]:
        priced = [v for _, v in x["series"] if v is not None]
        assert x["d_first"] == priced[-1] - priced[0] and x["low"] == min(priced) and x["high"] == max(priced)
    # listed units sort first, then by beds and price
    listed_flags = [x["listed"] for x in pay["units"]]
    assert listed_flags == sorted(listed_flags, reverse=True)
    assert len(pay["daily"]) == 45 and all(r["total"] == sum(r["counts"].values()) for r in pay["daily"])


def test_empty_data():
    pay = build_site.build_payload([], [])
    assert pay["latest"] is None and pay["plans"] == [] and pay["units"] == [] and pay["daily"] == []


def test_render_inlines_json_safely(tmp_path):
    tpl = "<script id=data type=application/json>__DATA__</script>"
    out = build_site.render(tpl, {"x": "</script><b>"})
    assert "</script><b>" not in out and "\\u003c/script>" in out


def test_main_builds_site(tmp_path):
    data = tmp_path / "data"; out = tmp_path / "site"
    from synthetic import write
    write(data, 10)
    rc = build_site.main(["--data-dir", str(data), "--out", str(out), "--template", str(Path(__file__).resolve().parents[1] / "dashboard_template.html")])
    assert rc == 0
    html = (out / "index.html").read_text()
    assert "__DATA__" not in html and '"n_days":10' in html
    assert (out / "data.json").exists() and (out / "data" / "prices.csv").exists() and (out / ".nojekyll").exists()


# ---- review follow-ups ----------------------------------------------------------
def test_unit_gap_breaks_the_line_and_counts_checks():
    p, u = make_history(20)
    # knock unit 5120 out of two mid-history days
    u = [r for r in u if not (r.unit == "5120" and r.date in ("2026-09-03", "2026-09-04"))]
    pay = build_site.build_payload(rows(p), rows(u))
    x = next(x for x in pay["units"] if x["unit"] == "5120")
    assert x["gaps"] == 2 and x["n_checks"] == 18 and x["days_listed"] == 20
    assert [s for s in x["series"] if s[0] in ("2026-09-03", "2026-09-04")] == [["2026-09-03", None], ["2026-09-04", None]]
    assert len(x["series"]) == 20


def test_deltas_after_a_scrape_gap():
    p, u = make_history(20)
    # drop every check between Sep 1 and Sep 11: d7 has nothing within tolerance, d1 compares with the last check
    keep = lambda r: not ("2026-09-01" <= r.date <= "2026-09-11")  # noqa: E731
    pay = build_site.build_payload(rows([r for r in p if keep(r)]), rows([r for r in u if keep(r)]))
    x = next(x for x in pay["plans"] if x["slug"] == "denver-2")
    series = {d: v for d, v, _ in x["series"]}
    assert x["d7"] is None                                   # target Sep 6 -> last check Aug 31 is 6 days stale
    assert x["d1"] == x["price"] - series["2026-09-12"]     # "since last check" = the previous check, whenever it was
    y = build_site.build_payload(rows([r for r in p if keep(r) and r.date != "2026-09-12"]), rows([r for r in u if keep(r) and r.date != "2026-09-12"]))
    z = next(x for x in y["plans"] if x["slug"] == "denver-2")
    assert z["d1"] == z["price"] - series["2026-08-31"]     # ...even if that was two weeks ago
    assert build_site.price_at([("2026-08-31", 100)], "2026-09-02") == 100
    assert build_site.price_at([("2026-08-31", 100)], "2026-09-04") is None


def test_daily_is_deduplicated():
    p, u = make_history(3)
    dup = rows(u) + rows(u[:5])          # five duplicate unit rows
    pay = build_site.build_payload(rows(p), dup)
    assert pay["daily"][-1]["total"] == sum(1 for r in u if r.date == pay["latest"])


def test_render_escapes_every_angle_bracket():
    out = build_site.render("<script id=data type=application/json>__DATA__</script>", {"x": "<!--<script>"})
    assert "<!--" not in out and "\\u003c!--\\u003cscript>" in out


def test_main_writes_header_only_csvs_when_data_is_missing(tmp_path):
    out = tmp_path / "site"
    rc = build_site.main(["--data-dir", str(tmp_path / "nodata"), "--out", str(out), "--template", str(Path(__file__).resolve().parents[1] / "dashboard_template.html")])
    assert rc == 0
    assert (out / "data" / "units.csv").read_text().startswith("date,scraped_at,plan,slug,unit,")
    assert '"latest":null' in (out / "index.html").read_text()
