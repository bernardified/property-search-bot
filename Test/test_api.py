"""
Tests for api.py's pure payload shaping + endpoint routing.
Run with: python -m Test.test_api
All external calls (URA cache, rental, geocode, Mongo) are mocked — no network.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import app, build_property_payload, order_by_band, sale_prices_from_bands

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


class TestEndpoints(unittest.TestCase):

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

    @patch("api.get_recent_searches", return_value=[{"name": "PARC ESTA", "count": 5}])
    def test_list(self, mock_recent):
        data = client.get("/api/list").json()
        self.assertEqual(data["searches"][0]["name"], "PARC ESTA")
        mock_recent.assert_called_once_with(limit=10)

    def test_static_frontend_served_at_root(self):
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("SG Property Map", r.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
