"""
Tests for api.py's pure payload shaping + endpoint routing.
Run with: python -m Test.test_api
All external calls (URA cache, rental, geocode, Mongo) are mocked — no network.
"""
import os
import sys
import unittest
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import (
    app,
    build_hdb_block_payload,
    build_hdb_street_payload,
    build_property_payload,
    order_by_band,
    order_by_flat_type,
    project_xy_coords,
    sale_prices_from_bands,
    shape_flat_types,
)

from fastapi.testclient import TestClient

client = TestClient(app)

URA_RESULT = {
    "development": "PARC ESTA",
    "street": "SIMS AVENUE",
    "bands": {
        "<= 600 sqft": {"price": 900000, "psf": 1800, "area_sqft": 500,
                        "floor_range": "06-10", "contract_date_display": "Aug 2026"},
    },
    "band_avg_psf": {"<= 600 sqft": {"avg_psf": 1750, "count": 4}},
    "band_avg_price": {},
    "overall_avg_psf": 1750,
    "overall_psf_count": 4,
    "under_construction": False,
    "fuzzy_match": None,
    "alternatives": [],
    "total_units": 1399,
    "units_source": "pipeline",
    "expected_top": None,
}

RENTAL_RESULT = {"development": "PARC ESTA", "bands": {"<= 600 sqft": {"latest_rent": 3200}}}

# hdb.block_detail's shape. `latest` is a whole normalised row — a datetime and
# every raw field included — which is exactly what shape_flat_types has to keep
# out of the JSON.
HDB_LATEST_ROW = {
    "town": "BISHAN", "flat_type": "4 ROOM", "block": "257", "street": "BISHAN ST 22",
    "storey_range": "04 TO 06", "flat_model": "Improved", "area_sqft": 1076.4,
    "price": 840000.0, "psf": 780, "month": "2026-08",
    "month_dt": datetime(2026, 8, 1), "lease_years": 65.0,
}
HDB_BLOCK = {
    "block": "257", "street": "BISHAN ST 22", "town": "BISHAN", "total_txns": 23,
    "flat_types": {
        "4 ROOM": {"count": 14, "median_price": 840000, "avg_psf": 729,
                   "typical_lease": 65, "latest": HDB_LATEST_ROW},
    },
}
HDB_STREET = {
    "street": "BISHAN ST 22", "town": "BISHAN", "total_txns": 52, "window_months": 12,
    "flat_types": HDB_BLOCK["flat_types"],
    "blocks": [("257", 5), ("236", 4)],
}


class TestPayloadShaping(unittest.TestCase):

    def test_sale_prices_from_bands_mirrors_bot(self):
        prices = sale_prices_from_bands(URA_RESULT["bands"])
        self.assertEqual(prices, {"<= 600 sqft": {"price": 900000}})

    def test_sale_prices_handles_empty(self):
        self.assertEqual(sale_prices_from_bands({}), {})
        self.assertEqual(sale_prices_from_bands(None), {})

    def test_build_property_payload_combines_all_sources(self):
        coords = {"lat": 1.317, "lng": 103.906}
        p = build_property_payload(URA_RESULT, RENTAL_RESULT, coords)
        self.assertEqual(p["development"], "PARC ESTA")
        self.assertEqual(p["street"], "SIMS AVENUE")
        self.assertEqual(p["lat"], 1.317)
        self.assertEqual(p["rental"], RENTAL_RESULT)
        self.assertEqual(p["total_units"], 1399)
        self.assertIn("<= 600 sqft", p["bands"])

    def test_order_by_band_uses_size_bands_order(self):
        from utils import SIZE_BANDS
        labels = [b["label"] for b in SIZE_BANDS]
        shuffled = {labels[2]: 3, labels[0]: 1, "UNKNOWN BAND": 99, labels[1]: 2}
        ordered = order_by_band(shuffled)
        self.assertEqual(list(ordered.keys()), [labels[0], labels[1], labels[2], "UNKNOWN BAND"])

    def test_build_property_payload_tolerates_no_coords(self):
        p = build_property_payload(URA_RESULT, RENTAL_RESULT, None)
        self.assertIsNone(p["lat"])
        self.assertIsNone(p["lng"])

    def test_project_xy_coords_converts_uras_own_xy(self):
        projects = [{"project": "Parc Esta", "x": "28001.642", "y": "38744.572"}]
        # matched case-insensitively; SVY21 → WGS84, same as the explore dots
        coords = project_xy_coords(projects, "PARC ESTA")
        self.assertAlmostEqual(coords["lat"], 1.366666, places=5)
        self.assertAlmostEqual(coords["lng"], 103.833333, places=5)

    def test_project_xy_coords_none_without_xy_or_match(self):
        projects = [{"project": "PARC ESTA"}, {"project": "BRAVO", "x": "", "y": None}]
        self.assertIsNone(project_xy_coords(projects, "PARC ESTA"))   # no x/y
        self.assertIsNone(project_xy_coords(projects, "BRAVO"))       # blank x/y
        self.assertIsNone(project_xy_coords(projects, "NOT A PROJECT"))
        self.assertIsNone(project_xy_coords([], "PARC ESTA"))


