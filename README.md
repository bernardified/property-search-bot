# Property Search Bot

Singapore property search with two independent frontends over one set of domain modules:

- **Telegram bot** (`bot.py`) — conversational search, amenities and calculators
- **Web app** (`api.py` + `webapp/`) — a Leaflet map of every development and HDB block, with charts

Both cover **private property** (URA transactions, rentals, pipeline) and **HDB resale**
(data.gov.sg), and share the same cache, search and formatting code — the webapp is a thin
JSON wrapper over the modules the bot already uses.

## Features

### Private property (URA)
- **Transaction prices** — search any non-landed development by name; results bucketed by size band (≤600 sqft → >1200 sqft)
- **Rental contracts & gross yield** — latest URA rental data for the same development, grouped by size band
- **Price trend** — 5-year PSF trend with a fitted growth rate (OLS on log PSF over individual transactions; quoted only when statistically significant)
- **Liquidity / absorption** — take-up rate for under-construction projects, turnover rate for completed ones
- **Affordability** — mortgage + TDSR check, seeded from a size band's 12-month average price
- **Browse by district** — top developments per district (28 districts, named by estate) over the last 6 months
- **Nearby developments** — private projects within 1 km
- **PropertyGuru links** — per-bedroom sale/rent search links (links only, no scraping)

### HDB resale
- **Block / street / town search** — results grouped by flat type, with remaining lease and PSF
- **Browse by town** — per-flat-type median snapshot (towns sourced from the data, not hardcoded)
- **Price trend** — 5-year resale PSF trend per block or street
- **Amenities** on block detail (the only level with a real coordinate)

### Shared
- **Postal code search** — a bare 6-digit code auto-detects private vs HDB from the authoritative HDB block dataset, regardless of the market toggle
- **Amenities** — nearest MRT (walking distance + exit), Phase 2A primary schools within 1 km, malls, supermarkets, hawker centres and coffee shops; all six looked up concurrently
- **Smart cache** — URA transactions refresh Tue/Fri, rentals on the 15th, HDB resale monthly

### Web app map
- **Explore layer** — every non-landed development (~2.4k dots) or every HDB block (~9.6k), one market at a time, clustered
- **Dot encoding** — sequential colour ramp by 12-month avg PSF / gross yield (private) or remaining lease (HDB); quintiles computed once over the whole dataset, cluster bubbles wear the mean of their children
- **Client-side filters** — transacted-in-12mo, nearest MRT, tenure, PSF range, district (private) · flat type, minimum lease, town (HDB)
- **Type-ahead** — private project names plus HDB streets and blocks, no extra requests
- **Nearby view** — 1 km ring around the loaded property, on either market
- **Charts** — PSF by size band, and the price trend with its fitted line

## Commands

| Command | Description |
|---|---|
| `/start` | Market toggle (Private / HDB), then that market's discovery options |
| `/search` | Search a property (name, postal code, or browse) |
| `/mortgage` | Mortgage & affordability calculator |
| `/list` | Show your recent searches |
| `/refresh` | Force-refresh the URA cache |
| `/help` | Same as `/start` |

## Setup

1. Copy `.env.test` to `.env` and fill in your credentials:

```
TELEGRAM_BOT_TOKEN=
MONGO_URI=
ONEMAP_EMAIL=
ONEMAP_PASSWORD=
GOOGLE_MAPS_API_KEY=
URA_ACCESS_KEY=
```

MongoDB is required — it holds the transaction/rental/HDB caches, search history, cache
freshness timestamps and the permanent lookup collections.

2. Install dependencies:

```bash
python -m venv venv && source venv/bin/activate && pip install -r requirements.txt
```

3. Run the bot:

```bash
python bot.py
```

4. Run the web app (independent of the bot):

```bash
uvicorn api:app --reload --port 8000
```

`webapp/` is served same-origin at `/` — static files, no build step.

## Tests

```bash
python -m pytest Test -q
```

