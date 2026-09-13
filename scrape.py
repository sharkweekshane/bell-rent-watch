#!/usr/bin/env python3
"""Snapshot Bell Westford's floor-plan rents into daily CSVs.

Where the numbers come from
---------------------------
bellwestford.com/floor-plans/ ships "call for pricing" in its HTML and fills in the
real prices with JavaScript. The script that does that (the site's `module5`
WordPress plugin) reads a public JSON feed -- a Yardi RentCafe availability export
cached on mmccdn.com -- whose URL the page declares as `js_mits_feed_source`. The
feed is one record per *available unit*: floor plan, beds/baths/sqft, min/max
rent, availability date, status, amenities. No browser, login or API key needed.

What the page shows is exactly what we record:
  * a plan's price range  = min(unit Min) - max(unit Max) over its units in the feed
  * "N AVAILABLE"         = the number of its units in the feed
  * "call for pricing"    = the plan has no units in the feed

Outputs (re-running on the same date replaces that date's rows):
  data/prices.csv        one row per floor plan per day (all plans, listed or not)
  data/units.csv         one row per available unit per day
  data/raw/<date>_<HHMM>Z.json  the feed as fetched, one file per run (under a `feed` key)
  data/plans.json        the plan catalog parsed from the page (fallback if it changes)

Usage:
  python scrape.py                 # fetch + write data/
  python scrape.py --dry-run -v    # fetch, print a summary, write nothing
  python scrape.py --feed-file f.json --page-file p.html   # offline (tests)
"""
from __future__ import annotations

import argparse
import csv
import html as html_lib
import json
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

FEED_URL = "https://www.mmccdn.com/mits/rentcafe/bellwestford-rentcafe-feed/feeds/bellwestford-feed.json"
PAGE_URL = "https://www.bellwestford.com/floor-plans/"
LOCAL_TZ = ZoneInfo("America/New_York")
USER_AGENT = "bell-rent-watch/1.0 (+https://github.com/sharkweekshane/bell-rent-watch; personal rent tracker, one fetch a day)"

ROOT = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = ROOT / "data"

# A unit rent outside this band is a feed glitch, not a price.
RENT_MIN, RENT_MAX = 300, 30_000
# Refuse to record a day with fewer plans than this from the page -- the markup changed.
MIN_CATALOG_PLANS = 10

log = logging.getLogger("scrape")