class _NoProjectCoords(unittest.TestCase):
    """URA's project list is the pin's first source; default it to empty so
    each test drives the street-geocode fallback unless it says otherwise."""

    def setUp(self):
        p = patch("api.get_ura_data", return_value=([], {}))
        p.start()
        self.addCleanup(p.stop)


class TestEndpoints(_NoProjectCoords):

    @patch("api.geocode_building", return_value={"lat": 1.3, "lng": 103.9})
    @patch("api.get_rental_by_band", return_value=RENTAL_RESULT)
    @patch("api.search_property", return_value=URA_RESULT)
    def test_property_happy_path(self, mock_search, mock_rental, mock_geo):
        r = client.get("/api/property", params={"q": "parc esta"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["development"], "PARC ESTA")
        self.assertEqual(data["lat"], 1.3)
        self.assertEqual(data["rental"]["bands"]["<= 600 sqft"]["latest_rent"], 3200)
        # street (not project name) is what gets geocoded — bot convention
        mock_geo.assert_called_once_with("SIMS AVENUE")
        # rental called with resolved development + latest-price map + street
        mock_rental.assert_called_once_with(
            "PARC ESTA", {"<= 600 sqft": {"price": 900000}}, "SIMS AVENUE"
        )

    @patch("api.search_property", return_value={
        "ambiguous": True,
        "candidates": [{"project": "A", "street": "X"}, {"project": "B", "street": "Y"}],
    })
    def test_property_ambiguous_passthrough(self, _):
        data = client.get("/api/property", params={"q": "the"}).json()
        self.assertTrue(data["ambiguous"])
        self.assertEqual(len(data["candidates"]), 2)

    @patch("api.search_property", return_value={"error": "No transactions found"})
    def test_property_error_passthrough(self, _):
        data = client.get("/api/property", params={"q": "zzz"}).json()
        self.assertEqual(data["error"], "No transactions found")

    @patch("api.geocode_building", return_value=None)
    @patch("api.get_rental_by_band")
    @patch("api.search_property", return_value={**URA_RESULT, "under_construction": True})
    def test_under_construction_gates_rental(self, _s, mock_rental, _g):
        data = client.get("/api/property", params={"q": "parc esta"}).json()
        mock_rental.assert_not_called()
        self.assertEqual(data["rental"], {"unavailable": "under_construction"})
        self.assertIsNone(data["lat"])

    @patch("api.geocode_building", side_effect=RuntimeError("onemap down"))
    @patch("api.get_rental_by_band", return_value=RENTAL_RESULT)
    @patch("api.search_property", return_value=URA_RESULT)
    def test_geocode_failure_does_not_break_property(self, *_):
        data = client.get("/api/property", params={"q": "parc esta"}).json()
        self.assertEqual(data["development"], "PARC ESTA")
        self.assertIsNone(data["lat"])

    @patch("api.get_nearby_info", return_value={"address": "SIMS AVENUE", "lat": 1.3,
                                                "lng": 103.9, "mrts": [], "malls": [],
                                                "schools": [], "supermarkets": []})
    def test_amenities_passes_street_and_coords(self, mock_nearby):
        r = client.get("/api/amenities", params={"street": "SIMS AVENUE"})
        self.assertEqual(r.status_code, 200)
        mock_nearby.assert_called_once_with("SIMS AVENUE", lat=None, lng=None)

    @patch("api.price_trend", return_value={"development": "PARC ESTA",
                                            "periods": [{"label": "2024", "avg_psf": 2000, "count": 30}],
                                            "pct_change": 12, "span_label": "5 yrs", "total_txns": 90})
    def test_trend_passthrough(self, mock_trend):
        data = client.get("/api/trend", params={"q": "PARC ESTA"}).json()
        self.assertEqual(data["pct_change"], 12)
        mock_trend.assert_called_once_with("PARC ESTA")

    @patch("api.geocode_building")
    @patch("api.get_rental_by_band", return_value=RENTAL_RESULT)
    @patch("api.search_property", return_value=URA_RESULT)
    def test_pin_prefers_ura_xy_over_street_geocode(self, _s, _r, mock_geo):
        """A street geocode lands anywhere along the street (YIO CHU KANG ROAD
        put HUNDRED PALMS RESIDENCES ~5km off, and the amenity response then
        snapped the pin to a *different* wrong spot). URA's x/y is exact, so
        it wins and the frontend is told not to snap."""
        projects = [{"project": "PARC ESTA", "x": "28001.642", "y": "38744.572"}]
        with patch("api.get_ura_data", return_value=(projects, {})):
            data = client.get("/api/property", params={"q": "parc esta"}).json()
        self.assertTrue(data["exact_coords"])
        self.assertAlmostEqual(data["lat"], 1.366666, places=5)
        self.assertAlmostEqual(data["lng"], 103.833333, places=5)
        mock_geo.assert_not_called()

    @patch("api.get_recent_searches", return_value=[{"name": "PARC ESTA", "count": 5}])
    def test_list(self, mock_recent):
        data = client.get("/api/list").json()
        self.assertEqual(data["searches"][0]["name"], "PARC ESTA")
        mock_recent.assert_called_once_with(limit=10)

    @patch("api.band_transactions", return_value={
        "development": "PARC ESTA", "band": "<= 600 sqft", "count": 2,
        "transactions": [
            {"price": 1000000, "psf": 1900, "area_sqft": 520,
             "floor_range": "06-10", "type_of_sale": "Resale",
             "contract_date_display": "Jun 2024"},
            {"price": 900000, "psf": 1800, "area_sqft": 500,
             "floor_range": "01-05", "type_of_sale": "Resale",
             "contract_date_display": "Jan 2022"},
        ],
    })
    def test_transactions_endpoint(self, mock_band):
        """The band drill-down passes the resolved name and band straight
        through, same re-query pattern as /api/trend."""
        data = client.get("/api/transactions",
                          params={"q": "PARC ESTA", "band": "<= 600 sqft"}).json()
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["transactions"][0]["price"], 1000000)
        mock_band.assert_called_once_with("PARC ESTA", "<= 600 sqft")

    def test_transactions_requires_band(self):
        """Without a band there is nothing to drill into — reject, don't guess."""
        r = client.get("/api/transactions", params={"q": "PARC ESTA"})
        self.assertEqual(r.status_code, 422)

    def test_static_frontend_served_at_root(self):
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("SG Property Map", r.text)

    def test_frontend_assets_must_revalidate(self):
        """Without Cache-Control a browser invents its own freshness lifetime,
        and a phone then runs a map.js from days ago against a fresh server.
        no-cache keeps the copy but forces a conditional request."""
        for path in ("/", "/map.js", "/style.css"):
            with self.subTest(path=path):
                r = client.get(path)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.headers["cache-control"], "no-cache")
                self.assertIn("etag", r.headers)

    def test_revalidation_returns_304_and_keeps_the_header(self):
        """The saving only lands if the ETag answers with an empty 304 — and
        the 304 has to carry Cache-Control, or the next load caches blindly."""
        first = client.get("/map.js")
        second = client.get("/map.js", headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.content, b"")
        self.assertEqual(second.headers["cache-control"], "no-cache")


