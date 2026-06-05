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
   - Bot shows a grid of district buttons labelled with 2 estate names (`D1 · Raffles Place / Marina`, `D19 · Hougang / Serangoon`, …), 2 per row
   - User taps a district
   - Bot shows the top 10 developments in that district, ranked by transaction count (last 6 months)
   - Each development shows: name (as a tappable link), transaction count, and average PSF
4. User taps a development **name** to view full transaction details (same as name-based search)

### Tapping a development (deep links)
Telegram has no way to make in-message text trigger a bot action directly, so
each development name is rendered as a **deep link**:
`https://t.me/<bot_username>?start=d<district>r<rank>` (e.g. `d2r4`).

- The message is sent with **HTML** parse mode (robust for names containing
  `@`, `&`, etc. — e.g. `SKYSUITES@ANSON`), each name wrapped in `<a href=…>`.
- Tapping sends `/start d2r4`; the `start` handler parses the payload,
  re-fetches the district's ranked list, and calls `handle_property_search`
  for the matching development — same result as a name search.
- The payload is just district+rank (short, safe charset), so no property-name
  encoding is needed. Out-of-range payloads fall through to the welcome screen.
- Earlier this was a column of 10 full-width inline buttons; that was replaced
  with links because the button stack was too clunky. Only a single
  **🔍 New Search** button remains below the list.
- **Caveat:** tapping a deep link to the same bot can show a brief "START"
  confirmation / re-focus on some clients before loading the property.

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
  - `format_district_results(district, developments, bot_username=None)` — Format results as **HTML**; when `bot_username` is given, each name becomes a `t.me/<bot>?start=d<district>r<rank>` deep link
  - `DISTRICT_NAMES` / `district_full_name()` / `district_button_label()` — Estate-name labels (single source of truth); `district_button_label()` trims to 2 towns for buttons, `district_full_name()` returns all towns for the header
  - `NUM_DISTRICTS = 28`

### Modified File
- **`bot.py`**
  - `/start` now shows the two search-mode buttons directly (no need to type `/search` first), **and** handles the `d<district>r<rank>` deep-link payload to open a property
  - Shared keyboard helpers: `build_search_mode_keyboard()`, `build_district_keyboard()`
  - `search_mode_callback()` — Handle name vs. district selection
  - `district_callback()` — Handle district selection; sends the HTML list with name deep links + a single "New Search" button
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
     [D1 · Raffles Place / Marina] [D2 · Tanjong Pagar / Anson]
     [D3 · Tiong Bahru / Queenstown] [D4 · Harbourfront / Sentosa]
     ...
     [D19 · Hougang / Serangoon]   [D20 · Ang Mo Kio / Bishan]
     ...

User: [clicks D19 · Hougang / Serangoon]
Bot: 📍 District 19 — Hougang / Serangoon / Punggol
     Top developments · Last 6 months · tap a name for details
     1. RIVERCOVE RESIDENCES   ← tappable link → 47 txns · S$1,634 psf
     2. RIVERFRONT RESIDENCES  ← tappable link → 42 txns · S$1,732 psf
     3. THE FLORENCE RESIDENCES ← tappable link → 42 txns · S$1,881 psf
     ...
     [🔍 New Search]

User: [taps "RIVERFRONT RESIDENCES"]  (deep link /start d19r2)
Bot: [shows full transaction details + amenity buttons]
```

## Notes

- Ranking uses **resale + sub-sale transactions only** (new sales excluded)
- Only transactions from the **last 6 months** are considered
- Ranking metric is transaction count; average PSF is shown for context
- Landed property transactions (detached/terrace/bungalow) are excluded
- Developments with few recent resale transactions may not appear in the top 10
- Fully served from cached URA data — no API calls at query time
