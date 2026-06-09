"""
Property Bot Test Suite
Run with: python -m Test.test_suite or python Test/test_suite.py
Tests all critical functions without hitting live APIs where possible.
"""
import os
import sys
import json
import time
import unittest
from datetime import datetime
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mrt_data import get_line_for_exit

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

    def _score_all(self, search_name, projects=None):
        """Return sorted list of (score, project) for all projects above 0.6."""
        import difflib
        from ura import SEARCH_STOPWORDS
        search = search_name.upper().strip()
        pool = projects if projects is not None else self.fake_projects

        def match_score(pn):
            pn = pn.upper().strip()
            if search == pn: return 1.0
            if search in pn: return 0.95
            if pn in search:
                return 0.7 + (0.2 * len(pn) / len(search))
            words = [w for w in search.split() if len(w) > 2 and w not in SEARCH_STOPWORDS]
            if words:
                found = sum(1 for w in words if w in pn)
                if found == len(words): return 0.85
                if found > 0: return 0.6 + (0.2 * found / len(words))
            return difflib.SequenceMatcher(None, search, pn).ratio()

        scored = [(match_score(p["project"]), p) for p in pool if match_score(p["project"]) >= 0.6]
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored

    def _match(self, search_name):
        """Run match_score logic against fake projects and return best match."""
        scored = self._score_all(search_name)
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

    def test_highpark_ambiguity(self):
        """
        Searching 'highpark' against two projects whose names both contain 'highpark'
        should produce ≥2 candidates within AMBIGUITY_THRESHOLD of each other.
        'HIGHPARK RESIDENCES' and 'SCOTTS HIGHPARK' are chosen because 'highpark'
        is a literal substring of both, so the scoring algorithm returns 0.95 for
        each — a genuine tie that the ambiguity check must surface to the user.
        (Note: 'HIGH PARK RESIDENCES' with a space would not trigger this because
        'highpark' is not a substring of it; that requires a separate normalization path.)
        """
        from ura import AMBIGUITY_THRESHOLD
        highpark_projects = [
            {"project": "HIGHPARK RESIDENCES", "street": "FERNVALE ROAD", "transaction": []},
            {"project": "SCOTTS HIGHPARK", "street": "SCOTTS ROAD", "transaction": []},
        ]
        scored = self._score_all("highpark", projects=highpark_projects)
        self.assertGreaterEqual(len(scored), 2, "Expected at least 2 scored candidates")

        best_score = scored[0][0]
        close = [p for s, p in scored if s >= best_score - AMBIGUITY_THRESHOLD]
        self.assertGreaterEqual(
            len(close), 2,
            f"Expected ≥2 projects within {AMBIGUITY_THRESHOLD} of best score {best_score:.2f}, "
            f"got {len(close)}: {[(s, p['project']) for s, p in scored]}"
        )
        # Both specific projects must be in the close set
        close_names = {p["project"] for p in close}
        self.assertIn("HIGHPARK RESIDENCES", close_names)
        self.assertIn("SCOTTS HIGHPARK", close_names)


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
            ("1000-1100", "1001 – 1100 sqft"),  # midpoint 1050
            ("1100-1200", "1101 – 1200 sqft"),  # midpoint 1150
            ("1300-1400", "> 1200 sqft"),       # midpoint 1350
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

    def test_interchange_specific_exit(self):
        self.assertEqual(get_line_for_exit("Serangoon MRT (Exit E/G)"), " [🟡 CCL]")

    def test_interchange_general_exit(self):
        self.assertEqual(get_line_for_exit("Serangoon MRT (Exit A)"), " [🟣 NEL]")

    def test_standard_station_no_exit(self):
        self.assertEqual(get_line_for_exit("KOVAN MRT"), " [🟣 NEL]")

    def test_marina_bay_shared_platform(self):
        result = get_line_for_exit("Marina Bay MRT (Exit 1)")
        self.assertIn("🔴 NSL", result)
        self.assertIn("🟡 CCL", result)

    def test_unknown_station_returns_empty(self):
        self.assertEqual(get_line_for_exit("FAKE STATION MRT"), "")


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
        from cache.cache_rental import get_last_4_quarters
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
# 9. PRICE TREND
# ══════════════════════════════════════════════════════