RESOLVED_PRIVATE = {"building": "PARC ESTA", "road": "SIMS AVENUE",
                    "address": "8 SIMS AVENUE PARC ESTA", "postal": "408563",
                    "block": "8", "lat": 1.316, "lng": 103.887}


class TestPostalSearch(_NoProjectCoords):
    """A bare 6-digit q routes through resolve_postal_code + the authoritative
    HDB-block check — never through the market-agnostic name search."""

    @patch("api.geocode_building")
    @patch("api.get_rental_by_band", return_value=RENTAL_RESULT)
    @patch("api.search_property", return_value=URA_RESULT)
    @patch("api.is_hdb_residential_block", return_value=False)
    @patch("api.resolve_postal_code", return_value=RESOLVED_PRIVATE)
    def test_private_postal_uses_exact_coords(self, mock_resolve, _hdb, mock_search, _r, mock_geo):
        data = client.get("/api/property", params={"q": "408563"}).json()
        mock_resolve.assert_called_once_with("408563")
        mock_search.assert_called_once_with("PARC ESTA")  # resolved building name
        mock_geo.assert_not_called()  # exact postal coordinate, no street geocode
        self.assertTrue(data["exact_coords"])
        self.assertEqual(data["postal"], "408563")
        self.assertEqual(data["lat"], 1.316)

    @patch("api._hdb_records", return_value=[])
    @patch("api.hdb.block_detail", return_value=dict(HDB_BLOCK))
    @patch("api.hdb.resolve_query", return_value={"kind": "block", "block": "8", "street": "SIMS AVENUE"})
    @patch("api.search_property")
    @patch("api.is_hdb_residential_block", return_value=True)
    @patch("api.resolve_postal_code", return_value={**RESOLVED_PRIVATE, "building": "WOODLEIGH GLEN"})
    def test_hdb_block_postal_returns_an_hdb_payload(self, _res, mock_hdb, mock_search, *_):
        """A postal code is market-agnostic: the HDB block dataset decides, and
        an HDB block now comes back as a result rather than a rejection."""
        data = client.get("/api/property", params={"q": "361206"}).json()
        mock_hdb.assert_called_once_with("8", "SIMS AVENUE")
        mock_search.assert_not_called()  # never fuzzy-matched to a nearby condo
        self.assertEqual(data["market"], "hdb")
        self.assertEqual(data["kind"], "block")
        self.assertEqual(data["postal"], "408563")
        # The postal coordinate is the address itself — no second geocode.
        self.assertTrue(data["exact_coords"])
        self.assertEqual(data["lat"], 1.316)

    @patch("api._hdb_records", return_value=[])
    @patch("api.hdb.resolve_query", return_value={"error": "no such block"})
    @patch("api.is_hdb_residential_block", return_value=True)
    @patch("api.resolve_postal_code", return_value={**RESOLVED_PRIVATE, "building": "WOODLEIGH GLEN"})
    def test_hdb_block_with_no_resale_explains_mop(self, *_):
        """A real HDB block with no resale rows is a pre-MOP development, not a
        missing one — saying 'not found' would read as a bug."""
        data = client.get("/api/property", params={"q": "361206"}).json()
        self.assertIn("MOP", data["error"])
        self.assertIn("Woodleigh Glen", data["error"])

    @patch("api.resolve_postal_code", return_value=None)
    def test_unknown_postal(self, _):
        data = client.get("/api/property", params={"q": "000000"}).json()
        self.assertIn("Couldn't find", data["error"])

    @patch("api.is_hdb_residential_block", return_value=False)
    @patch("api.resolve_postal_code", return_value={**RESOLVED_PRIVATE, "building": ""})
    def test_landed_or_commercial(self, *_):
        data = client.get("/api/property", params={"q": "308215"}).json()
        self.assertIn("landed home or commercial", data["error"])

    @patch("api.geocode_building", return_value={"lat": 1.3, "lng": 103.9})
    @patch("api.get_rental_by_band", return_value=RENTAL_RESULT)
    @patch("api.search_property", return_value=URA_RESULT)
    @patch("api.resolve_postal_code")
    def test_name_search_never_hits_postal_path(self, mock_resolve, *_):
        data = client.get("/api/property", params={"q": "parc esta"}).json()
        mock_resolve.assert_not_called()
        self.assertNotIn("exact_coords", data)