# ---------------------------------------------------------------------------
# Small parsers (pure functions; see tests/test_scrape.py)
# ---------------------------------------------------------------------------
def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def parse_mdy(s: str | None) -> str | None:
    """'7/2/2026' or '2026-07-02' -> '2026-07-02'. Blank/invalid -> None."""
    s = (s or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if m:
        mm, dd, yy = (int(g) for g in m.groups())
        if yy < 100:
            yy += 2000
        try:
            return date(yy, mm, dd).isoformat()
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?", s)
    if m:
        try:
            return date(*(int(g) for g in m.groups())).isoformat()
        except ValueError:
            return None
    return None


def parse_dollars(s) -> int | None:
    """'3106.00' / '$3,106' / 3106 -> 3106. Blank, junk or implausible -> None."""
    if s is None:
        return None
    txt = re.sub(r"[^0-9.]", "", str(s))
    if not txt or txt == ".":
        return None
    try:
        n = int(round(float(txt)))
    except ValueError:
        return None
    return n if RENT_MIN <= n <= RENT_MAX else None


def parse_int(s) -> int | None:
    try:
        return int(round(float(str(s).replace(",", "").strip())))
    except (TypeError, ValueError):
        return None


def parse_float(s) -> float | None:
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


FLOOR_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\s+Floor\b", re.I)


def parse_floor(amenities: list[str]) -> int | None:
    for a in amenities:
        if m := FLOOR_RE.search(a):
            return int(m.group(1))
    return None


def split_amenities(s: str | None) -> list[str]:
    return [a.strip() for a in (s or "").split("^") if a.strip()]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Unit:
    plan: str            # FloorplanName as the feed spells it, e.g. "Denver 2"
    unit: str            # ApartmentName / MarketingName, e.g. "5120"
    apartment_id: str
    floorplan_id: str
    beds: int | None
    baths: float | None
    sqft: int | None
    floor: int | None
    rent_min: int | None
    rent_max: int | None
    deposit: int | None
    available_date: str | None
    made_ready_date: str | None
    status: str
    amenities: list[str]
    specials: str
    apply_url: str


@dataclass
class Plan:
    slug: str
    name: str
    beds: int | None
    baths: float | None
    sqft: int | None
    bldg: str
    feedmap: str         # the feed's FloorplanCode this card is priced from
    url: str
    image: str


@dataclass
class PlanRow:
    date: str
    scraped_at: str
    plan: str
    slug: str
    beds: int | None
    baths: float | None
    sqft: int | None
    bldg: str
    listed: int
    n_units: int
    price_min: int | None
    price_max: int | None
    price_text: str
    earliest_available: str | None
    url: str


@dataclass
class UnitRow:
    date: str
    scraped_at: str
    plan: str
    slug: str
    unit: str
    apartment_id: str
    floorplan_id: str
    beds: int | None
    baths: float | None
    sqft: int | None
    floor: int | None
    rent_min: int | None
    rent_max: int | None
    deposit: int | None
    available_date: str | None
    made_ready_date: str | None
    status: str
    amenities: str
    specials: str
    apply_url: str


# ---------------------------------------------------------------------------
# Feed
# ---------------------------------------------------------------------------
class FeedError(RuntimeError):
    pass


def parse_feed(obj) -> list[Unit]:
    """The feed's `floorplans` map (keyed by ApartmentId) -> one Unit per record."""
    if not isinstance(obj, dict) or not isinstance(obj.get("floorplans"), dict):
        raise FeedError("feed JSON has no 'floorplans' object -- format changed?")
    units: list[Unit] = []
    for key, rec in obj["floorplans"].items():
        if not isinstance(rec, dict):
            log.warning("feed record %s is not an object; skipped", key)
            continue
        plan = (rec.get("FloorplanName") or rec.get("FloorplanCode") or "").strip()
        unit = (rec.get("ApartmentName") or rec.get("MarketingName") or "").strip()
        if not plan or not unit:
            log.warning("feed record %s lacks plan/unit name; skipped", key)
            continue
        amenities = split_amenities(rec.get("Amenities"))
        units.append(Unit(
            plan=plan, unit=unit,
            apartment_id=str(rec.get("ApartmentId") or key),
            floorplan_id=str(rec.get("FloorplanId") or ""),
            beds=parse_int(rec.get("Beds")), baths=parse_float(rec.get("Baths")),
            sqft=parse_int(rec.get("SQFT")), floor=parse_floor(amenities),
            rent_min=parse_dollars(rec.get("MinimumRent", rec.get("Min"))),
            rent_max=parse_dollars(rec.get("MaximumRent", rec.get("Max"))),
            deposit=parse_int(rec.get("Deposit")),
            available_date=parse_mdy(rec.get("AvailableDate")),
            made_ready_date=parse_mdy(rec.get("MadeReadyDate")),
            status=(rec.get("UnitStatus") or "").strip(),
            amenities=amenities, specials=(rec.get("Specials") or "").strip(),
            apply_url=(rec.get("ApplyOnlineURL") or "").strip(),
        ))
    units.sort(key=lambda u: (u.beds if u.beds is not None else 99, u.plan, u.unit))
    return units


# ---------------------------------------------------------------------------
# Plan catalog from the floor-plans page (static markup: one <section class="floorplan"> per card)
# ---------------------------------------------------------------------------
SECTION_RE = re.compile(r'<section\s+class="floorplan[^"]*"([^>]*)>(.*?)</section>', re.S)
ATTR_RE = re.compile(r'data-([a-z0-9_-]+)="([^"]*)"')
TAGLINE_RE = re.compile(r'<span class="floorplan__tagline">(.*?)</span>\s*</span>', re.S)
HREF_RE = re.compile(r'href="(https?://[^"]*/floorplans-m5-pt/[^"]+)"')
HEADLINE_RE = re.compile(r'class="floorplan__headline[^"]*"[^>]*>([^<]*)<')
IMG_RE = re.compile(r'<img[^>]*\ssrc="([^"]+)"')
BEDS_RE = re.compile(r"(\d+)\s*Bed", re.I)
BATHS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*Bath", re.I)
SQFT_RE = re.compile(r"([\d,]+)\s*SQ\.?\s*FT", re.I)


def parse_catalog(page_html: str) -> list[Plan]:
    plans: list[Plan] = []
    seen: set[str] = set()
    for attrs_s, body in SECTION_RE.findall(page_html or ""):
        attrs = dict(ATTR_RE.findall(attrs_s))
        fpid = html_lib.unescape(attrs.get("fpid", "")).strip()
        if not fpid:
            continue  # the "Online Leasing" box and the no-results template
        feedmap = html_lib.unescape(attrs.get("feedmap") or fpid).strip()
        slug = slugify(feedmap or fpid)        # keyed on the FEED's plan name: stable even if the page re-slugs a card
        if slug in seen:
            continue
        seen.add(slug)
        m = TAGLINE_RE.search(body)
        tagline = re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))).strip() if m else ""
        catg = attrs.get("catg", "")
        beds = 0 if re.search(r"studio", catg + " " + tagline, re.I) else None
        if mb := (BEDS_RE.search(catg) or BEDS_RE.search(tagline)):
            beds = int(mb.group(1))
        baths = float(mb.group(1)) if (mb := BATHS_RE.search(tagline)) else None
        sqft = int(mb.group(1).replace(",", "")) if (mb := SQFT_RE.search(tagline)) else parse_int(attrs.get("sqft"))
        href = HREF_RE.search(body)
        head = HEADLINE_RE.search(body)
        img = IMG_RE.search(body)
        plans.append(Plan(
            slug=slug, name=html_lib.unescape(head.group(1)).strip() if head and head.group(1).strip() else fpid,
            beds=beds, baths=baths, sqft=sqft, bldg=attrs.get("bldg", ""),
            feedmap=feedmap, url=href.group(1) if href else "", image=img.group(1) if img else "",
        ))
    return plans