| Suite | Tests | Notes |
|---|---|---|
| `Test/test_suite.py` | 214 | curated suite; also runnable as `python -m Test.test_suite` |
| `Test/test_api.py` | 90 | webapp endpoints |
| `Test/test_utils.py` | 21 | shared helpers |
| `Test/test_cache_memo.py` | 6 | cache memoisation |

External APIs are mocked, so the tests run offline without credentials.

## Architecture

### Entry points
| File | Purpose |
|---|---|
| `bot.py` | Telegram handlers, conversation flow, inline keyboards |
| `api.py` | FastAPI JSON wrapper over the domain modules + static `webapp/` |
| `webapp/` | Leaflet (OneMap tiles) + Chart.js frontend — `index.html`, `map.js`, `style.css` |
| `refresh_job.py` | Scheduled cache refresh (Tue/Fri/15th) |

### Domain modules
| File | Purpose |
|---|---|
| `ura.py` | URA transaction search, size-band formatting, price trend |
| `rental.py` | URA rental contracts by size band |
| `hdb.py` | HDB resale search/format — block, street, town, trend |
| `district_search.py` | Browse-by-district ranking + `DISTRICT_NAMES` |
| `nearby.py` | Private developments within a radius of a project |
| `mortgage.py` | Mortgage & TDSR affordability (pure) |
| `liquidity.py` | Take-up / turnover absorption metrics |
| `propertyguru.py` | Per-bedroom listing links (pure, no IO) |
| `maps.py` | Google Places/Distance Matrix amenities, OneMap geocoding |
| `utils.py` | Size bands, flat types, haversine, SVY21→WGS84, Mongo, OneMap token, staleness |
| `storage.py` | Search history in Mongo (`user_searches`) |

### Cache & data (`cache/`)
| File | Purpose |
|---|---|
| `cache_ura.py` | URA transactions + pipeline (chunked in Mongo) |
| `cache_rental.py` | URA rental contracts |
| `cache_hdb.py` | HDB resale (60-month window) + the HDB Property Information block check |
| `explore_cache.py` | Persisted map layers, keyed to the caches they derive from |
| `onemap_mrt.py` | Nearest-MRT lookups via OneMap routing |
| `schools_cache.py` | Phase 2A-eligible schools within 1 km |
| `hawker_cache.py` | NEA hawker-centre register (123 open centres) |
| `unit_counts.py` | Permanent project → total units table |

### Scripts (`scripts/`, manual, never imported at runtime)
`build_project_coords.py` · `build_hdb_block_coords.py` · `scrape_unit_counts.py`

## Deployment

Three independent Railway services, so one can fail without touching the others:

| Service | Command | Kind |
|---|---|---|
| `property-bot` | `python bot.py` | long-running |
| webapp | `uvicorn api:app --host 0.0.0.0 --port $PORT` | long-running |
| `ura-cache-refresh` | `python refresh_job.py` | cron (Tue/Fri/15th) |

The cron job refreshes URA transactions, rentals, MRT and schools; the HDB resale cache
refreshes lazily on its own monthly staleness check. No extra secrets — all three services
share the same env vars.

## Data sources & attribution

Under the Singapore Open Data Licence (SODL v1.0), which requires a conspicuous notice
naming the dataset, source and licence, and forbids implying endorsement. The webapp
carries this in its sidebar footer and map credit — keep both.

- **URA** — private transactions, rentals, project pipeline (real-estate API)
- **data.gov.sg** — HDB resale prices, HDB Property Information block listings
- **NEA** (via data.gov.sg) — hawker-centre locations
- **OneMap** — geocoding, postal codes, routing, map tiles
- **Google Maps** — Places and Distance Matrix for malls, supermarkets, coffee shops and transit timings

## Further docs

- `CLAUDE.md` — full architecture notes, design decisions and the reasoning behind them
- `DISTRICT_SEARCH.md` — district-browse feature and its deep-link scheme