class TestDevelopments(unittest.TestCase):

    def test_svy21_conversion_roundtrips_projection_origin(self):
        from utils import svy21_to_wgs84
        lat, lng = svy21_to_wgs84(28001.642, 38744.572)
        self.assertAlmostEqual(lat, 1.366666, places=5)
        self.assertAlmostEqual(lng, 103.833333, places=5)

    def _projects(self):
        return [
            {"project": "ALPHA", "street": "A ST", "x": "28001.642", "y": "38744.572",
             "transaction": [
                 {"propertyType": "Condominium", "district": "20", "contractDate": "0826",
                  "area": "100", "price": "2000000"},
                 {"propertyType": "Condominium", "district": "20", "contractDate": "0120",
                  "area": "100", "price": "1000000"},  # outside the 12-mo window
             ]},
            {"project": "BRAVO", "street": "B ST",  # no x/y → fallback coords
             "transaction": [{"propertyType": "Apartment", "district": "15",
                              "contractDate": "0826", "area": "50", "price": "1000000"}]},
            {"project": "LANDED ONLY", "street": "C ST", "x": "30000", "y": "30000",
             "transaction": [{"propertyType": "Terrace House", "district": "10"}]},
            {"project": "NO COORDS", "street": "D ST",
             "transaction": [{"propertyType": "Condominium", "district": "09",
                              "contractDate": "0826", "area": "80", "price": "1600000"}]},
        ]

    def test_build_developments(self):
        from datetime import datetime
        from api import build_developments
        devs = build_developments(self._projects(), {"BRAVO": {"lat": 1.30, "lng": 103.90}},
                                  now=datetime(2026, 9, 1))
        names = [d["project"] for d in devs]
        self.assertEqual(names, ["ALPHA", "BRAVO"])  # landed + coordless skipped

        alpha = devs[0]
        self.assertAlmostEqual(alpha["lat"], 1.366666, places=5)  # URA x/y wins
        self.assertEqual(alpha["txns_12mo"], 1)  # old txn excluded from the window
        self.assertEqual(alpha["avg_psf"], 1858)  # 2,000,000 / (100 sqm → sqft)
        self.assertEqual(alpha["district"], "20")

        bravo = devs[1]
        self.assertEqual((bravo["lat"], bravo["lng"]), (1.30, 103.90))  # fallback

    def test_developments_endpoint_memoizes_per_cache_object(self):
        import api
        projects = self._projects()
        api._dev_memo.update(txns=None, rentals=None, payload=None)
        with patch("api.get_ura_data", return_value=(projects, [])), \
             patch("api.get_rental_data", return_value=[]), \
             patch("api._mrt_coords", return_value=[]), \
             patch("api._load_fallback_coords", return_value={}) as mock_fb:
            first = client.get("/api/developments").json()
            second = client.get("/api/developments").json()
        self.assertEqual(first["count"], 1)  # only ALPHA has usable coords
        self.assertEqual(first, second)
        mock_fb.assert_called_once()  # second call served from the memo
        api._dev_memo.update(txns=None, rentals=None, payload=None)

    def test_developments_payload_carries_district_estate_names(self):
        """The dots only carry a district number; the frontend labels them off
        this table, keyed on the same zero-padded code."""
        import api
        api._dev_memo.update(txns=None, rentals=None, payload=None)
        with patch("api.get_ura_data", return_value=(self._projects(), [])), \
             patch("api.get_rental_data", return_value=[]), \
             patch("api._mrt_coords", return_value=[]), \
             patch("api._load_fallback_coords", return_value={}):
            body = client.get("/api/developments").json()
        api._dev_memo.update(txns=None, rentals=None, payload=None)
        self.assertEqual(len(body["districts"]), 28)
        self.assertEqual(body["districts"]["19"], "Hougang / Serangoon / Punggol")
        self.assertEqual(body["districts"]["01"], "Raffles Place / Marina / Cecil")
        for dev in body["developments"]:
            self.assertIn(dev["district"], body["districts"])

