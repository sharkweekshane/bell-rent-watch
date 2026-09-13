#!/usr/bin/env python3
"""Turn data/prices.csv + data/units.csv into the static dashboard in site/.

    python build_site.py            # -> site/index.html (+ site/data.json, site/data/*.csv)

The page is `dashboard_template.html` with the JSON payload inlined, so it works
from a file:// URL as well as on GitHub Pages. All derivations (deltas, first/last
seen, days listed, daily totals) happen here in Python -- see tests/test_build.py --
and the page only draws.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FEED_URL = "https://www.mmccdn.com/mits/rentcafe/bellwestford-rentcafe-feed/feeds/bellwestford-feed.json"
PAGE_URL = "https://www.bellwestford.com/floor-plans/"
REPO_URL = "https://github.com/sharkweekshane/bell-rent-watch"
PRICES_HEADER = "date,scraped_at,plan,slug,beds,baths,sqft,bldg,listed,n_units,price_min,price_max,price_text,earliest_available,url"
UNITS_HEADER = "date,scraped_at,plan,slug,unit,apartment_id,floorplan_id,beds,baths,sqft,floor,rent_min,rent_max,deposit,available_date,made_ready_date,status,amenities,specials,apply_url"


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def num(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def d(s: str) -> date:
    return date.fromisoformat(s)


def shift(iso: str, days: int) -> str:
    return (d(iso) + timedelta(days=days)).isoformat()


TOLERANCE_DAYS = 3   # a 7-/30-day comparison must land within this many days of its target


def price_at(obs: list[tuple[str, int | None]], target: str, tolerance: int | None = TOLERANCE_DAYS):
    """Price of the last observation on or before `target` (obs sorted by date), or None.

    With a tolerance, an observation more than `tolerance` days older than the target
    doesn't count -- after a gap in checks we show "–" rather than a mislabelled delta.
    """
    best = None
    for dt, p in obs:
        if dt > target:
            break
        if p is not None:
            best = (dt, p)
    if best is None:
        return None
    if tolerance is not None and (d(target) - d(best[0])).days > tolerance:
        return None
    return best[1]


def prev_price(obs: list[tuple[str, int | None]], latest: str):
    """Price at the check before `latest`, whenever that was (the 'since last check' delta)."""
    return price_at([o for o in obs if o[0] < latest], latest, tolerance=None)


def delta(price, ref):
    return (price - ref) if price is not None and ref is not None else None


def build_payload(prices: list[dict], units: list[dict], generated_at: str | None = None) -> dict:
    dates = sorted({r["date"] for r in prices if r.get("date")} | {r["date"] for r in units if r.get("date")})
    latest = dates[-1] if dates else None
    first = dates[0] if dates else None

    # ---- plans -------------------------------------------------------------
    by_slug: dict[str, dict] = {}
    for r in sorted(prices, key=lambda r: r["date"]):
        slug = r.get("slug")
        if not slug or not r.get("date"):
            continue
        p = by_slug.setdefault(slug, {"slug": slug, "obs": {}})
        p["name"] = r.get("plan") or slug
        p["beds"], p["baths"], p["sqft"] = num(r.get("beds")), num(r.get("baths")), num(r.get("sqft"))
        p["bldg"], p["url"] = r.get("bldg", ""), r.get("url", "")
        p["obs"][r["date"]] = (num(r.get("price_min")), num(r.get("price_max")), num(r.get("n_units")) or 0,
                               r.get("earliest_available") or None)
    plans = []
    for p in by_slug.values():
        obs = sorted(p["obs"].items())
        po = [(dt, o[0]) for dt, o in obs]
        last = p["obs"].get(latest)
        price = last[0] if last else None
        first_priced = next((dt for dt, o in obs if o[0] is not None), None)
        plans.append({
            "slug": p["slug"], "name": p["name"], "beds": p["beds"], "baths": p["baths"], "sqft": p["sqft"],
            "bldg": p["bldg"], "url": p["url"],
            "listed": bool(last and last[2] > 0), "price": price, "price_max": last[1] if last else None,
            "n_units": last[2] if last else 0, "earliest_available": last[3] if last else None,
            "d1": delta(price, prev_price(po, latest)),
            "d7": delta(price, price_at(po, shift(latest, -7))),
            "d30": delta(price, price_at(po, shift(latest, -30))),
            "first_priced": first_priced,
            "series": [[dt, o[0], o[2]] for dt, o in obs],
        })
    plans.sort(key=lambda p: (p["beds"] if p["beds"] is not None else 99, p["sqft"] or 0, p["name"]))
    # Stable shade index within each bedroom group (colour follows the entity, never the filter).
    counters: dict = {}
    for p in plans:
        g = p["beds"] if p["beds"] in (0, 1, 2, 3) else "x"
        p["k"] = counters.get(g, 0)
        counters[g] = p["k"] + 1

    # ---- units -------------------------------------------------------------
    by_unit: dict[tuple, dict] = {}
    for r in sorted(units, key=lambda r: r["date"]):
        key = (r.get("slug"), r.get("unit"))
        if not key[0] or not key[1] or not r.get("date"):
            continue
        u = by_unit.setdefault(key, {"slug": key[0], "unit": key[1], "obs": {}})
        u["plan"] = r.get("plan") or key[0]
        u["obs"][r["date"]] = num(r.get("rent_min"))
        u["meta"] = {
            "beds": num(r.get("beds")), "baths": num(r.get("baths")), "sqft": num(r.get("sqft")), "floor": num(r.get("floor")),
            "rent_max": num(r.get("rent_max")), "status": r.get("status", ""),
            "available_date": r.get("available_date") or r.get("made_ready_date") or None,
            "amenities": [a for a in (r.get("amenities") or "").split("; ") if a],
            "specials": r.get("specials", ""), "apply_url": r.get("apply_url", ""),
        }
    unit_list = []
    for u in by_unit.values():
        seen = sorted(u["obs"].items())
        first_seen, last_seen = seen[0][0], seen[-1][0]
        # every observed date between first and last seen; missing ones become None so the chart breaks the line
        obs = [(dt, u["obs"].get(dt)) for dt in dates if first_seen <= dt <= last_seen]
        gaps = sum(1 for dt in dates if first_seen <= dt <= last_seen and dt not in u["obs"])
        priced = [(dt, p) for dt, p in seen if p is not None]
        price = u["obs"].get(last_seen)
        first_price = priced[0][1] if priced else None
        p7 = price_at(obs, shift(last_seen, -7))
        unit_list.append({
            "slug": u["slug"], "unit": u["unit"], "plan": u["plan"], **u["meta"],
            "first_seen": first_seen, "last_seen": last_seen,
            "listed": last_seen == latest,
            "censored": first_seen == first,           # was already listed when tracking began
            "days_listed": (d(last_seen) - d(first_seen)).days + 1,   # calendar span
            "n_checks": len(seen), "gaps": gaps,                          # checks it appeared in / dates it was missing
            "price": price, "first_price": first_price,
            "d_first": delta(price, first_price),
            "d7": delta(price, p7),
            "low": min((p for _, p in priced), default=None), "high": max((p for _, p in priced), default=None),
            "series": [[dt, p] for dt, p in obs],
        })
    unit_list.sort(key=lambda u: (not u["listed"], u["beds"] if u["beds"] is not None else 99, u["price"] if u["price"] is not None else 10**9, u["slug"], u["unit"]))

    # ---- daily totals ------------------------------------------------------
    daily = [{"date": dt, "counts": {}, "cheapest": {}} for dt in dates]
    idx = {row["date"]: row for row in daily}
    for u in by_unit.values():
        b = u["meta"]["beds"]
        g = str(b) if b in (0, 1, 2, 3) else "x"
        for dt, p in u["obs"].items():
            row = idx[dt]
            row["counts"][g] = row["counts"].get(g, 0) + 1
            if p is not None and (g not in row["cheapest"] or p < row["cheapest"][g]):
                row["cheapest"][g] = p
    for row in daily:
        row["total"] = sum(row["counts"].values())

    return {
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "latest": latest, "first": first, "n_days": len(dates),
        "source": {"feed": FEED_URL, "page": PAGE_URL, "repo": REPO_URL},
        "plans": plans, "units": unit_list, "daily": daily,
    }


def render(template: str, payload: dict) -> str:
    blob = json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c")
    if "__DATA__" not in template:
        raise SystemExit("dashboard_template.html has no __DATA__ placeholder")
    return template.replace("__DATA__", blob, 1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data")
    ap.add_argument("--out", type=Path, default=ROOT / "site")
    ap.add_argument("--template", type=Path, default=ROOT / "dashboard_template.html")
    args = ap.parse_args(argv)

    prices = read_csv(args.data_dir / "prices.csv")
    units = read_csv(args.data_dir / "units.csv")
    payload = build_payload(prices, units)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "index.html").write_text(render(args.template.read_text(encoding="utf-8"), payload), encoding="utf-8")
    (args.out / "data.json").write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    (args.out / ".nojekyll").write_text("")
    (args.out / "data").mkdir(exist_ok=True)
    for name, header in (("prices.csv", PRICES_HEADER), ("units.csv", UNITS_HEADER)):
        src = args.data_dir / name
        if src.exists():
            shutil.copy(src, args.out / "data" / name)
        else:
            (args.out / "data" / name).write_text(header + "\n", encoding="utf-8")
    print(f"{args.out / 'index.html'}: {payload['n_days']} day(s), {len(payload['plans'])} plans, "
          f"{sum(1 for u in payload['units'] if u['listed'])} units listed on {payload['latest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
