# Property Search Bot

A Telegram bot for searching Singapore private property transaction prices and rental data, powered by URA's real-estate API.

## Features

- **Transaction prices** — search any private development by name; results bucketed by size band (≤600 sqft → >1200 sqft)
- **Rental contracts and gross yield** — latest URA rental data for the same development, grouped by size band
- **Nearest MRT** — walking distance and exit number via OneMap + Google Maps
- **Primary schools** — Phase 2A-eligible schools within 1 km, sorted by distance
- **Shopping malls & supermarkets** — nearby amenities via Google Places
- **Price trend** — price trend for the past 5 years
- **Smart cache** — URA data refreshes automatically on Tuesday/Friday (transactions) and the 15th of each month (rentals)

## Commands

| Command | Description |
|---|---|
| `/search` | Search a property by name |
| `/list` | Show your recent searches |
| `/refresh` | Force-refresh the URA cache |
| `/help` | Show available commands |

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

2. Install dependencies:

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

3. Run the bot:

```bash
python bot.py
```

## Architecture

| File | Purpose |
|---|---|
| `bot.py` | Telegram handlers and conversation flow |
| `ura.py` | URA transaction search and formatting |
| `rental.py` | URA rental contract search |
| `maps.py` | Google Maps amenity lookups |
| `onemap_mrt.py` | Nearest MRT via OneMap routing |
| `schools_cache.py` | Primary school proximity data |
| `utils.py` | Shared helpers (size bands, haversine, MongoDB, OneMap token, cache staleness) |
| `refresh_job.py` | Scheduled cache refresh job |
| `storage.py` | Search history persistence (MongoDB) |