class TestExploreEncoding(unittest.TestCase):
    """The fields the colour ramp and the four filters read."""

    def _rentals(self):
        return [{"project": "ALPHA", "rental": [
            # 3 recent contracts → indexed; rent psf = 5000*12/1000 = 60/yr
            {"leaseDate": "0826", "areaSqft": "900-1100", "rent": 5000},
            {"leaseDate": "0726", "areaSqft": "900-1100", "rent": 5000},
            {"leaseDate": "0626", "areaSqft": "900-1100", "rent": 5000},
            {"leaseDate": "0120", "areaSqft": "900-1100", "rent": 9999},  # too old
        ]}, {"project": "BRAVO", "rental": [
            {"leaseDate": "0826", "areaSqft": "400-600", "rent": 3000},   # only 1 → dropped
        ]}]

    def test_rent_index_needs_three_recent_contracts(self):
        from api import build_rent_index
        idx = build_rent_index(self._rentals(), now=datetime(2026, 9, 1))
        self.assertAlmostEqual(idx["ALPHA"], 60.0, places=4)
        self.assertNotIn("BRAVO", idx)  # a single lease is not a yield

    def test_sqft_midpoint_handles_junk(self):
        from api import sqft_midpoint
        self.assertEqual(sqft_midpoint("900-1100"), 1000)
        self.assertIsNone(sqft_midpoint("NA"))
        self.assertIsNone(sqft_midpoint(None))

    def test_classify_tenure_buckets_every_lease_term(self):
        from api import classify_tenure
        self.assertEqual(classify_tenure([{"tenure": "Freehold"}]), "freehold")
        self.assertEqual(
            classify_tenure([{"tenure": "99 yrs lease commencing from 2018"}]), "99")
        # >= 900 years is its own bucket, not folded into freehold: 999-year
        # leases would otherwise be unfindable for anyone filtering for one.
        self.assertEqual(
            classify_tenure([{"tenure": "999 yrs lease commencing from 1876"}]), "999")
        self.assertEqual(
            classify_tenure([{"tenure": "9999 yrs lease commencing from 1876"}]), "999")
        self.assertEqual(
            classify_tenure([{"tenure": "946 yrs lease commencing from 1929"}]), "999")
        # Everything else in the data (60/70/85/93 and 100-115 yrs) shares one
        # bucket — nine developments between them.
        self.assertEqual(
            classify_tenure([{"tenure": "60 yrs lease commencing from 2011"}]), "other")
        self.assertEqual(
            classify_tenure([{"tenure": "103 yrs lease commencing from 2000"}]), "other")
        self.assertIsNone(classify_tenure([{"tenure": ""}]))
        self.assertIsNone(classify_tenure([{"tenure": "NA"}]))

    def test_classify_tenure_majority_wins(self):
        from api import classify_tenure
        self.assertEqual(classify_tenure([
            {"tenure": "99 yrs lease commencing from 2018"},
            {"tenure": "99 yrs lease commencing from 2018"},
            {"tenure": "Freehold"},
        ]), "99")

    def test_nearest_mrt_uses_cached_station_coords(self):
        from api import station_coords, nearest_mrt_m
        coords = station_coords({
            "A": {"lat": 1.3000, "lng": 103.9000},
            "B": {"lat": 1.4000, "lng": 103.9000},
            "BROKEN": {"name": "no coords"},
        })
        self.assertEqual(len(coords), 2)  # coordless station skipped
        self.assertEqual(nearest_mrt_m(1.3001, 103.9000, coords), 11)
        self.assertIsNone(nearest_mrt_m(1.3, 103.9, []))

    def test_build_developments_adds_encoding_fields(self):
        from api import build_developments, build_rent_index
        projects = [{
            "project": "ALPHA", "street": "A ST", "x": "28001.642", "y": "38744.572",
            "transaction": [
                {"propertyType": "Condominium", "district": "20", "contractDate": "0826",
                 "area": "100", "price": "2000000", "tenure": "Freehold"},
                {"propertyType": "Condominium", "district": "20", "contractDate": "0120",
                 "area": "100", "price": "1000000", "tenure": "Freehold"},
            ]}]
        devs = build_developments(
            projects, {}, rent_index=build_rent_index(self._rentals(), now=datetime(2026, 9, 1)),
            mrt_coords=[(1.366666, 103.833333)], now=datetime(2026, 9, 1))
        a = devs[0]
        self.assertEqual(a["tenure"], "freehold")
        self.assertEqual(a["last_txn"], "Aug 2026")      # newest across ALL txns
        self.assertLess(a["mrt_m"], 20)                  # station sits on the project
        self.assertEqual(a["avg_psf"], 1858)
        self.assertAlmostEqual(a["yield_pct"], round(60 / 1858 * 100, 2), places=2)

    def test_yield_is_none_without_a_recent_sale_psf(self):
        """No 12-mo transaction → no PSF → no yield, rather than a stale one."""
        from api import build_developments
        projects = [{
            "project": "ALPHA", "street": "A ST", "x": "28001.642", "y": "38744.572",
            "transaction": [{"propertyType": "Condominium", "district": "20",
                             "contractDate": "0120", "area": "100", "price": "1000000",
                             "tenure": "Freehold"}]}]
        devs = build_developments(projects, {}, rent_index={"ALPHA": 60.0},
                                  now=datetime(2026, 9, 1))
        self.assertIsNone(devs[0]["avg_psf"])
        self.assertIsNone(devs[0]["yield_pct"])
        self.assertEqual(devs[0]["txns_12mo"], 0)
        self.assertEqual(devs[0]["last_txn"], "Jan 2020")  # still dated for the filter

    def test_missing_mrt_cache_degrades_to_none(self):
        from api import build_developments
        projects = [{"project": "ALPHA", "street": "A ST", "x": "28001.642",
                     "y": "38744.572",
                     "transaction": [{"propertyType": "Condominium", "district": "20",
                                      "contractDate": "0826", "area": "100",
                                      "price": "2000000", "tenure": "Freehold"}]}]
        devs = build_developments(projects, {}, mrt_coords=[], now=datetime(2026, 9, 1))
        self.assertIsNone(devs[0]["mrt_m"])


