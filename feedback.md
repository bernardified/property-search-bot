# Session Feedback & Handoff Notes

> Purpose: carry context across sessions. Read this before working on the
> district-search feature or making similar data-driven changes. It records
> what the user wanted, mistakes that were made, how they were fixed, and the
> key facts that would have prevented the mistakes.

---

## What the user wanted

Expand the Telegram property bot beyond search-by-name:

1. **Browse by District** — pick a Singapore district and see the **top 10
   most-transacted developments** in the **last 6 months**.
2. Surface **Search by Name** and **Browse by District** as buttons directly
   from `/start` (and `/search`) — don't make the user type `/search` first.
3. Show **average PSF** per development in the district list.
4. Later refinement: **exclude new-sale transactions** from the district
   ranking — count **resale + sub-sale only** (secondary market).
5. Later refinement: label districts with **estate/town names** (e.g.
   "D19 · Hougang") since most users don't know district numbers.

---

## What went wrong (and the lesson each time)

### 1. Built an elaborate geocoding pipeline that was never needed ⚠️ BIGGEST MISS
I assumed there was no district info in the data and built a system that
geocoded every street via OneMap → postal code → district, cached in MongoDB.

**Reality: every URA transaction already has a `district` field** (values
`"01"`–`"28"`, 100% coverage across ~138k transactions).

**Lesson:** *Inspect the raw data structure FIRST.* Before building any
derivation/enrichment layer, dump the actual keys and a sample record. One
`print(sorted(txn.keys()))` would have saved the entire detour.

### 2. OneMap geocoding didn't scale
First implementation geocoded ~3905 streets one-by-one with a 5s timeout each.
Timed out constantly; only ~190/800 unique streets ever resolved, silently
**dropping whole developments** from districts.

**Lesson:** per-item synchronous API calls over thousands of items is a red
flag. But more importantly — see #1, the calls shouldn't have existed.

### 3. Wrong API field name
Read `results[0].get("POSTAL_CODE")` — OneMap returns the field as `"POSTAL"`.
Every lookup silently returned `None` → 0 streets mapped → misleading "no
OneMap credentials" error even though credentials worked fine.

**Lesson:** verify API response field names against an actual response, not
assumption. Don't let a swallowed exception/None masquerade as a config error.

### 4. Fundamentally wrong postal→district mapping
Assumed "first 2 digits of postal code = district number." **False.**
Singapore postal *sectors* and *district numbers* are different systems —
e.g. postal sector `18`/`19` = **District 7** (Beach Road), NOT District 19.
Result: Beach Road condos (City Gate, Concourse Skyline, Midtown Bay) showed
up under D19, while real D19 (Hougang/Serangoon/Punggol) condos were missing.

**Lesson:** never invent a mapping for a real-world coding system. Verify
against an authoritative source, or — as here — avoid the mapping entirely
because the source data already had it.

### 5. Hardcoded the wrong district count
Button grid generated D1–D27. Singapore has **28** postal districts, and D28
(Seletar/Yio Chu Kang) had real volume (~99 txns/6mo). D28 was unreachable.

**Lesson:** check the data's actual domain (`distinct districts = 28`) rather
than going off a casual "1-27" mention.

### 6. Left stale documentation
`DISTRICT_SEARCH.md` kept describing the removed OneMap/postal approach,
"27 districts," and deleted functions. Would have misled a PR reviewer.

**Lesson:** when an approach is ripped out, update its docs in the same change.

### 7. Process / workflow misses
- Committed onto the **`add-readme`** branch. **User rule going forward:
  every new feature gets its OWN feature branch off `main` (e.g.
  `feature/<name>`). NEVER reuse `add-readme` or pile features onto an
  unrelated branch.** (Now also recorded in `CLAUDE.md` → Git Workflow.)
- Git identity wasn't configured; had to set it locally
  (`bernardified@gmail.com`).

---

## Final working design (what's in the code now)

- **`district_search.py`** reads `txn["district"]` directly from cached URA
  data. No OneMap, no postal codes, no street→district cache. Much simpler
  (~150 lines), faster (zero API calls at query time), correct.
