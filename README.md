# Bell Westford rent watch

A daily record of the floor-plan rents at [bellwestford.com/floor-plans](https://www.bellwestford.com/floor-plans/), and a live dashboard of how they move. No servers: a GitHub Actions cron fetches the community's availability feed every morning, commits the day's CSVs back to this repo, and rebuilds the dashboard on GitHub Pages.

```
GitHub Actions (cron, 9:05am and 7:05pm EDT = 13:05 / 23:05 UTC)
  └─ scrape.py ── GET ──▶ RentCafe availability feed (JSON, one record per available unit)
        │                  + the floor-plans page, for the list of all 21 plans
        ▼
  data/prices.csv · data/units.csv · data/raw/<date>.json    (committed by the workflow)
        ▼
  build_site.py ──▶ site/index.html ──▶ GitHub Pages
  email_report.py ──▶ Gmail SMTP ──▶ your inbox (2-bedroom report, optional SMS)
```

## Where the prices come from

The floor-plans page ships “call for pricing” in its HTML and fills the real numbers in with JavaScript. That script (the site's `module5` WordPress plugin) reads a **public JSON feed** — a Yardi RentCafe availability export cached at `mmccdn.com`, declared in the page as `js_mits_feed_source`. The feed has one record per *available unit*: floor plan, beds/baths/sqft, min and max rent, availability date, status, amenities. So the scraper is a plain HTTP request; no headless browser, login or API key.

What the page shows is exactly what gets recorded:

| on the page | in the data |
|---|---|
| `$2,275 - $4,511` on a plan card | `price_min` = lowest unit `MinimumRent`, `price_max` = highest unit `MaximumRent` |
| `3 AVAILABLE` | `n_units` = the plan's records in the feed |
| `call for pricing` | the plan has no records in the feed (`listed = 0`) |

The dashboard's headline number per plan is `price_min` — the lowest advertised rent among its available units. The per-unit `rent_max` (a much higher lease-term-dependent figure) is kept but not charted.

## Setup (once)

1. Push this folder to a GitHub repo (public, so Pages is free). The push triggers a deploy-only run, which will fail at the Pages step until step 2 is done — that's expected.
2. Turn on Pages with the Actions source: **Settings → Pages → Build and deployment → Source: GitHub Actions**, or from a terminal `gh api -X POST repos/<you>/<repo>/pages -f build_type=workflow`. (The workflow's own token can't do this for you.)
3. **Actions → “Scrape rents & deploy dashboard” → Run workflow** to take the first snapshot. The log should say `Feed: 30 available units across 15 plans`, then `21 plans, 15 listed, 30 units`, then a green deploy.
4. The dashboard is at `https://<you>.github.io/<repo>/`. It updates itself at 9:05am and 7:05pm Eastern. (Pushes to `main` only rebuild and redeploy the page; they don't re-scrape.)

## Email (and text) updates

After every scheduled scrape the workflow runs `email_report.py`, which emails the current 2-bedroom units (cheapest first, with what changed since the last check) — but only once these repository secrets exist (**Settings → Secrets and variables → Actions → New repository secret**):

| secret | value |
|---|---|
| `MAIL_USERNAME` | the Gmail address to send from, e.g. `you@gmail.com` |
| `MAIL_PASSWORD` | a Gmail **App Password** for that account: Google Account → Security → 2-Step Verification → App passwords → create one named “rent watch”. It's a 16-character code; your real password never goes anywhere. |
| `MAIL_TO` | where to send it (comma-separate several addresses) |
| `SMS_TO` | optional: your carrier's email-to-SMS address, which gets a one-line summary — Verizon `number@vtext.com`, AT&T `number@txt.att.net`, T-Mobile `number@tmomail.net` |

To report on a different bedroom count, add a repository *variable* `REPORT_BEDS` (0 = studio, 1, 2, 3). Until the secrets exist the step just prints the report in the run log and publishes it at `<site>/report.html`.

Each run is ~1 minute of Actions time. The workflow needs no secrets.

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python scrape.py --dry-run -v      # fetch and print, write nothing
.venv/bin/python scrape.py                   # fetch and write data/
.venv/bin/python build_site.py               # build site/index.html
open site/index.html                         # the page works from a file:// URL
.venv/bin/python -m pytest                   # tests, no network
```

For a preview with history before the real one accumulates, `.venv/bin/python tests/synthetic.py /tmp/demo 45` writes a 45-day synthetic dataset; build it with `.venv/bin/python build_site.py --data-dir /tmp/demo --out /tmp/demo-site`.

## Data

`data/prices.csv` — one row per floor plan per day (all 21 plans, listed or not). The 7pm run replaces the 9am rows for that date in both CSVs (a unit that left the feed between the two is dropped, not kept); the raw feed from both runs is kept.

| column | meaning |
|---|---|
| `date` | snapshot date in America/New_York |
| `scraped_at` | UTC timestamp of the fetch |
| `plan`, `slug`, `url` | floor plan identity; `slug` is derived from the feed's plan name (`Denver 2` → `denver-2`), so it stays stable even if the page re-slugs a card |
| `beds`, `baths`, `sqft`, `bldg` | from the plan card (`beds = 0` is a studio; `bldg` is the site's Building 1 / 2 grouping) |
| `listed` | 1 if the feed had at least one unit for the plan |
| `n_units` | number of available units |
| `price_min`, `price_max` | lowest unit `MinimumRent` / highest unit `MaximumRent`; blank when unlisted |
| `price_text` | what the page prints: `$2,275 - $4,511` or `call for pricing` |
| `earliest_available` | earliest `MadeReadyDate` among the plan's units |

`data/units.csv` — one row per available unit per day: `unit` (e.g. `5120`), `apartment_id`, `floorplan_id`, `beds`, `baths`, `sqft`, `floor` (parsed from the amenities), `rent_min`, `rent_max`, `deposit`, `available_date`, `made_ready_date`, `status` (`Vacant Unrented Ready`, `Notice Unrented`, …), `amenities` (`; `-separated), `specials`, `apply_url`.

`data/raw/<date>_<HHMM>Z.json` — one file per run (the day's CSV rows are replaced by the later run, but every raw feed is kept): the feed as fetched, under a `feed` key, wrapped with `date`, `scraped_at`, `feed_url` and the response's `Last-Modified`; any new field can be back-filled from it later. `data/plans.json` — the last good plan catalog parsed from the page, used if the page can't be read.

## When it breaks

- **`feed JSON has no 'floorplans' object`** — the feed format changed. Open the URL in `scrape.py` (`FEED_URL`) and compare with `tests/fixtures/feed_2026-09-13.json`; adjust `parse_feed`.
- **`The feed listed zero units … nothing was written`** — treated as an upstream glitch, not a fully-leased building. The run fails (so you get an email) and no row is recorded. If it's real, run with `--allow-empty`.
- **`Only N plan cards parsed from the page`** — the page markup changed; the run continues with the cached `data/plans.json`, so nothing is lost. Fix `parse_catalog` when convenient.
- **HTTP 403 / 404 on the feed** — the property changed vendors or the CDN path. Load the floor-plans page, view source, and search for `js_mits_feed_source` to find the new URL.
- **Dashboard shows a yellow “last successful check was N days ago” banner** — the workflow is failing or GitHub paused the cron (it does that on inactive repos; the Actions tab shows a re-enable button).
- **Runs show `cancelled` in a row** — each run is being cancelled by the next one before it finishes (the `pages` concurrency group keeps only the newest). Open the most recent cancelled run and see which step it stalled on — usually the deploy step waiting on the `github-pages` environment. Fix that and the next run recovers everything; nothing is lost because every run is idempotent.
- **Push rejected in the “Commit the data” step** — two runs pushed the same date at once; the workflow rebases with this run's rows winning. If it still fails, just re-run the workflow.

Be a good neighbour: it's two small GET requests a day.
