# District Search Feature

## Overview
Users can browse Singapore's 28 districts to discover the most-transacted developments in each area, without needing to know property names. Districts are labelled with their estate names (e.g. "D19 · Hougang") so users don't need to memorise district numbers.

## How It Works

### User Flow
1. User runs `/start` or `/search`
2. Bot shows two options:
   - 🔍 **Search by Name** — Search a specific development (existing flow)
   - 📍 **Browse by District** — Browse top developments by area (new)
3. If "Browse by District" is selected:
   - Bot shows a grid of district buttons labelled with estate names (`D1 · Raffles Place`, `D19 · Hougang`, …), 2 per row
   - User taps a district
   - Bot shows the top 10 developments in that district, ranked by transaction count (last 6 months)
   - Each development shows: name, transaction count, and average PSF
4. User taps a development to view full transaction details (same as name-based search)

### Data Source
- Uses URA transaction data (already cached in MongoDB) — **no external geocoding required**
- Every URA transaction carries a `district` field (`"01"`–`"28"`), which is read directly
- Filters to transactions from the **last 6 months only**
- Counts only **resale and sub-sale** transactions — new sales (developer launch activity) are excluded so the ranking reflects the genuine secondary market
- Ranks developments by transaction count and shows the **average PSF** across those transactions

## Technical Implementation

### New File
- **`district_search.py`** — Core district search logic
  - `get_top_developments_by_district(district, limit=10)` — Top N developments by resale/sub-sale transaction count over the last 6 months
  - `format_district_results(district, developments)` — Format results for Telegram, with estate name in the header
  - `DISTRICT_NAMES` / `district_short_name()` / `district_full_name()` — Estate-name labels (single source of truth)
  - `NUM_DISTRICTS = 28`

### Modified File
- **`bot.py`**
  - `/start` now shows the two search-mode buttons directly (no need to type `/search` first)
  - Shared keyboard helpers: `build_search_mode_keyboard()`, `build_district_keyboard()`
  - `search_mode_callback()` — Handle name vs. district selection
  - `district_callback()` — Handle district selection and display results
  - Registered both callbacks globally so they work from `/start` and `/search`

## District Numbering

Singapore has 28 postal districts. The district is taken straight from URA's transaction records — there is **no postal-code-to-district mapping** in this feature (an earlier OneMap-based approach was removed because URA already provides the district directly, with 100% coverage).

Estate-name labels are defined in `DISTRICT_NAMES`, e.g.:
- D1 → Raffles Place / Marina
- D9 → Orchard / River Valley
- D19 → Hougang / Serangoon / Punggol
- D28 → Seletar / Yio Chu Kang

## Example Usage

```
User: /start
Bot: 🏠 Singapore Private Property Search …
     [🔍 Search by Name]
     [📍 Browse by District]

User: [clicks Browse by District]
Bot: 📍 Select an area:
     [D1 · Raffles Place] [D2 · Tanjong Pagar]
     [D3 · Tiong Bahru]   [D4 · Harbourfront]
     ...
     [D19 · Hougang]      [D20 · Ang Mo Kio]
     ...

User: [clicks D19 · Hougang]
Bot: 📍 District 19 — Hougang / Serangoon / Punggol
     Top developments · Last 6 months
     1. RIVERCOVE RESIDENCES — 47 txns · S$1,634 psf
     2. RIVERFRONT RESIDENCES — 42 txns · S$1,732 psf
     3. THE FLORENCE RESIDENCES — 42 txns · S$1,881 psf
     ...
```

## Notes

- Ranking uses **resale + sub-sale transactions only** (new sales excluded)
- Only transactions from the **last 6 months** are considered
- Ranking metric is transaction count; average PSF is shown for context
- Landed property transactions (detached/terrace/bungalow) are excluded
- Developments with few recent resale transactions may not appear in the top 10
- Fully served from cached URA data — no API calls at query time
