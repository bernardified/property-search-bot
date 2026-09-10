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
    build_property_payload,
    order_by_band,
    project_xy_coords,
    sale_prices_from_bands,
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

    @patch("api.search_property")
    @patch("api.is_hdb_residential_block", return_value=True)
    @patch("api.resolve_postal_code", return_value={**RESOLVED_PRIVATE, "building": "WOODLEIGH GLEN"})
    def test_hdb_block_is_not_searched_as_private(self, _res, mock_hdb, mock_search):
        data = client.get("/api/property", params={"q": "361206"}).json()
        mock_hdb.assert_called_once_with("8", "SIMS AVENUE")
        mock_search.assert_not_called()  # never fuzzy-matched to a nearby condo
        self.assertIn("HDB", data["error"])

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

    def test_classify_tenure_buckets_999_year_leases_as_freehold(self):
        from api import classify_tenure
        self.assertEqual(classify_tenure([{"tenure": "Freehold"}]), "freehold")
        self.assertEqual(
            classify_tenure([{"tenure": "99 yrs lease commencing from 2018"}]), "leasehold")
        self.assertEqual(
            classify_tenure([{"tenure": "999 yrs lease commencing from 1876"}]), "freehold")
        self.assertEqual(
            classify_tenure([{"tenure": "9999 yrs lease commencing from 1876"}]), "freehold")
        self.assertIsNone(classify_tenure([{"tenure": ""}]))

    def test_classify_tenure_majority_wins(self):
        from api import classify_tenure
        self.assertEqual(classify_tenure([
            {"tenure": "99 yrs lease commencing from 2018"},
            {"tenure": "99 yrs lease commencing from 2018"},
            {"tenure": "Freehold"},
        ]), "leasehold")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