class TestNearby(unittest.TestCase):
    """The map's nearby view: a radius filter over the explore-dot payload."""

    DEVS = [
        {"project": "ORIGIN", "street": "O ST", "district": "19", "lat": 1.3600,
         "lng": 103.8700, "avg_psf": 1500, "txns_12mo": 4, "yield_pct": 3.4,
         "tenure": "freehold", "mrt_m": 300, "last_txn": "Jul 2026"},
        {"project": "CLOSE", "street": "C ST", "district": "19", "lat": 1.3609,
         "lng": 103.8700, "avg_psf": 1600, "txns_12mo": 2, "yield_pct": None,
         "tenure": "99", "mrt_m": 700, "last_txn": "Jun 2026"},
        # ~1.3 km north — a different district too, which must NOT be what
        # decides inclusion (see nearby_developments' docstring).
        {"project": "FAR", "street": "F ST", "district": "20", "lat": 1.3720,
         "lng": 103.8700, "avg_psf": None, "txns_12mo": 0, "yield_pct": None,
         "tenure": None, "mrt_m": None, "last_txn": None},
        {"project": "MID", "street": "M ST", "district": "20", "lat": 1.3645,
         "lng": 103.8700, "avg_psf": 1400, "txns_12mo": 1, "yield_pct": 4.0,
         "tenure": "freehold", "mrt_m": 900, "last_txn": "Mar 2026"},
    ]

    def test_radius_filter_is_nearest_first_and_excludes_the_origin(self):
        from api import nearby_developments
        out = nearby_developments(self.DEVS, "origin", radius_m=1000)
        names = [r["project"] for r in out["results"]]
        self.assertEqual(names, ["CLOSE", "MID"])       # FAR is beyond 1 km
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["origin"]["project"], "ORIGIN")
        self.assertLess(out["results"][0]["distance_m"], out["results"][1]["distance_m"])

    def test_results_keep_the_full_dot_payload(self):
        """The popups are the explore popups — every field they read survives."""
        from api import nearby_developments
        row = nearby_developments(self.DEVS, "ORIGIN")["results"][0]
        for key in ("street", "district", "avg_psf", "txns_12mo", "yield_pct",
                    "tenure", "mrt_m", "last_txn", "lat", "lng"):
            self.assertIn(key, row)
        self.assertEqual(row["distance_m"], 100)

    def test_district_does_not_bound_the_radius(self):
        """nearby.nearby_for_project bounds candidates to the origin's own
        district; on a map that reads as a straight-line edge of missing dots
        (SANDY EIGHT: 171 developments within 1 km, only 86 in its district)."""
        from api import nearby_developments
        names = [r["project"] for r in nearby_developments(self.DEVS, "ORIGIN")["results"]]
        self.assertIn("MID", names)   # D20 neighbour of a D19 origin

    def test_limit_caps_results_but_not_the_total(self):
        from api import nearby_developments
        out = nearby_developments(self.DEVS, "ORIGIN", limit=1)
        self.assertEqual(len(out["results"]), 1)
        self.assertEqual(out["total"], 2)

    def test_explicit_origin_coordinate_wins(self):
        """The frontend passes the pin it is showing (URA x/y, or an exact
        postal coordinate) so the ring is centred on what the user sees."""
        from api import nearby_developments
        out = nearby_developments(self.DEVS, "NOT IN THE LIST",
                                  origin=(1.3600, 103.8700))
        self.assertEqual(out["origin"]["project"], "NOT IN THE LIST")
        # Exclusion is by name, so a dot at that exact spot under another name
        # is a genuine neighbour, not the origin repeated.
        self.assertEqual([r["project"] for r in out["results"]],
                         ["ORIGIN", "CLOSE", "MID"])

    def test_unknown_origin_without_coords_errors(self):
        from api import nearby_developments
        self.assertIn("error", nearby_developments(self.DEVS, "NOWHERE"))

    def test_nearby_endpoint(self):
        import api
        api._dev_memo.update(txns=None, rentals=None, payload=None)
        with patch("api.api_developments", return_value={"developments": self.DEVS}):
            data = client.get("/api/nearby", params={"q": "ORIGIN", "radius_m": 1000}).json()
        self.assertEqual([r["project"] for r in data["results"]], ["CLOSE", "MID"])
        self.assertEqual(data["radius_m"], 1000)


