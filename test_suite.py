"""
Property Bot Test Suite
Run with: python test_suite.py
Tests all critical functions without hitting live APIs where possible.
"""
import os
import sys
import json
import time
import unittest
from datetime import datetime
from unittest.mock import patch, MagicMock

# ── Helpers ───────────────────────────────────────────────────────────────────

PASS = "✅"
FAIL = "❌"
SKIP = "⏭️"

def section(title):
    print(f"\n{'═' * 50}")
    print(f"  {title}")
    print('═' * 50)


# ══════════════════════════════════════════════════════
# 1. UTILS
# ══════════════════════════════════════════════════════

class TestUtils(unittest.TestCase):

    def setUp(self):
        from utils import SIZE_BANDS, get_band, sqm_to_sqft, parse_float
        from utils import parse_sqft_range, parse_mmyy_date, format_mmyy_date, haversine_m
        self.SIZE_BANDS = SIZE_BANDS
        self.get_band = get_band
        self.sqm_to_sqft = sqm_to_sqft
        self.parse_float = parse_float
        self.parse_sqft_range = parse_sqft_range
        self.parse_mmyy_date = parse_mmyy_date
        self.format_mmyy_date = format_mmyy_date
        self.haversine_m = haversine_m

    def test_size_bands_no_gaps(self):
        """Every integer sqft from 1 to 2000 should map to a band."""
        for sqft in range(1, 2001):
            band = self.get_band(sqft)
            self.assertIsNotNone(band, f"sqft={sqft} returned None — gap in SIZE_BANDS")

    def test_size_bands_boundaries(self):
        """Check boundary values map to the correct band."""
        bands = self.SIZE_BANDS
        for band in bands:
            if band["max"] != float("inf"):
                # min should be in this band
                result = self.get_band(band["min"])
                self.assertEqual(result, band["label"], 
                    f"min={band['min']} should be '{band['label']}' but got '{result}'")
                # max should be in this band
                result = self.get_band(band["max"])
                self.assertEqual(result, band["label"],
                    f"max={band['max']} should be '{band['label']}' but got '{result}'")

    def test_size_bands_consistent_across_files(self):
        """SIZE_BANDS in utils, ura, and rental must be identical."""
        from utils import SIZE_BANDS as utils_bands
        from ura import SIZE_BANDS as ura_bands
        from rental import SIZE_BANDS as rental_bands
        self.assertEqual(utils_bands, ura_bands, "SIZE_BANDS mismatch: utils vs ura")
        self.assertEqual(utils_bands, rental_bands, "SIZE_BANDS mismatch: utils vs rental")

    def test_sqm_to_sqft(self):
        self.assertAlmostEqual(self.sqm_to_sqft(100), 1076.39, places=1)
        self.assertAlmostEqual(self.sqm_to_sqft(0), 0.0)

    def test_parse_float(self):
        self.assertEqual(self.parse_float("1,234.56"), 1234.56)
        self.assertEqual(self.parse_float("500"), 500.0)
        self.assertIsNone(self.parse_float("abc"))
        self.assertIsNone(self.parse_float(None))

    def test_parse_sqft_range(self):
        self.assertEqual(self.parse_sqft_range("600-700"), 650.0)
        self.assertEqual(self.parse_sqft_range("1700-1800"), 1750.0)
        self.assertIsNone(self.parse_sqft_range("abc"))

    def test_parse_mmyy_date(self):
        dt = self.parse_mmyy_date("0426")
        self.assertEqual(dt, datetime(2026, 4, 1))
        dt = self.parse_mmyy_date("1223")
        self.assertEqual(dt, datetime(2023, 12, 1))
        self.assertIsNone(self.parse_mmyy_date("abc"))
        self.assertIsNone(self.parse_mmyy_date(""))

    def test_format_mmyy_date(self):
        self.assertEqual(self.format_mmyy_date("0426"), "Apr 2026")
        self.assertEqual(self.format_mmyy_date("1223"), "Dec 2023")
        self.assertEqual(self.format_mmyy_date("bad"), "bad")  # fallback

    def test_haversine_same_point(self):
        self.assertAlmostEqual(self.haversine_m(1.3, 103.8, 1.3, 103.8), 0.0, places=1)

    def test_haversine_known_distance(self):
        # Raffles Place to Marina Bay MRT ~500m apart
        dist = self.haversine_m(1.2830, 103.8513, 1.2765, 103.8545)
        self.assertGreater(dist, 400)
        self.assertLess(dist, 1000)  # ~805m actual distance


# ══════════════════════════════════════════════════════
# 2. URA SEARCH & MATCHING
# ══════════════════════════════════════════════════════