def catalog_from_units(units: list[Unit]) -> list[Plan]:
    """Last-resort catalog: one plan per distinct feed plan name."""
    by: dict[str, Plan] = {}
    for u in units:
        if u.plan not in by:
            by[u.plan] = Plan(slug=slugify(u.plan), name=u.plan, beds=u.beds, baths=u.baths, sqft=u.sqft,
                              bldg="", feedmap=u.plan, url="", image="")
    return list(by.values())


def load_catalog(path: Path) -> list[Plan]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        return [Plan(**{f.name: p.get(f.name, "") for f in fields(Plan)}) for p in raw if isinstance(p, dict)]
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def save_catalog(path: Path, plans: list[Plan]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(p) for p in plans], indent=1) + "\n", encoding="utf-8")


def merge_catalog(catalog: list[Plan], units: list[Unit]) -> list[Plan]:
    """Add any feed plan the page doesn't list (a brand-new floor plan) so it's never dropped."""
    known = {p.feedmap for p in catalog} | {p.slug for p in catalog}
    extra = [p for p in catalog_from_units(units) if p.feedmap not in known and p.slug not in known]
    for p in extra:
        log.warning("feed plan %r is not on the floor-plans page; recording it anyway", p.name)
    return catalog + extra


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------
def price_text(pmin: int | None, pmax: int | None) -> str:
    if pmin is None:
        return "call for pricing"
    if pmax is None or pmax == pmin:
        return f"${pmin:,}"
    return f"${pmin:,} - ${pmax:,}"