class TestHDBPayloads(unittest.TestCase):
    """The FLAT_TYPES parallel to the band shaping above — hdb.py's shapes are
    passed through unchanged; these helpers only put them in JSON's terms."""

    def test_flat_types_are_ordered_not_encounter_ordered(self):
        from utils import FLAT_TYPES
        jumbled = {"EXECUTIVE": 1, "3 ROOM": 2, "5 ROOM": 3}
        self.assertEqual(list(order_by_flat_type(jumbled)),
                         [ft for ft in FLAT_TYPES if ft in jumbled])

    def test_unknown_flat_type_still_gets_through(self):
        out = order_by_flat_type({"MULTI-GENERATION PLUS": 1, "4 ROOM": 2})
        self.assertEqual(list(out)[0], "4 ROOM")          # known ones lead
        self.assertIn("MULTI-GENERATION PLUS", out)       # nothing is dropped

    def test_shape_flat_types_keeps_the_datetime_out_of_json(self):
        """`latest` is a whole normalised row; serialising it wholesale would
        leak month_dt and every raw field into the response."""
        shaped = shape_flat_types(HDB_BLOCK["flat_types"])["4 ROOM"]
        self.assertNotIn("month_dt", shaped["latest"])
        self.assertNotIn("town", shaped["latest"])
        self.assertEqual(shaped["latest"]["month"], "2026-08")
        self.assertEqual(shaped["latest"]["area_sqft"], 1076)   # rounded for display
        self.assertEqual(shaped["typical_lease"], 65)

    def test_shape_flat_types_tolerates_a_missing_latest(self):
        out = shape_flat_types({"4 ROOM": {"count": 1, "median_price": 5, "avg_psf": None,
                                           "typical_lease": None, "latest": None}})
        self.assertIsNone(out["4 ROOM"]["latest"])

    def test_block_payload_declares_its_market_and_pin(self):
        p = build_hdb_block_payload(HDB_BLOCK, {"lat": 1.36, "lng": 103.84})
        self.assertEqual((p["market"], p["kind"]), ("hdb", "block"))
        self.assertEqual(p["development"], "Block 257 Bishan St 22")
        # A block coordinate is an ADDRESS geocode, exact in the way a street
        # geocode is not — the frontend feeds it straight to /api/amenities.
        self.assertTrue(p["exact_coords"])

    def test_block_payload_without_a_coordinate_is_still_an_answer(self):
        """Prices and the 5-year trend need no coordinate, so a geocode miss
        must not turn a good block into an error."""
        p = build_hdb_block_payload(HDB_BLOCK, None)
        self.assertIsNone(p["lat"])
        self.assertNotIn("exact_coords", p)
        self.assertEqual(p["total_txns"], 23)

    def test_street_payload_has_blocks_and_no_coordinate(self):
        p = build_hdb_street_payload(HDB_STREET)
        self.assertEqual((p["market"], p["kind"]), ("hdb", "street"))
        # A street spans blocks along its length; no single point represents it.
        self.assertIsNone(p["lat"])
        self.assertEqual(p["blocks"], [{"block": "257", "count": 5},
                                       {"block": "236", "count": 4}])