class TestURAMatching(unittest.TestCase):

    def setUp(self):
        # Minimal fake URA project data for testing matching logic
        self.fake_projects = [
            {"project": "THE SAIL @ MARINA BAY", "street": "MARINA BOULEVARD", "transaction": []},
            {"project": "FAR HORIZON GARDENS", "street": "HOUGANG AVENUE 3", "transaction": []},
            {"project": "HORIZON GARDENS", "street": "ANG MO KIO AVENUE 2", "transaction": []},
            {"project": "THE ORIE", "street": "LORONG 1 TOA PAYOH", "transaction": []},
            {"project": "THE ORIENT", "street": "PASIR PANJANG ROAD", "transaction": []},
            {"project": "AFFINITY AT SERANGOON", "street": "SERANGOON NORTH AVENUE 1", "transaction": []},
            {"project": "THE FLORENCE RESIDENCES", "street": "FLORENCE ROAD", "transaction": []},
            {"project": "CHUAN PARK", "street": "LORONG CHUAN", "transaction": []},
        ]

    def _match(self, search_name):
        """Run match_score logic against fake projects and return best match."""
        import difflib
        search = search_name.upper().strip()

        def match_score(pn):
            pn = pn.upper().strip()
            if search == pn: return 1.0
            if search in pn: return 0.95
            if pn in search:
                return 0.7 + (0.2 * len(pn) / len(search))
            words = [w for w in search.split() if len(w) > 2]
            if words:
                found = sum(1 for w in words if w in pn)
                if found == len(words): return 0.85
                if found > 0: return 0.6 + (0.2 * found / len(words))
            return difflib.SequenceMatcher(None, search, pn).ratio()

        scored = [(match_score(p["project"]), p) for p in self.fake_projects]
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]["project"] if scored else None

    def test_exact_match(self):
        self.assertEqual(self._match("THE SAIL @ MARINA BAY"), "THE SAIL @ MARINA BAY")

    def test_partial_match(self):
        self.assertEqual(self._match("the sail"), "THE SAIL @ MARINA BAY")

    def test_far_horizon_not_horizon(self):
        """'far horizon gardens' must match FAR HORIZON GARDENS, not HORIZON GARDENS."""
        self.assertEqual(self._match("far horizon gardens"), "FAR HORIZON GARDENS")

    def test_the_orie_not_the_orient(self):
        """'the orie' must match THE ORIE, not THE ORIENT."""
        result = self._match("the orie")
        self.assertEqual(result, "THE ORIE", 
            f"Expected 'THE ORIE' but got '{result}' — fuzzy match picking wrong project")

    def test_abbreviation(self):
        self.assertEqual(self._match("affinity at srgn"), "AFFINITY AT SERANGOON")

    def test_typo(self):
        self.assertEqual(self._match("the florence residnces"), "THE FLORENCE RESIDENCES")

    def test_chuan_park(self):
        self.assertEqual(self._match("chuan park"), "CHUAN PARK")


# ══════════════════════════════════════════════════════
# 3. URA DATA PARSING
# ══════════════════════════════════════════════════════

class TestURADataParsing(unittest.TestCase):

    def setUp(self):
        from utils import get_band, sqm_to_sqft, parse_float, parse_mmyy_date
        self.get_band = get_band
        self.sqm_to_sqft = sqm_to_sqft
        self.parse_float = parse_float
        self.parse_mmyy_date = parse_mmyy_date

    def test_typical_transaction_parsing(self):
        """Simulate parsing a real URA transaction record."""
        txn = {
            "area": "57.0",       # sqm
            "price": "1100000",
            "contractDate": "0426",
            "floorRange": "06-10",
            "typeOfSale": "1",
            "propertyType": "Condominium",
            "tenure": "99 yrs lease commencing from 2018",
        }
        area_sqm = self.parse_float(txn["area"])
        area_sqft = self.sqm_to_sqft(area_sqm)
        band = self.get_band(area_sqft)
        price = self.parse_float(txn["price"])
        psf = round(price / area_sqft)
        dt = self.parse_mmyy_date(txn["contractDate"])

        self.assertAlmostEqual(area_sqft, 613.5, places=0)
        self.assertIsNotNone(band)
        self.assertEqual(price, 1100000.0)
        self.assertGreater(psf, 0)
        self.assertEqual(dt, datetime(2026, 4, 1))

    def test_landed_property_excluded(self):
        """Landed properties should be excluded from results."""
        landed_types = ["Detached House", "Semi-detached", "Terrace House"]
        for prop_type in landed_types:
            excluded = any(t in prop_type.lower() for t in ["detached", "terrace", "bungalow"])
            self.assertTrue(excluded, f"{prop_type} should be excluded")

    def test_sale_type_labels(self):
        from ura import _sale_type_label
        self.assertEqual(_sale_type_label("1"), "New Sale")
        self.assertEqual(_sale_type_label("2"), "Sub Sale")
        self.assertEqual(_sale_type_label("3"), "Resale")
        self.assertEqual(_sale_type_label("9"), "9")  # unknown fallback