- Ranking = **resale (`typeOfSale == "3"`) + sub-sale (`"2"`)** only, last 6
  months, grouped by project, sorted by transaction count, shows avg PSF.
- Landed property excluded (propertyType contains detached/terrace/bungalow).
- **28 districts**, each with an estate name in `DISTRICT_NAMES`
  (`district_short_name()` for buttons, `district_full_name()` for headers).
- **`bot.py`**: `/start` and `/search` both show the two-button menu via
  `build_search_mode_keyboard()`; district grid via `build_district_keyboard()`
  (2 per row, estate-name labels). Both callbacks registered globally so they
  work from either entry point.
- PR #5 opened against `main`:
  https://github.com/bernardified/property-search-bot/pull/5

---

## Key data facts (memorize — these prevent the mistakes above)

**URA transaction record (`txn`) keys:**
`area`, `contractDate`, `district`, `floorRange`, `noOfUnits`, `price`,
`propertyType`, `tenure`, `typeOfArea`, `typeOfSale`

**Project-level keys:** `marketSegment`, `project`, `street`, `transaction`
(some also have `x`, `y` SVY21 coords).

- **`district`**: string `"01"`–`"28"`, present on 100% of transactions.
  Singapore has **28** districts.
- **`typeOfSale`**: `"1"` = New Sale, `"2"` = Sub Sale, `"3"` = Resale.
  (See `ura.py:_sale_type_label`.) District ranking keeps `"2"` and `"3"`.
- **`contractDate`**: MMYY format, e.g. `"0921"` = Sep 2021
  (parse via `utils.parse_mmyy_date`).
- **`area`**: square metres (convert with `utils.sqm_to_sqft`).
- Data is cached in MongoDB; access via `cache.cache_ura.get_ura_data()`
  which returns `(transactions, pipeline)`.

---

## General lessons for future sessions

1. **Explore the data before designing.** Print real records and their keys.
   The cheapest possible step prevents the most expensive mistakes.
2. **Don't invent mappings for real-world systems** (postal codes, districts,
   currencies, etc.). Use the source of truth or authoritative reference.
3. **Prefer reading an existing field over deriving/enriching one.**
4. **A swallowed exception returning None is not a config error** — don't
   report it as one. Distinguish "no credentials" from "lookup failed."
5. **Update docs in the same change that changes the behaviour.**
6. **One feature = one feature branch off `main`.** Never reuse `add-readme`.
7. **Re-read the file before claiming what the code does.** I twice asserted
   "there are no buttons" from memory while the live code (and the user's
   screenshot) clearly had a 10-button column. Trust the file on disk, not
   recollection — especially after background/`/loop` edits you didn't watch.

---

## UI iteration notes (district list → tappable property)

The district results went through three display iterations; final state:

- **Now:** development names are **inline tappable links** (HTML `<a>` deep
  links), with txns + avg PSF underneath, plus one "🔍 New Search" button.
- **Before:** a column of 10 full-width inline buttons — rejected as too clunky.

**Telegram constraint to remember:** you cannot make in-message text run a bot
action directly. The only inline-text mechanism is a **deep link**
`https://t.me/<bot>?start=<payload>`:
- Payload charset is `[A-Za-z0-9_-]`, max 64 chars → don't encode raw property
  names (they have spaces/`@`/`&`). Encode an index instead — we use
  `d<district>r<rank>` (e.g. `d19r2`) and re-fetch the ranked list on click.
- The `start` handler parses `context.args[0]` against `d(\d+)r(\d+)`; out-of-
  range payloads fall through to the welcome screen.
- Use **HTML** parse mode for the message (not Markdown) so names like
  `SKYSUITES@ANSON` / `SPOTTISWOODE 18` don't break formatting. Escape only
  `& < >`.
- `bot_username` comes from `context.bot.username` (populated after startup);
  `format_district_results()` keeps it optional so it degrades to plain text
  (and tests stay simple).
- **UX caveat:** tapping a deep link to the *same* bot can show a brief
  "START" confirmation / re-focus on some clients. User accepted this over the
  button stack. If they ever object, fallback = compact buttons (2 per row).
