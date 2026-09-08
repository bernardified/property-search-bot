"""
Tests for the in-process memo on cache_ura.get_ura_data / cache_rental.get_rental_data.
Run with: python -m Test.test_cache_memo
Mongo and staleness checks are mocked — no network, no DB.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cache.cache_ura as cache_ura
import cache.cache_rental as cache_rental

TXNS = [{"project": "PARC ESTA"}]
PIPELINE = [{"project": "LENTOR MODERN"}]
RENTALS = [{"project": "PARC ESTA", "rental": []}]


class TestUraMemo(unittest.TestCase):

    def setUp(self):
        cache_ura._memo_ts = None
        cache_ura._memo_data = None

    @patch("cache.cache_ura.is_ura_transactions_stale", return_value=False)
    @patch("cache.cache_ura._load_cache", return_value=(TXNS, PIPELINE))
    @patch("cache.cache_ura._meta_timestamp", return_value=1000.0)
    def test_second_call_hits_memo(self, _ts, mock_load, _stale):
        first = cache_ura.get_ura_data()
        second = cache_ura.get_ura_data()
        self.assertEqual(mock_load.call_count, 1)
        self.assertIs(first, second)  # same object — memoized, not reloaded

    @patch("cache.cache_ura.is_ura_transactions_stale", return_value=False)
    @patch("cache.cache_ura._load_cache", return_value=(TXNS, PIPELINE))
    @patch("cache.cache_ura._meta_timestamp")
    def test_new_timestamp_invalidates_memo(self, mock_ts, mock_load, _stale):
        mock_ts.return_value = 1000.0
        cache_ura.get_ura_data()
        mock_ts.return_value = 2000.0  # another process refreshed
        cache_ura.get_ura_data()
        self.assertEqual(mock_load.call_count, 2)

    @patch("cache.cache_ura.is_ura_transactions_stale", return_value=False)
    @patch("cache.cache_ura._load_cache", return_value=([], []))
    @patch("cache.cache_ura._meta_timestamp", return_value=1000.0)
    def test_empty_load_is_not_memoized(self, _ts, mock_load, _stale):
        cache_ura.get_ura_data()
        cache_ura.get_ura_data()
        self.assertEqual(mock_load.call_count, 2)

    @patch("cache.cache_ura._fetch_pipeline", return_value=PIPELINE)
    @patch("cache.cache_ura._fetch_all_transactions", return_value=TXNS)
    @patch("cache.cache_ura._get_token", return_value="tok")
    @patch("cache.cache_ura._save_cache")
    @patch("cache.cache_ura.is_ura_transactions_stale", return_value=True)
    @patch("cache.cache_ura._meta_timestamp", return_value=1000.0)
    def test_refresh_path_populates_memo(self, _ts, mock_stale, _save, _tok, _f1, _f2):
        data = cache_ura.get_ura_data()
        self.assertEqual(data, (TXNS, PIPELINE))
        # After refresh the memo holds the new data: a fresh-path call with the
        # same timestamp must not hit _load_cache at all.
        mock_stale.return_value = False
        with patch("cache.cache_ura._load_cache") as mock_load:
            again = cache_ura.get_ura_data()
        mock_load.assert_not_called()
        self.assertIs(again, data)


class TestRentalMemo(unittest.TestCase):

    def setUp(self):
        cache_rental._memo_ts = None
        cache_rental._memo_data = None

    @patch("cache.cache_rental.is_rental_stale", return_value=False)
    @patch("cache.cache_rental._load_cache", return_value=RENTALS)
    @patch("cache.cache_rental._meta_timestamp", return_value=1000.0)
    def test_second_call_hits_memo(self, _ts, mock_load, _stale):
        first = cache_rental.get_rental_data()
        second = cache_rental.get_rental_data()
        self.assertEqual(mock_load.call_count, 1)
        self.assertIs(first, second)

    @patch("cache.cache_rental.is_rental_stale", return_value=False)
    @patch("cache.cache_rental._load_cache", return_value=RENTALS)
    @patch("cache.cache_rental._meta_timestamp")
    def test_new_timestamp_invalidates_memo(self, mock_ts, mock_load, _stale):
        mock_ts.return_value = 1000.0
        cache_rental.get_rental_data()
        mock_ts.return_value = 2000.0
        cache_rental.get_rental_data()
        self.assertEqual(mock_load.call_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
