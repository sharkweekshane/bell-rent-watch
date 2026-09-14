"""Deterministic synthetic history for tests and local previews.

    python tests/synthetic.py /tmp/demo-data 45   # 45 days of prices.csv + units.csv

Starts from today's real feed fixture and walks prices around it: a couple of units
lease (disappear), new ones appear, some plans go unlisted and come back.
"""
from __future__ import annotations

import json
import random
import sys
import zlib
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scrape  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"


def make_history(n_days: int = 45, end: date = date(2026, 9, 13), seed: int = 7):
    rng = random.Random(seed)
    feed = json.loads((FIX / "feed_2026-09-13.json").read_text())
    catalog = scrape.parse_catalog((FIX / "floor-plans_2026-09-13.html").read_text())
    base_units = scrape.parse_feed(feed)
    # a few extra units that exist only in the past (they "lease" mid-history)
    ghosts = [replace(base_units[i], unit=f"9{i:03d}", apartment_id=f"9{i:03d}", rent_min=base_units[i].rent_min + 40,
                      rent_max=base_units[i].rent_max + 40) for i in (0, 5, 12, 20)]
    all_prices: list = []
    all_units: list = []
    start = end - timedelta(days=n_days - 1)
    drift = {u.unit: 0 for u in base_units + ghosts}
    for i in range(n_days):
        day = start + timedelta(days=i)
        units = []
        for u in base_units:
            appear_day = zlib.crc32(u.unit.encode()) % 12 if u.unit not in ("5120", "3413") else 0   # stable across processes
            if i < appear_day:
                continue          # not listed yet
            if rng.random() < 0.12:
                drift[u.unit] += rng.choice([-50, -25, 0, 0, 25, 40])
            units.append(replace(u, rent_min=u.rent_min + drift[u.unit], rent_max=u.rent_max + drift[u.unit]))
        for k, g in enumerate(ghosts):
            if i < n_days - 8 - 5 * k:     # each ghost disappears at a different point
                if rng.random() < 0.1:
                    drift[g.unit] -= 25
                units.append(replace(g, rent_min=g.rent_min + drift[g.unit], rent_max=g.rent_max + drift[g.unit]))
        if i in (9, 10):                   # a whole day where a plan drops out
            units = [u for u in units if u.plan != "Eaton 1"]
        p, ur = scrape.build_rows(list(catalog), units, day.isoformat(), f"{day.isoformat()}T13:25:00+00:00")
        all_prices.extend(p)
        all_units.extend(ur)
    return all_prices, all_units


def write(out: Path, n_days: int = 45) -> None:
    prices, units = make_history(n_days)
    out.mkdir(parents=True, exist_ok=True)
    for name in ("prices.csv", "units.csv"):
        (out / name).unlink(missing_ok=True)
    dates = {r.date for r in prices}
    scrape.upsert_csv(out / "prices.csv", prices, scrape.PlanRow, dates)
    scrape.upsert_csv(out / "units.csv", units, scrape.UnitRow, dates)


if __name__ == "__main__":
    write(Path(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 45)
    print("wrote", sys.argv[1])