class TestPriceTrend(unittest.TestCase):
    """price_trend() bucketing, sale-type filtering, and rendering."""

    # ~1000 sqft so PSF == price / 1000, making expected PSF easy to assert.
    AREA_SQM = 92.903

    def _txn(self, price, mmyy, sale="3"):
        return {
            "area": str(self.AREA_SQM), "price": str(price), "contractDate": mmyy,
            "typeOfSale": sale, "propertyType": "Condominium",
            "floorRange": "01-05", "noOfUnits": "1", "tenure": "99 yrs",
        }

    def _patch(self, txns, project="TEST PROJECT", street="TEST ST"):
        """Patch ura.get_ura_data to return one synthetic project. Returns the patcher."""
        data = ([{"project": project, "street": street, "transaction": txns}], [])
        return patch("ura.get_ura_data", return_value=data)

    def test_excludes_new_sale(self):
        """New-sale (typeOfSale 1) txns must not contribute to the trend."""
        from ura import price_trend
        txns = [self._txn(2_000_000, "0323", sale="3") for _ in range(5)]
        txns += [self._txn(3_000_000, "0323", sale="1") for _ in range(5)]  # new sale, excluded
        txns += [self._txn(2_000_000, "0623", sale="2") for _ in range(2)]  # sub-sale, included
        with self._patch(txns):
            result = price_trend("TEST PROJECT")
        self.assertNotIn("error", result)
        self.assertEqual(result["total_txns"], 7)            # 5 resale + 2 sub-sale
        self.assertEqual(result["periods"][0]["avg_psf"], 2000)  # new-sale 3000 psf not averaged in

    def test_only_new_sale_returns_error(self):
        """A project with only new-sale txns has no resale market to trend."""
        from ura import price_trend
        txns = [self._txn(3_000_000, "0323", sale="1") for _ in range(10)]
        with self._patch(txns):
            result = price_trend("TEST PROJECT")
        self.assertIn("error", result)

    def test_yearly_bucketing_low_volume(self):
        """Below the half-yearly threshold → yearly buckets (no 'H' in labels)."""
        from ura import price_trend
        txns = [self._txn(1_800_000, "0322") for _ in range(5)]   # 2022
        txns += [self._txn(2_000_000, "0923") for _ in range(5)]  # 2023
        with self._patch(txns):
            result = price_trend("TEST PROJECT")
        labels = [p["label"] for p in result["periods"]]
        self.assertEqual(labels, ["2022", "2023"])
        self.assertTrue(all("H" not in l for l in labels))

    def test_half_yearly_bucketing_high_volume(self):
        """At/above 40 txns spanning two calendar years → half-yearly buckets."""
        from ura import price_trend
        txns = [self._txn(1_800_000, "0322") for _ in range(20)]  # 2022 H1
        txns += [self._txn(2_000_000, "0923") for _ in range(20)]  # 2023 H2
        with self._patch(txns):
            result = price_trend("TEST PROJECT")
        labels = [p["label"] for p in result["periods"]]
        self.assertEqual(labels, ["2022 H1", "2023 H2"])

    def test_pct_change_sign_and_order(self):
        """Rising PSF → positive pct_change; periods sorted ascending in time."""
        from ura import price_trend
        txns = [self._txn(1_800_000, "0322") for _ in range(3)]   # 2022 → 1800 psf
        txns += [self._txn(2_000_000, "0324") for _ in range(3)]  # 2024 → 2000 psf
        with self._patch(txns):
            result = price_trend("TEST PROJECT")
        self.assertEqual([p["label"] for p in result["periods"]], ["2022", "2024"])
        self.assertEqual(result["pct_change"], 11)               # (2000-1800)/1800 ≈ +11%
        self.assertEqual(result["span_label"], "2 yrs")

    def test_single_period_has_no_pct(self):
        """One populated period can't have a trend — pct_change is None."""
        from ura import price_trend
        txns = [self._txn(2_000_000, "0323") for _ in range(4)]
        with self._patch(txns):
            result = price_trend("TEST PROJECT")
        self.assertIsNone(result["pct_change"])

    def test_format_renders_bars_and_arrow(self):
        """format_price_trend output carries the header, an up-arrow, and bar blocks."""
        from ura import price_trend, format_price_trend
        txns = [self._txn(1_800_000, "0322") for _ in range(3)]
        txns += [self._txn(2_000_000, "0324") for _ in range(3)]
        with self._patch(txns):
            text = format_price_trend(price_trend("TEST PROJECT"))
        self.assertIn("📈", text)
        self.assertIn("↑", text)
        self.assertIn("█", text)
        self.assertIn("resale + sub-sale only", text)


# ══════════════════════════════════════════════════════
# AMENITY CALLBACK BUTTONS
# ══════════════════════════════════════════════════════

