"""
FastAPI backend for the property webapp — a second frontend beside the
Telegram bot (bot.py is untouched; both call the same domain modules).

Run locally:  uvicorn api:app --reload --port 8000
Railway:      uvicorn api:app --host 0.0.0.0 --port $PORT
              (own service, independent of property-bot / ura-cache-refresh)

The API is a thin JSON wrapper: search_property / get_nearby_info /
get_rental_by_band already return plain dicts, so endpoints only combine
them — no domain logic lives here.

Amenity lookup is slow (sequential Google Places/Distance Matrix calls —
the reason the bot fetches amenities lazily per button tap), so it is a
separate endpoint: /api/property answers fast from the URA cache with a
cheap OneMap geocode for the map pin, and the frontend fetches
/api/amenities afterwards while showing a loading state.

CORS: none configured on purpose — the frontend is served same-origin by
this app (StaticFiles below). Revisit if the webapp ever moves to its own
domain for a client-facing version.
"""

import threading

from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles

from ura import search_property
from rental import get_rental_by_band
from maps import get_nearby_info, geocode_building
from storage import get_recent_searches
from utils import SIZE_BANDS

app = FastAPI(title="Property Bot API")


# ── Pure payload shaping (no IO — tested in Test/test_api.py) ────────────────

def sale_prices_from_bands(bands: dict) -> dict:
    """Band → latest transacted price, the shape get_rental_by_band expects
    for yield. Mirrors bot.py's amenity_callback exactly (latest price per
    band, not the 12-month average)."""
    return {label: {"price": txn.get("price")} for label, txn in (bands or {}).items()}


def order_by_band(mapping: dict) -> dict:
    """Re-key a band→value dict into SIZE_BANDS order (search_property builds
    bands in encounter order; JSON object order is what the frontend renders)."""
    ordered = {b["label"]: mapping[b["label"]] for b in SIZE_BANDS if b["label"] in mapping}
    # Anything unexpected still gets through, at the end.
    ordered.update({k: v for k, v in mapping.items() if k not in ordered})
    return ordered


def build_property_payload(ura_result: dict, rental_result: dict, coords: dict | None) -> dict:
    """Combine the three sources into the /api/property response.

    ura_result must already be a successful search (no error/ambiguous).
    coords is geocode_building()'s dict or None — the quick OneMap pin;
    /api/amenities later returns the Google-geocoded origin the distances
    are measured from.
    """
    return {
        "development": ura_result["development"],
        "street": ura_result["street"],
        "lat": coords["lat"] if coords else None,
        "lng": coords["lng"] if coords else None,
        "bands": order_by_band(ura_result["bands"]),
        "band_avg_psf": ura_result.get("band_avg_psf", {}),
        "overall_avg_psf": ura_result.get("overall_avg_psf"),
        "overall_psf_count": ura_result.get("overall_psf_count", 0),
        "total_units": ura_result.get("total_units"),
        "expected_top": ura_result.get("expected_top"),
        "under_construction": ura_result.get("under_construction", False),
        "fuzzy_match": ura_result.get("fuzzy_match"),
        "rental": rental_result,
    }


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/api/list")
def api_list():
    return {"searches": get_recent_searches(limit=10)}


# get_ura_data()/get_rental_data() refresh in-line when stale with no
# concurrency guard — fine for the bot (Telegram updates are serial), but
# concurrent web requests would each launch a full URA refresh. Serialize
# the cache-touching section here rather than modifying the cache modules.
_search_lock = threading.Lock()


@app.get("/api/property")
def api_property(q: str = Query(..., min_length=1)):
    with _search_lock:
        ura_result = search_property(q)

        if ura_result.get("ambiguous"):
            return {"ambiguous": True, "candidates": ura_result["candidates"]}
        if "error" in ura_result:
            return {"error": ura_result["error"]}

        # Under construction → no rental contracts of its own; any name match
        # would be stale/wrong (same gate as bot.py's rental button).
        if ura_result.get("under_construction"):
            rental_result = {"unavailable": "under_construction"}
        else:
            sale_prices = sale_prices_from_bands(ura_result.get("bands"))
            rental_result = get_rental_by_band(
                ura_result["development"], sale_prices, ura_result["street"]
            )

    # Quick OneMap pin so the map isn't blank while amenities load.
    # Street only — never "project + street" (wrong-coordinate convention).
    try:
        coords = geocode_building(ura_result["street"])
    except Exception:
        coords = None

    return build_property_payload(ura_result, rental_result, coords)


@app.get("/api/amenities")
def api_amenities(
    street: str = Query(..., min_length=1),
    lat: float | None = None,
    lng: float | None = None,
):
    """Slow bundle: MRT + malls + schools + supermarkets in one get_nearby_info
    call (each item already carries dest_lat/dest_lng/maps_link). Geocodes the
    street via Google unless lat/lng are passed."""
    return get_nearby_info(street, lat=lat, lng=lng)


# Mounted last so /api/* wins; html=True serves index.html at /.
app.mount("/", StaticFiles(directory="webapp", html=True), name="webapp")