def build_rows(catalog: list[Plan], units: list[Unit], today: str, scraped_at: str) -> tuple[list[PlanRow], list[UnitRow]]:
    by_feedmap = {p.feedmap: p for p in catalog}
    by_slug = {p.slug: p for p in catalog}
    unit_rows: list[UnitRow] = []
    per_plan: dict[str, list[Unit]] = {}
    for u in units:
        p = by_feedmap.get(u.plan) or by_slug.get(slugify(u.plan))
        if p is None:  # cannot happen after merge_catalog, but never lose a unit
            p = Plan(slug=slugify(u.plan), name=u.plan, beds=u.beds, baths=u.baths, sqft=u.sqft, bldg="", feedmap=u.plan, url="", image="")
            catalog.append(p); by_feedmap[p.feedmap] = p; by_slug[p.slug] = p
        per_plan.setdefault(p.slug, []).append(u)
        unit_rows.append(UnitRow(
            date=today, scraped_at=scraped_at, plan=p.name, slug=p.slug, unit=u.unit,
            apartment_id=u.apartment_id, floorplan_id=u.floorplan_id,
            beds=u.beds, baths=u.baths, sqft=u.sqft, floor=u.floor,
            rent_min=u.rent_min, rent_max=u.rent_max, deposit=u.deposit,
            available_date=u.available_date, made_ready_date=u.made_ready_date, status=u.status,
            amenities="; ".join(u.amenities), specials=u.specials, apply_url=u.apply_url,
        ))
    plan_rows: list[PlanRow] = []
    for p in catalog:
        us = per_plan.get(p.slug, [])
        mins = [u.rent_min for u in us if u.rent_min is not None]
        maxs = [u.rent_max for u in us if u.rent_max is not None]
        pmin = min(mins) if mins else None
        pmax = max(maxs) if maxs else None
        dates = [u.made_ready_date or u.available_date for u in us if (u.made_ready_date or u.available_date)]
        plan_rows.append(PlanRow(
            date=today, scraped_at=scraped_at, plan=p.name, slug=p.slug,
            beds=p.beds, baths=p.baths, sqft=p.sqft, bldg=p.bldg,
            listed=1 if us else 0, n_units=len(us), price_min=pmin, price_max=pmax,
            price_text=price_text(pmin, pmax), earliest_available=min(dates) if dates else None, url=p.url,
        ))
    plan_rows.sort(key=lambda r: (r.beds if r.beds is not None else 99, r.sqft or 0, r.slug))
    unit_rows.sort(key=lambda r: (r.beds if r.beds is not None else 99, r.slug, r.unit))
    return plan_rows, unit_rows


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else str(v)
    return str(v)