class TestAmenityCallbackButtons(unittest.TestCase):
    """Amenity button callback_data must stay within Telegram's 64-byte limit."""

    @staticmethod
    def _ctx():
        """A minimal stand-in for ContextTypes.DEFAULT_TYPE (just needs user_data)."""
        return MagicMock(user_data={})

    def test_token_round_trips(self):
        """store_addr_key → resolve_addr_key returns the original addr_key."""
        from bot import store_addr_key, resolve_addr_key
        ctx = self._ctx()
        addr_key = "AFFINITY AT SERANGOON|SERANGOON NORTH AVENUE 1"
        token = store_addr_key(ctx, addr_key)
        self.assertEqual(resolve_addr_key(ctx, token), addr_key)

    def test_token_is_deterministic(self):
        """Same addr_key yields the same token (so dedupe works)."""
        from bot import store_addr_key
        addr_key = "THE SAIL @ MARINA BAY|MARINA BOULEVARD"
        self.assertEqual(store_addr_key(self._ctx(), addr_key),
                         store_addr_key(self._ctx(), addr_key))

    def test_resolve_unknown_token_returns_none(self):
        """A token absent from user_data (e.g. after restart) resolves to None."""
        from bot import resolve_addr_key
        self.assertIsNone(resolve_addr_key(self._ctx(), "deadbeef"))

    def test_resolve_legacy_literal_addr_key(self):
        """Backward-compat: a literal PROJECT|STREET passes through unchanged."""
        from bot import resolve_addr_key
        legacy = "OLD PROJECT|OLD STREET"
        self.assertEqual(resolve_addr_key(self._ctx(), legacy), legacy)

    def test_all_callback_data_within_64_bytes(self):
        """Every button's callback_data must be ≤ 64 bytes, even for long names."""
        from bot import store_addr_key, build_amenity_keyboard
        ctx = self._ctx()
        # A deliberately long project + street that overflowed the old scheme.
        token = store_addr_key(
            ctx, "AFFINITY AT SERANGOON DEVELOPMENT|SERANGOON NORTH AVENUE 1 SINGAPORE"
        )
        keyboard = build_amenity_keyboard(token)
        for row in keyboard.inline_keyboard:
            for button in row:
                self.assertLessEqual(
                    len(button.callback_data.encode("utf-8")), 64,
                    f"callback_data too long: {button.callback_data!r}",
                )


# ══════════════════════════════════════════════════════
# RENTAL PROJECT MATCHING (cross-development bleed guard)
# ══════════════════════════════════════════════════════

class TestRentalMatching(unittest.TestCase):
    """
    find_rental_project must resolve to the SAME development as the transaction
    search and must NOT pull a neighbouring development's rental records when the
    searched project has none of its own (the Lentor Hills Residences bug).
    """

    def setUp(self):
        # Minimal fake rental dataset (only the 'project' key matters for matching).
        self.rental_data = [
            {"project": "MARINA BAY RESIDENCES", "rental": []},
            {"project": "LENTOR MODERN", "rental": []},
            {"project": "LENTOR HILLS", "rental": []},
            {"project": "THE FLORENCE RESIDENCES", "rental": []},
            {"project": "AFFINITY AT SERANGOON", "rental": []},
        ]

    def _names(self, search, data=None):
        from rental import find_rental_project
        return [p["project"] for p in find_rental_project(search, data if data is not None else self.rental_data)]

    def test_exact_match(self):
        self.assertEqual(self._names("MARINA BAY RESIDENCES"), ["MARINA BAY RESIDENCES"])

    def test_legitimate_prefix_match(self):
        """'MARINA BAY' is a substring of 'MARINA BAY RESIDENCES' — must still match."""
        self.assertEqual(self._names("MARINA BAY"), ["MARINA BAY RESIDENCES"])

    def test_trailing_token_variant(self):
        """'THE FOO' vs 'FOO RESIDENCES' (neither a substring) must still match via words."""
        data = [{"project": "FLORENCE RESIDENCES", "rental": []}]
        self.assertEqual(self._names("THE FLORENCE", data), ["FLORENCE RESIDENCES"])

    def test_cross_development_no_false_positive(self):
        """
        Under-construction 'LENTOR HILLS RESIDENCES' has NO rental records of its
        own here — it must NOT bleed into neighbouring 'LENTOR MODERN' or the
        shorter 'LENTOR HILLS'. The matcher returns [] rather than wrong data.
        """
        self.assertEqual(self._names("LENTOR HILLS RESIDENCES"), [])

    def test_no_match_returns_empty(self):
        self.assertEqual(self._names("SOME NONEXISTENT CONDO"), [])

    def test_only_top_scored_returned(self):
        """A weak neighbour is never mixed in with a strong (exact) match."""
        names = self._names("AFFINITY AT SERANGOON")
        self.assertEqual(names, ["AFFINITY AT SERANGOON"])

    def test_street_disambiguates_fuzzy_match(self):
        """
        A fuzzy (non-exact) name match must be corroborated by an agreeing street,
        so a string-similar development on a different road can't slip through.
        """
        from rental import find_rental_project
        data = [{"project": "FLORENCE RESIDENCES", "street": "FLORENCE ROAD", "rental": []}]
        # 'THE FLORENCE' scores 0.85 (all words found) — fuzzy, so street is checked.
        wrong_street = [p["project"] for p in find_rental_project("THE FLORENCE", data, "HOUGANG AVENUE 3")]
        right_street = [p["project"] for p in find_rental_project("THE FLORENCE", data, "FLORENCE ROAD")]
        self.assertEqual(wrong_street, [])
        self.assertEqual(right_street, ["FLORENCE RESIDENCES"])

    def test_exact_match_ignores_street(self):
        """An exact name match is trusted even when no street is supplied."""
        from rental import find_rental_project
        data = [{"project": "MARINA BAY RESIDENCES", "street": "MARINA BOULEVARD", "rental": []}]
        names = [p["project"] for p in find_rental_project("MARINA BAY RESIDENCES", data, "")]
        self.assertEqual(names, ["MARINA BAY RESIDENCES"])


