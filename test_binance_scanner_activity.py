import sys
import types
import unittest
from unittest import mock

if "dotenv" not in sys.modules:
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv_stub

import binance_scanner


class BinanceScannerActivityTests(unittest.TestCase):
    def setUp(self):
        self._old_running = binance_scanner._SCAN_STATE["running"]
        binance_scanner._SCAN_STATE["running"] = True

    def tearDown(self):
        binance_scanner._SCAN_STATE["running"] = self._old_running

    def test_summarize_recent_activity_marks_inactive_when_window_is_empty(self):
        now_ms = 1_700_000_000_000
        summary = binance_scanner._summarize_recent_activity([], now_ms=now_ms, active_days=7)

        self.assertFalse(summary["is_recently_active"])
        self.assertIsNone(summary["last_trade_time"])
        self.assertEqual(0, summary["recent_trade_count_24h"])
        self.assertEqual(0, summary["recent_trade_count_7d"])

    @mock.patch("binance_scanner.time.sleep", lambda *_args, **_kwargs: None)
    @mock.patch("binance_scanner.time.time", return_value=1_700_000_000)
    @mock.patch("binance_scraper.fetch_trader_info")
    @mock.patch("binance_scraper.fetch_operation_records_with_status")
    def test_score_with_details_skips_good_but_inactive_trader(
        self,
        mock_fetch_activity,
        mock_fetch_info,
        _mock_time,
    ):
        mock_fetch_info.return_value = {
            "copy_trade_days": 180,
            "total_trades": 300,
            "aum": 100_000,
            "follower_count": 500,
            "copier_pnl": 80_000,
            "win_rate": 68,
        }
        mock_fetch_activity.return_value = ([], None)

        scored = binance_scanner._score_with_details(
            [
                {
                    "portfolio_id": "inactive-1",
                    "nickname": "inactive",
                    "copier_pnl": 80_000,
                    "follower_count": 500,
                    "aum": 100_000,
                    "copy_days": 180,
                    "total_trades": 300,
                    "win_rate": 68,
                }
            ],
            filters={"active_days": 7},
        )

        self.assertEqual([], scored)

    @mock.patch("binance_scanner.time.sleep", lambda *_args, **_kwargs: None)
    @mock.patch("binance_scanner.time.time", return_value=1_700_000_000)
    @mock.patch("binance_scraper.fetch_trader_info")
    @mock.patch("binance_scraper.fetch_operation_records_with_status")
    def test_more_active_trader_ranks_higher_when_quality_is_similar(
        self,
        mock_fetch_activity,
        mock_fetch_info,
        _mock_time,
    ):
        mock_fetch_info.side_effect = [
            {
                "copy_trade_days": 180,
                "total_trades": 300,
                "aum": 100_000,
                "follower_count": 500,
                "copier_pnl": 80_000,
                "win_rate": 68,
            },
            {
                "copy_trade_days": 180,
                "total_trades": 300,
                "aum": 100_000,
                "follower_count": 500,
                "copier_pnl": 80_000,
                "win_rate": 68,
            },
        ]
        now_ms = 1_700_000_000_000
        very_active_records = [
            {"order_time": now_ms - 2 * 3600 * 1000},
            {"order_time": now_ms - 8 * 3600 * 1000},
            {"order_time": now_ms - 30 * 3600 * 1000},
            {"order_time": now_ms - 2 * 24 * 3600 * 1000},
        ]
        barely_active_records = [
            {"order_time": now_ms - 6 * 24 * 3600 * 1000},
        ]
        mock_fetch_activity.side_effect = [
            (very_active_records, None),
            (barely_active_records, None),
        ]

        scored = binance_scanner._score_with_details(
            [
                {
                    "portfolio_id": "active-1",
                    "nickname": "active",
                    "copier_pnl": 80_000,
                    "follower_count": 500,
                    "aum": 100_000,
                    "copy_days": 180,
                    "total_trades": 300,
                    "win_rate": 68,
                },
                {
                    "portfolio_id": "active-2",
                    "nickname": "less-active",
                    "copier_pnl": 80_000,
                    "follower_count": 500,
                    "aum": 100_000,
                    "copy_days": 180,
                    "total_trades": 300,
                    "win_rate": 68,
                },
            ],
            filters={"active_days": 7},
        )

        self.assertEqual(["active-1", "active-2"], [item["portfolio_id"] for item in scored])
        self.assertGreater(scored[0]["score"], scored[1]["score"])


if __name__ == "__main__":
    unittest.main()