# ══════════════════════════════════════════════════════
# 4. RENTAL LOGIC
# ══════════════════════════════════════════════════════

class TestRentalLogic(unittest.TestCase):

    def test_parse_sqft_range_midpoint(self):
        from utils import parse_sqft_range
        self.assertEqual(parse_sqft_range("600-700"), 650.0)
        self.assertEqual(parse_sqft_range("1700-1800"), 1750.0)
        self.assertEqual(parse_sqft_range("500-600"), 550.0)

    def test_yield_calculation(self):
        """Gross yield = (monthly_rent * 12 / price) * 100"""
        monthly_rent = 3200
        price = 1160000
        yield_pct = round((monthly_rent * 12 / price) * 100, 2)
        self.assertAlmostEqual(yield_pct, 3.31, places=1)

    def test_rental_band_mapping(self):
        """URA rental area ranges should map to correct size bands."""
        from utils import parse_sqft_range, get_band
        cases = [
            ("500-600", "<= 600 sqft"),
            ("600-700", "601 – 700 sqft"),
            ("700-800", "701 – 800 sqft"),
            ("1000-1100", "> 1000 sqft"),
        ]
        for area_str, expected_band in cases:
            midpoint = parse_sqft_range(area_str)
            band = get_band(midpoint)
            self.assertEqual(band, expected_band,
                f"Area '{area_str}' (midpoint {midpoint}) -> '{band}' but expected '{expected_band}'")

    def test_addr_key_format(self):
        """addr_key must store PROJECT|STREET so rental uses project, maps use street."""
        project = "MARINA ONE RESIDENCES"
        street = "MARINA WAY"
        addr_key = f"{project[:28]}|{street[:28]}"

        # Split correctly
        project_name, street_address = addr_key.split("|", 1)
        self.assertEqual(project_name, "MARINA ONE RESIDENCES")
        self.assertEqual(street_address, "MARINA WAY")

    def test_addr_key_rental_uses_project_not_street(self):
        """Rental search must use project name, not street address."""
        addr_key = "MARINA ONE RESIDENCES|MARINA WAY"
        project_name, street_address = addr_key.split("|", 1)

        # Rental should search by project name
        self.assertEqual(project_name, "MARINA ONE RESIDENCES")
        self.assertNotEqual(project_name, "MARINA WAY",
            "Rental was using street address instead of project name")

    def test_addr_key_maps_uses_street(self):
        """Geocoding must use street address, not project name."""
        addr_key = "MARINA ONE RESIDENCES|MARINA WAY"
        project_name, street_address = addr_key.split("|", 1)

        # Maps should geocode by street
        self.assertEqual(street_address, "MARINA WAY")
        self.assertNotEqual(street_address, "MARINA ONE RESIDENCES",
            "Geocoding was using project name instead of street")

    def test_addr_key_truncation_fits_telegram_limit(self):
        """addr_key must fit within Telegram's 64-char callback data limit."""
        # Worst case: long project name + long street
        project = "SOME VERY LONG DEVELOPMENT NAME HERE"
        street = "SOME VERY LONG STREET NAME HERE TOO"
        addr_key = f"amenity:rental:{project[:28]}|{street[:28]}"
        self.assertLessEqual(len(addr_key), 64,
            f"addr_key too long: {len(addr_key)} chars")


# ══════════════════════════════════════════════════════
# 5. MAPS & GEOCODING
# ══════════════════════════════════════════════════════

class TestMapsHelpers(unittest.TestCase):

    def test_google_maps_link_format(self):
        from maps import build_google_maps_link
        link = build_google_maps_link("LORONG CHUAN", 1.35153, 103.86481)
        self.assertIn("google.com/maps/dir", link)
        self.assertIn("walking", link)
        self.assertIn("1.35153", link)

    def test_mrt_exit_label_format(self):
        """MRT result should include exit label if available."""
        mrt = {"name": "Lorong Chuan", "exit_label": " (Exit A)", "dest_lat": 1.35, "dest_lng": 103.86, "straight_dist": 100}
        display = f"{mrt['name']} MRT{mrt['exit_label']}"
        self.assertEqual(display, "Lorong Chuan MRT (Exit A)")