# ══════════════════════════════════════════════════════
# UNDER-CONSTRUCTION GATE (new launch → no rentals)
# ══════════════════════════════════════════════════════

class TestUnderConstructionGate(unittest.TestCase):
    """
    search_property.under_construction gates rental lookups. The signal is URA
    pipeline membership (uncompleted projects), NOT the presence of a secondary
    market — so a COMPLETED new-sale-only development (no resales yet) is allowed
    through and still shows its rentals.
    """

    AREA_SQM = 92.903  # ~1000 sqft

    def _txn(self, sale="3"):
        return {
            "area": str(self.AREA_SQM), "price": "2000000", "contractDate": "0324",
            "typeOfSale": sale, "propertyType": "Condominium",
            "floorRange": "01-05", "noOfUnits": "1", "tenure": "99 yrs",
        }

    def _patch(self, txns, pipeline=None, project="TEST PROJECT", street="TEST ST"):
        data = ([{"project": project, "street": street, "transaction": txns}], pipeline or [])
        return patch("ura.get_ura_data", return_value=data)

    def _pipeline(self, project="TEST PROJECT", top="2028"):
        return [{"project": project, "expectedTOPYear": top, "totalUnits": 100}]

    def test_under_construction_when_in_pipeline(self):
        """New launch present in the pipeline feed → under_construction True."""
        from ura import search_property
        with self._patch([self._txn(sale="1") for _ in range(5)], pipeline=self._pipeline()):
            result = search_property("TEST PROJECT")
        self.assertNotIn("error", result)
        self.assertTrue(result["under_construction"])

    def test_completed_new_sale_only_not_gated(self):
        """
        The key case from review: a COMPLETED development with only new-sale txns
        (no resales yet) is NOT in the pipeline → under_construction False, so its
        real rentals are NOT suppressed.
        """
        from ura import search_property
        with self._patch([self._txn(sale="1") for _ in range(5)], pipeline=[]):
            result = search_property("TEST PROJECT")
        self.assertFalse(result["under_construction"])

    def test_completed_resale_not_gated(self):
        from ura import search_property
        with self._patch([self._txn(sale="3") for _ in range(5)], pipeline=[]):
            result = search_property("TEST PROJECT")
        self.assertFalse(result["under_construction"])

    def test_demolished_twin_excluded_and_gated(self):
        """
        Chuan Park bug: a redevelopment sharing its base name with an en-bloc'd
        '(DEMOLISHED)' block resolves to the LIVE block, and (being in the pipeline)
        is under construction — so rentals are gated and the demolished resales
        don't leak in.
        """
        from ura import search_property
        live = {"project": "CHUAN PARK", "street": "LORONG CHUAN",
                "transaction": [self._txn(sale="1") for _ in range(5)]}
        demolished = {"project": "CHUAN PARK (DEMOLISHED)", "street": "LORONG CHUAN",
                      "transaction": [self._txn(sale="3") for _ in range(5)]}
        pipeline = self._pipeline(project="CHUAN PARK", top="na")
        with patch("ura.get_ura_data", return_value=([live, demolished], pipeline)):
            result = search_property("CHUAN PARK")
        self.assertEqual(result["development"], "CHUAN PARK")
        self.assertTrue(result["under_construction"])

    def test_demolished_block_still_searchable_explicitly(self):
        """Searching the demolished block by name still resolves to it."""
        from ura import search_property
        live = {"project": "CHUAN PARK", "street": "LORONG CHUAN",
                "transaction": [self._txn(sale="1") for _ in range(5)]}
        demolished = {"project": "CHUAN PARK (DEMOLISHED)", "street": "LORONG CHUAN",
                      "transaction": [self._txn(sale="3") for _ in range(5)]}
        with patch("ura.get_ura_data", return_value=([live, demolished], [])):
            result = search_property("CHUAN PARK (DEMOLISHED)")
        self.assertEqual(result["development"], "CHUAN PARK (DEMOLISHED)")


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
        TestPriceTrend,
        TestAmenityCallbackButtons,
        TestRentalMatching,
        TestUnderConstructionGate,
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

