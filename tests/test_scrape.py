"""Scraper tests against the real feed + page captured on 2026-09-13. No network."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scrape  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
FEED = json.loads((FIX / "feed_2026-09-13.json").read_text())
PAGE = (FIX / "floor-plans_2026-09-13.html").read_text()


# ---- small parsers -----------------------------------------------------------
def test_slugify():
    assert scrape.slugify("Denver 2") == "denver-2"
    assert scrape.slugify("Townhome") == "townhome"
    assert scrape.slugify("  Breckenridge  1 ") == "breckenridge-1"


@pytest.mark.parametrize("raw,expected", [
    ("7/2/2026", "2026-07-02"), ("12/31/26", "2026-12-31"), ("2026-07-02", "2026-07-02"),
    ("2026-07-02T00:00:00", "2026-07-02"), ("", None), (None, None), ("2/30/2026", None), ("soon", None),
])
def test_parse_mdy(raw, expected):
    assert scrape.parse_mdy(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("3106.00", 3106), ("$2,275", 2275), (2490, 2490), ("2490.49", 2490), ("", None), (None, None),
    ("0", None), ("-1", None), ("99999999", None), ("abc", None),
])
def test_parse_dollars(raw, expected):
    assert scrape.parse_dollars(raw) == expected


def test_parse_floor_and_amenities():
    ams = scrape.split_amenities("4th Floor^Balcony^Premium View^Maple White^Botticino")
    assert ams == ["4th Floor", "Balcony", "Premium View", "Maple White", "Botticino"]
    assert scrape.parse_floor(ams) == 4
    assert scrape.parse_floor(["Garage", "1st Floor"]) == 1
    assert scrape.parse_floor(["Garage"]) is None
    assert scrape.split_amenities("") == [] and scrape.split_amenities(None) == []


def test_price_text():
    assert scrape.price_text(2275, 4511) == "$2,275 - $4,511"
    assert scrape.price_text(2275, 2275) == "$2,275"
    assert scrape.price_text(2275, None) == "$2,275"
    assert scrape.price_text(None, None) == "call for pricing"


# ---- feed ---------------------------------------------------------------------
def test_parse_feed_real_fixture():
    units = scrape.parse_feed(FEED)
    assert len(units) == 30
    assert {u.plan for u in units} == {
        "Ames 1", "Ames 2", "Aspen 1", "Aspen 2", "Avon 1", "Avon 2", "Breckenridge 1", "Breckenridge 2",
        "Denver 2", "Eaton 1", "Erie 1", "Erie 2", "Evans 2", "Evergreen 1", "Townhome"}
    u = next(u for u in units if u.unit == "5120")
    assert (u.plan, u.beds, u.baths, u.sqft, u.floor) == ("Denver 2", 0, 1.0, 535, None)   # 5120 lists no "Nth Floor" amenity
    assert next(u for u in units if u.unit == "3420").floor == 4
    assert (u.rent_min, u.rent_max, u.deposit) == (2275, 4267, 0)
    assert u.available_date == "2026-09-02" and u.made_ready_date == "2026-09-02"
    assert u.status == "Vacant Unrented Ready"
    assert u.apartment_id and u.floorplan_id == "3539945"
    assert u.apply_url.startswith("https://bellwestford.securecafe.com/")
    # sorted by beds, plan, unit
    assert [u.beds for u in units] == sorted(u.beds for u in units)


def test_parse_feed_rejects_wrong_shape():
    with pytest.raises(scrape.FeedError):
        scrape.parse_feed({"nope": 1})
    with pytest.raises(scrape.FeedError):
        scrape.parse_feed([])


def test_parse_feed_skips_junk_records(caplog):
    obj = {"floorplans": {"a": "not-a-dict", "b": {"FloorplanName": "", "ApartmentName": "1"},
                          "c": {"FloorplanName": "X 1", "ApartmentName": "0101", "MinimumRent": "junk", "Beds": "2"}}}
    units = scrape.parse_feed(obj)
    assert [u.unit for u in units] == ["0101"]
    assert units[0].rent_min is None and units[0].beds == 2 and units[0].amenities == []


# ---- page catalog --------------------------------------------------------------
def test_parse_catalog_real_fixture():
    plans = scrape.parse_catalog(PAGE)
    assert len(plans) == 21
    assert [p.slug for p in plans][:4] == ["denver-1", "denver-2", "eaton-1", "eaton-2"]
    d1 = plans[0]
    assert (d1.name, d1.beds, d1.baths, d1.sqft, d1.bldg, d1.feedmap) == ("Denver 1", 0, 1.0, 532, "1", "Denver 1")
    assert d1.url == "https://www.bellwestford.com/floorplans-m5-pt/denver-1/"
    assert d1.image.endswith(".png")
    th = next(p for p in plans if p.slug == "townhome")
    assert (th.beds, th.baths, th.sqft) == (3, 2.0, 1932)
    assert {p.beds for p in plans} == {0, 1, 2, 3}
    # every feed plan maps onto a catalog card
    feed_plans = {u.plan for u in scrape.parse_feed(FEED)}
    assert feed_plans <= {p.feedmap for p in plans}


def test_parse_catalog_garbage_is_empty():
    assert scrape.parse_catalog("<html><body>nothing here</body></html>") == []
    assert scrape.parse_catalog("") == []


def test_catalog_roundtrip_and_merge(tmp_path):
    plans = scrape.parse_catalog(PAGE)
    scrape.save_catalog(tmp_path / "plans.json", plans)
    assert scrape.load_catalog(tmp_path / "plans.json") == plans
    assert scrape.load_catalog(tmp_path / "missing.json") == []
    units = scrape.parse_feed(FEED)
    assert scrape.merge_catalog(list(plans), units) == plans      # nothing new
    stranger = scrape.Unit(plan="Zephyr 9", unit="0001", apartment_id="1", floorplan_id="1", beds=1, baths=1.0, sqft=700,
                           floor=None, rent_min=2000, rent_max=3000, deposit=0, available_date=None, made_ready_date=None,
                           status="", amenities=[], specials="", apply_url="")
    merged = scrape.merge_catalog(list(plans), units + [stranger])
    assert merged[-1].slug == "zephyr-9" and merged[-1].beds == 1 and len(merged) == 22


# ---- rows ---------------------------------------------------------------------
def test_build_rows_matches_what_the_site_displays():
    plans = scrape.parse_catalog(PAGE)
    units = scrape.parse_feed(FEED)
    prow, urow = scrape.build_rows(plans, units, "2026-09-13", "2026-09-13T14:49:10+00:00")
    assert len(prow) == 21 and len(urow) == 30
    by = {r.slug: r for r in prow}
    # rendered page on 2026-09-13: "Denver 2 ... $2,275 - $4,511 ... 3 AVAILABLE"
    d2 = by["denver-2"]
    assert (d2.listed, d2.n_units, d2.price_min, d2.price_max, d2.price_text) == (1, 3, 2275, 4511, "$2,275 - $4,511")
    assert d2.earliest_available == "2026-06-11"
    # "Breckenridge 1 ... $4,205 - $9,169 ... 3 AVAILABLE"
    b1 = by["breckenridge-1"]
    assert (b1.n_units, b1.price_min, b1.price_max) == (3, 4205, 9169)
    # "Denver 1 ... call for pricing" (no units in the feed)
    d1 = by["denver-1"]
    assert (d1.listed, d1.n_units, d1.price_min, d1.price_text, d1.earliest_available) == (0, 0, None, "call for pricing", None)
    assert d1.beds == 0 and d1.sqft == 532
    assert sum(r.listed for r in prow) == 15
    # ordering: beds, then sqft
    assert [r.beds for r in prow] == sorted(r.beds for r in prow)
    u = next(r for r in urow if r.unit == "3420")
    assert (u.plan, u.slug, u.floor, u.rent_min, u.amenities) == ("Ames 1", "ames-1", 4, 3106, "4th Floor; Balcony; Premium View; Maple White; Botticino")


def test_build_rows_never_drops_a_unit_for_an_unknown_plan():
    units = scrape.parse_feed(FEED)
    prow, urow = scrape.build_rows([], units, "2026-09-13", "t")
    assert len(urow) == 30 and len(prow) == 15 and all(r.listed for r in prow)


# ---- csv ----------------------------------------------------------------------
def test_upsert_csv_replaces_same_day_rows(tmp_path):
    plans = scrape.parse_catalog(PAGE)
    units = scrape.parse_feed(FEED)
    p1, _ = scrape.build_rows(plans, units, "2026-09-12", "t1")
    p2, _ = scrape.build_rows(plans, units, "2026-09-13", "t2")
    path = tmp_path / "prices.csv"
    assert scrape.upsert_csv(path, p1, scrape.PlanRow, {"2026-09-12"}) == 21
    assert scrape.upsert_csv(path, p2, scrape.PlanRow, {"2026-09-13"}) == 42
    p2b, _ = scrape.build_rows(plans, units[:10], "2026-09-13", "t3")   # re-run same day, fewer units
    assert scrape.upsert_csv(path, p2b, scrape.PlanRow, {"2026-09-13"}) == 42
    text = path.read_text()
    assert text.count("t3") == 21 and "t2" not in text and text.count("t1") == 21
    assert text.splitlines()[0].startswith("date,scraped_at,plan,slug,beds,baths,sqft,bldg,listed,n_units,price_min,price_max,price_text,earliest_available,url")
    assert ",0,1,532," in text            # beds 0 (studio), baths float written as int; None -> empty


def test_upsert_csv_keeps_unknown_columns(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("date,slug,extra\n2026-01-01,a,keepme\n")
    plans = scrape.parse_catalog(PAGE)[:1]
    rows, _ = scrape.build_rows(plans, [], "2026-01-02", "t")
    scrape.upsert_csv(path, rows, scrape.PlanRow, {"2026-01-02"})
    lines = path.read_text().splitlines()
    assert "extra" in lines[0] and "keepme" in lines[1]


# ---- main (offline) ------------------------------------------------------------
def test_main_offline_writes_everything(tmp_path):
    rc = scrape.main(["--feed-file", str(FIX / "feed_2026-09-13.json"), "--page-file", str(FIX / "floor-plans_2026-09-13.html"),
                      "--date", "2026-09-13", "--data-dir", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / "prices.csv").exists() and (tmp_path / "units.csv").exists()
    assert (tmp_path / "plans.json").exists()
    raw = json.loads(next((tmp_path / "raw").glob("2026-09-13_*Z.json")).read_text())
    assert raw["date"] == "2026-09-13" and raw["feed"]["floorplans"] and raw["feed_url"] == scrape.FEED_URL
    assert len((tmp_path / "units.csv").read_text().splitlines()) == 31


def test_main_refuses_empty_feed_unless_allowed(tmp_path):
    empty = tmp_path / "empty.json"; empty.write_text('{"floorplans": {}, "rent_data": {}}')
    args = ["--feed-file", str(empty), "--page-file", str(FIX / "floor-plans_2026-09-13.html"), "--date", "2026-09-13", "--data-dir", str(tmp_path / "d")]
    assert scrape.main(args) == 2
    assert not (tmp_path / "d" / "prices.csv").exists()
    assert scrape.main(args + ["--allow-empty"]) == 0
    text = (tmp_path / "d" / "prices.csv").read_text()
    assert text.count("call for pricing") == 21


def test_main_falls_back_to_cached_catalog(tmp_path, caplog):
    scrape.save_catalog(tmp_path / "plans.json", scrape.parse_catalog(PAGE))
    bad_page = tmp_path / "bad.html"; bad_page.write_text("<html>redesigned</html>")
    rc = scrape.main(["--feed-file", str(FIX / "feed_2026-09-13.json"), "--page-file", str(bad_page), "--date", "2026-09-13", "--data-dir", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / "prices.csv").read_text().count("\n") == 22   # header + 21 plans from the cache


def test_main_dry_run_writes_nothing(tmp_path, capsys):
    rc = scrape.main(["--feed-file", str(FIX / "feed_2026-09-13.json"), "--page-file", str(FIX / "floor-plans_2026-09-13.html"),
                      "--date", "2026-09-13", "--data-dir", str(tmp_path), "--dry-run"])
    assert rc == 0 and not list(tmp_path.iterdir())
    out = json.loads(capsys.readouterr().out)
    assert len(out["plans"]) == 21 and len(out["units"]) == 30


# ---- review follow-ups ----------------------------------------------------------
def test_same_day_rerun_drops_units_that_left_the_feed(tmp_path):
    """Replacement is by date: a re-run with fewer units must not keep the stale ones."""
    plans = scrape.parse_catalog(PAGE)
    units = scrape.parse_feed(FEED)
    _, u1 = scrape.build_rows(plans, units, "2026-09-13", "t1")
    _, u2 = scrape.build_rows(plans, [u for u in units if u.plan != "Denver 2"], "2026-09-13", "t2")
    path = tmp_path / "units.csv"
    assert scrape.upsert_csv(path, u1, scrape.UnitRow, {"2026-09-13"}) == 30
    assert scrape.upsert_csv(path, u2, scrape.UnitRow, {"2026-09-13"}) == 27
    assert "denver-2" not in path.read_text()
    # an empty day really clears the date and still writes the header
    assert scrape.upsert_csv(path, [], scrape.UnitRow, {"2026-09-13"}) == 0
    assert path.read_text().startswith("date,scraped_at,plan,slug,unit,")
    # other dates are untouched
    _, u3 = scrape.build_rows(plans, units[:5], "2026-09-12", "t3")
    scrape.upsert_csv(path, u3, scrape.UnitRow, {"2026-09-12"})
    scrape.upsert_csv(path, u2, scrape.UnitRow, {"2026-09-13"})
    text = path.read_text()
    assert text.count("2026-09-12,") == 5 and text.count("2026-09-13,") == 27


def test_slug_is_stable_when_the_page_reslugs_a_card():
    plans = scrape.parse_catalog(PAGE.replace('data-formattedid="denver-2"', 'data-formattedid="denver-two-studio"'))
    assert next(p for p in plans if p.feedmap == "Denver 2").slug == "denver-2"


def test_load_catalog_tolerates_garbage(tmp_path):
    (tmp_path / "a.json").write_text('{"not": "a list"}')
    (tmp_path / "b.json").write_text('[1, "x", {"slug": "s", "name": "S"}]')
    (tmp_path / "c.json").write_text('not json')
    assert scrape.load_catalog(tmp_path / "a.json") == []
    assert [p.slug for p in scrape.load_catalog(tmp_path / "b.json")] == ["s"]
    assert scrape.load_catalog(tmp_path / "c.json") == []


def test_bad_date_is_rejected(tmp_path):
    with pytest.raises(SystemExit):
        scrape.main(["--feed-file", str(FIX / "feed_2026-09-13.json"), "--date", "yesterday", "--data-dir", str(tmp_path)])
    assert not list(tmp_path.iterdir())


def test_zero_units_does_not_touch_the_catalog_cache(tmp_path):
    empty = tmp_path / "empty.json"; empty.write_text('{"floorplans": {}}')
    assert scrape.main(["--feed-file", str(empty), "--page-file", str(FIX / "floor-plans_2026-09-13.html"), "--date", "2026-09-13", "--data-dir", str(tmp_path / "d")]) == 2
    assert not (tmp_path / "d").exists()