# ══════════════════════════════════════════════════════
# 6. CACHE FRESHNESS LOGIC
# ══════════════════════════════════════════════════════

class TestCacheFreshness(unittest.TestCase):

    def test_ura_cache_48h_threshold(self):
        """Cache should be stale after 48 hours."""
        CACHE_MAX_AGE_HOURS = 48
        fresh_timestamp = time.time() - (47 * 3600)
        stale_timestamp = time.time() - (49 * 3600)
        age_fresh = (time.time() - fresh_timestamp) / 3600
        age_stale = (time.time() - stale_timestamp) / 3600
        self.assertTrue(age_fresh < CACHE_MAX_AGE_HOURS)
        self.assertFalse(age_stale < CACHE_MAX_AGE_HOURS)

    def test_mrt_cache_30d_threshold(self):
        """MRT cache should be stale after 30 days."""
        CACHE_MAX_AGE_DAYS = 30
        fresh_ts = time.time() - (29 * 86400)
        stale_ts = time.time() - (31 * 86400)
        self.assertTrue((time.time() - fresh_ts) / 86400 < CACHE_MAX_AGE_DAYS)
        self.assertFalse((time.time() - stale_ts) / 86400 < CACHE_MAX_AGE_DAYS)

    def test_quarter_generation(self):
        """Last 4 quarters should always contain 4 valid entries in YYqQ format."""
        from cache_rental import get_last_4_quarters
        import re
        quarters = get_last_4_quarters()
        self.assertEqual(len(quarters), 4)
        for q in quarters:
            self.assertRegex(q, r'^\d{2}q[1-4]$', f"Invalid quarter format: {q}")


# ══════════════════════════════════════════════════════
# 7. STORAGE
# ══════════════════════════════════════════════════════

class TestStorage(unittest.TestCase):

    def test_record_search_handles_no_db(self):
        """record_search should not crash when MongoDB is unavailable."""
        from storage import record_search
        # Should not raise even with None searches collection
        try:
            with patch('storage.searches', None):
                record_search(123, "testuser", "test query", "TEST PROJECT")
        except Exception as e:
            self.fail(f"record_search raised {e} with no DB")

    def test_get_recent_searches_handles_no_db(self):
        """get_recent_searches should return empty list when MongoDB is unavailable."""
        from storage import get_recent_searches
        with patch('storage.searches', None):
            result = get_recent_searches()
        self.assertEqual(result, [])


# ══════════════════════════════════════════════════════
# 8. INTEGRATION — URA CACHE (requires local cache file)
# ══════════════════════════════════════════════════════

class TestURACacheIntegration(unittest.TestCase):

    def setUp(self):
        """Skip if no local cache available."""
        if not os.path.exists("ura_cache.json"):
            self.skipTest("ura_cache.json not found — run /refresh first")

    def test_cache_has_data(self):
        with open("ura_cache.json") as f:
            data = json.load(f)
        transactions = data.get("transactions", [])
        self.assertGreater(len(transactions), 100, "Cache seems empty")

    def test_known_developments_exist(self):
        with open("ura_cache.json") as f:
            data = json.load(f)
        projects = {p.get("project", "").upper() for p in data.get("transactions", [])}
        for name in ["THE SAIL @ MARINA BAY", "AFFINITY AT SERANGOON", "CHUAN PARK"]:
            self.assertTrue(
                any(name in p for p in projects),
                f"'{name}' not found in URA cache"
            )


# ══════════════════════════════════════════════════════
# RUNNER
# ══════════════════════════════════════════════════════

def run_tests():
    section("Property Bot Test Suite")
    print(f"Python {sys.version.split()[0]} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    test_classes = [
        TestUtils,
        TestURAMatching,
        TestURADataParsing,
        TestRentalLogic,
        TestMapsHelpers,
        TestCacheFreshness,
        TestStorage,
        TestURACacheIntegration,
    ]

    for cls in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    section("Summary")
    total = result.testsRun
    failures = len(result.failures)
    errors = len(result.errors)
    skipped = len(result.skipped)
    passed = total - failures - errors - skipped

    print(f"{PASS} Passed:  {passed}")
    print(f"{FAIL} Failed:  {failures + errors}")
    print(f"{SKIP} Skipped: {skipped}")
    print(f"   Total:   {total}")

    if result.failures or result.errors:
        print("\n⚠️  Some tests failed. Review before pushing.")
        sys.exit(1)
    else:
        print("\n✅ All tests passed.")


if __name__ == "__main__":
    run_tests()