class TestHDBEndpoints(unittest.TestCase):

    @patch("api._hdb_records", return_value=[])
    @patch("api._geocode_hdb_block", return_value={"lat": 1.36, "lng": 103.84})
    @patch("api.hdb.block_detail", return_value=dict(HDB_BLOCK))
    @patch("api.hdb.resolve_query", return_value={"kind": "block", "block": "257", "street": "BISHAN ST 22"})
    def test_block_query(self, _rq, _bd, mock_geo, _recs):
        data = client.get("/api/hdb", params={"q": "257 bishan st 22"}).json()
        self.assertEqual(data["kind"], "block")
        self.assertEqual(data["block"], "257")
        mock_geo.assert_called_once()

    @patch("api._hdb_records", return_value=[])
    @patch("api.hdb.street_summary", return_value=dict(HDB_STREET))
    @patch("api.hdb.resolve_query", return_value={"kind": "street", "street": "BISHAN ST 22"})
    def test_street_query_is_never_geocoded(self, _rq, _ss, _recs):
        with patch("api._geocode_hdb_block") as mock_geo:
            data = client.get("/api/hdb", params={"q": "bishan st 22"}).json()
        self.assertEqual(data["kind"], "street")
        mock_geo.assert_not_called()

    @patch("api._hdb_records", return_value=[])
    @patch("api.hdb.resolve_query",
           return_value={"ambiguous": True, "block": "257", "candidates": ["BISHAN ST 22", "BISHAN ST 23"]})
    def test_ambiguous_carries_the_block_through(self, _rq, _recs):
        """Picking a street must re-ask for the same block, not drop the user
        at street level."""
        data = client.get("/api/hdb", params={"q": "257 bishan st"}).json()
        self.assertTrue(data["ambiguous"])
        self.assertEqual(data["block"], "257")
        self.assertEqual(data["market"], "hdb")

    @patch("api._hdb_records", return_value=[])
    @patch("api.hdb.resolve_query", return_value={"error": "No HDB blocks found."})
    def test_error_is_passed_through_and_labelled(self, _rq, _recs):
        data = client.get("/api/hdb", params={"q": "zzz"}).json()
        self.assertEqual(data["market"], "hdb")
        self.assertIn("No HDB blocks", data["error"])

    @patch("api._hdb_records", return_value=[])
    @patch("api.hdb.price_trend", return_value={"development": "Block 257 Bishan St 22"})
    def test_trend_takes_block_and_street(self, mock_trend, _recs):
        client.get("/api/hdb/trend", params={"street": "BISHAN ST 22", "block": "257"})
        self.assertEqual(mock_trend.call_args.args[:2], ("257", "BISHAN ST 22"))

    @patch("api._hdb_records", return_value=[])
    @patch("api.hdb.price_trend", return_value={"development": "Bishan St 22"})
    def test_trend_without_a_block_is_street_level(self, mock_trend, _recs):
        """hdb.price_trend takes block=None to aggregate a whole street, so a
        blank block must arrive as None rather than an empty string."""
        client.get("/api/hdb/trend", params={"street": "BISHAN ST 22", "block": ""})
        self.assertIsNone(mock_trend.call_args.args[0])

    @patch("api._hdb_meta_ts", return_value=1.0)
    @patch("api._hdb_records", return_value=[])
    @patch("api.hdb._normalise_all", return_value=[{"street": "ANG MO KIO AVE 6"},
                                                   {"street": "BISHAN ST 22"},
                                                   {"street": "ANG MO KIO AVE 6"}])
    def test_street_list_is_distinct_and_carries_both_spellings(self, *_):
        api_mod = sys.modules["api"]
        api_mod._hdb_streets_memo.update(ts=None, payload=None)   # cold
        data = client.get("/api/hdb/streets").json()
        self.assertEqual(data["count"], 2)
        amk = next(s for s in data["streets"] if s["s"] == "ANG MO KIO AVE 6")
        # The data abbreviates, users type either — so both forms ship.
        self.assertEqual(amk["c"], "ANG MO KIO AVENUE 6")

    @patch("api._hdb_meta_ts", return_value=7.0)
    @patch("api._hdb_records", return_value=[])
    def test_street_list_is_memoized_per_cache_refresh(self, mock_recs, _ts):
        """Deriving the list costs a full cache load; the list itself is tiny.
        Warm calls must not touch the records at all."""
        api_mod = sys.modules["api"]
        api_mod._hdb_streets_memo.update(ts=7.0, payload={"streets": [], "count": 0})
        client.get("/api/hdb/streets")
        mock_recs.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