def upsert_csv(path: Path, rows: list, row_type, dates: set[str]) -> int:
    """Replace every row dated in `dates` with `rows`, keeping all other dates. Returns total rows.

    Replacement is by date, not by row key, so a same-day re-run drops units that have left
    the feed (and an --allow-empty run really clears the day). Writes the header even when
    `rows` is empty. Columns an older/newer schema wrote are preserved.
    """
    new = [{k: _fmt(v) for k, v in asdict(r).items()} for r in rows]
    fieldnames = [f.name for f in fields(row_type)]
    existing: list[dict] = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            existing = list(reader)
            if reader.fieldnames:
                fieldnames = list(dict.fromkeys([*reader.fieldnames, *fieldnames]))
    kept = [r for r in existing if r.get("date", "") not in dates]
    merged = kept + new
    merged.sort(key=lambda r: (r.get("date", ""), r.get("beds", "") or "9", r.get("sqft", "").zfill(6), r.get("slug", ""), r.get("unit", "")))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in merged:
            w.writerow({k: r.get(k, "") for k in fieldnames})
    return len(merged)


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------
def fetch(url: str, *, retries: int = 3, timeout: int = 30):
    """GET with retries; returns a requests.Response. Fails fast on 4xx."""
    import requests  # imported late so `--help` and the pure parsers work without it

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9, */*;q=0.5"}
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if 400 <= r.status_code < 500:
                raise FeedError(f"{url}: HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except FeedError:
            raise
        except Exception as e:  # timeouts, 5xx, connection resets
            last = e
            log.warning("attempt %d/%d for %s failed: %s", attempt, retries, url, e)
            if attempt < retries:
                time.sleep(2 ** attempt)
    raise FeedError(f"{url}: giving up after {retries} attempts ({last})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="fetch and summarise, write nothing")
    ap.add_argument("--allow-empty", action="store_true", help="record the day even if the feed lists zero units")
    ap.add_argument("--feed-file", type=Path, help="read the feed from this JSON file instead of the network")
    ap.add_argument("--page-file", type=Path, help="read the floor-plans page from this HTML file instead of the network")
    ap.add_argument("--date", help="snapshot date to record (YYYY-MM-DD); default: today in America/New_York")
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    now_utc = datetime.now(timezone.utc)
    try:
        today = date.fromisoformat(args.date).isoformat() if args.date else now_utc.astimezone(LOCAL_TZ).date().isoformat()
    except ValueError:
        ap.error(f"--date must be YYYY-MM-DD, got {args.date!r}")
    scraped_at = now_utc.isoformat(timespec="seconds")
    data_dir: Path = args.data_dir

    # 1. The feed (the part that matters).
    feed_last_modified = ""
    if args.feed_file:
        feed_obj = json.loads(args.feed_file.read_text(encoding="utf-8"))
    else:
        log.info("Fetching feed %s", FEED_URL)
        r = fetch(FEED_URL)
        feed_last_modified = r.headers.get("Last-Modified", "")
        try:
            feed_obj = r.json()
        except ValueError as e:
            raise FeedError(f"feed is not JSON ({e}); first bytes: {r.text[:120]!r}") from e
    units = parse_feed(feed_obj)
    log.info("Feed: %d available units across %d plans (feed last-modified: %s)",
             len(units), len({u.plan for u in units}), feed_last_modified or "n/a")

    # 2. The plan catalog (so unlisted plans are recorded as "call for pricing").
    catalog: list[Plan] = []
    try:
        page_html = args.page_file.read_text(encoding="utf-8") if args.page_file else fetch(PAGE_URL).text
        catalog = parse_catalog(page_html)
        if len(catalog) < MIN_CATALOG_PLANS:
            log.warning("Only %d plan cards parsed from the page (expected ~21) -- markup changed? Using cached catalog.", len(catalog))
            catalog = []
    except Exception as e:  # the page is optional; the feed is not
        log.warning("Could not load the floor-plans page: %s -- using cached catalog", e)
    cache = data_dir / "plans.json"
    fresh_catalog = bool(catalog)
    if not catalog:
        catalog = load_catalog(cache)
        if catalog:
            log.info("Using cached catalog of %d plans from %s", len(catalog), cache)
        else:
            log.warning("No cached catalog either; plans will be derived from the feed alone")
    catalog = merge_catalog(catalog, units)

    plan_rows, unit_rows = build_rows(catalog, units, today, scraped_at)
    listed = [p for p in plan_rows if p.listed]
    log.info("%s: %d plans, %d listed, %d units", today, len(plan_rows), len(listed), len(unit_rows))
    for p in plan_rows:
        log.info("  %-16s %s bd %5s sf  %-22s units=%d  earliest=%s", p.plan, p.beds, p.sqft, p.price_text, p.n_units, p.earliest_available or "-")

    if not unit_rows and not args.allow_empty:
        log.error("The feed listed zero units. That is almost certainly an upstream glitch, so nothing was written. "
                  "Re-run with --allow-empty to record a genuinely empty day.")
        return 2
    if args.dry_run:
        print(json.dumps({"plans": [asdict(p) for p in plan_rows], "units": [asdict(u) for u in unit_rows]}, indent=1))
        return 0

    n = upsert_csv(data_dir / "prices.csv", plan_rows, PlanRow, {today})
    log.info("Wrote %s (%d rows total)", data_dir / "prices.csv", n)
    n = upsert_csv(data_dir / "units.csv", unit_rows, UnitRow, {today})
    log.info("Wrote %s (%d rows total, %d for %s)", data_dir / "units.csv", n, len(unit_rows), today)
    if fresh_catalog:
        save_catalog(cache, catalog)
    raw_path = data_dir / "raw" / f"{today}_{now_utc.strftime('%H%M')}Z.json"   # one file per run, never overwritten
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps({"date": today, "scraped_at": scraped_at, "feed_url": FEED_URL,
                                    "feed_last_modified": feed_last_modified, "feed": feed_obj},
                                   indent=1, sort_keys=True) + "\n", encoding="utf-8")
    log.info("Wrote %s", raw_path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FeedError as e:
        log.error("%s", e)
        sys.exit(1)
