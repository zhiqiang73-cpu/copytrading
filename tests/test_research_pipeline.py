import os
import sys
import time
import types
import unittest

if "dotenv" not in sys.modules:
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv_stub

import binance_scraper
import config
import database as db


class ResearchPipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = os.path.join(os.path.dirname(__file__), "_tmp")
        os.makedirs(self._tmpdir, exist_ok=True)
        self._db_file = os.path.join(self._tmpdir, f"research_{time.time_ns()}.db")
        self._old_db_path = config.DB_PATH
        config.DB_PATH = self._db_file
        db.init_db()

    def tearDown(self):
        config.DB_PATH = self._old_db_path
        if os.path.exists(self._db_file):
            os.remove(self._db_file)

    def test_filter_records_after_cursor_keeps_same_timestamp_new_order_id(self):
        records = [
            {"order_id": "a", "order_time": 1000},
            {"order_id": "b", "order_time": 1000},
            {"order_id": "c", "order_time": 1001},
        ]

        filtered = binance_scraper.filter_records_after_cursor(records, since_ms=1000, since_order_id="a")

        self.assertEqual(["b", "c"], [item["order_id"] for item in filtered])

    def test_source_event_upsert_is_idempotent_and_rebuilds_cycle(self):
        events = [
            {
                "trader_uid": "trader-1",
                "source_order_id": "evt-open",
                "symbol": "BTCUSDT",
                "action": "open_long",
                "direction": "long",
                "qty": 1.0,
                "price": 100.0,
                "leverage": 5,
                "order_time": 1000,
                "raw_payload": {"id": 1},
            },
            {
                "trader_uid": "trader-1",
                "source_order_id": "evt-close",
                "symbol": "BTCUSDT",
                "action": "close_long",
                "direction": "long",
                "qty": 1.0,
                "price": 105.0,
                "leverage": 5,
                "order_time": 2000,
                "raw_payload": {"id": 2},
            },
        ]

        db.upsert_source_trader_events(events, source_kind="history")
        db.upsert_source_trader_events(events, source_kind="history")
        rebuilt = db.rebuild_trader_position_cycles("trader-1")

        stored_events = db.get_source_trader_events("trader-1")
        cycles = db.get_trader_position_cycles("trader-1")
        self.assertEqual(2, len(stored_events))
        self.assertEqual(1, rebuilt)
        self.assertEqual(1, len(cycles))
        self.assertEqual("normal_close", cycles[0]["close_reason"])
        self.assertAlmostEqual(5.0, cycles[0]["realized_pnl"], places=6)

    def test_rebuild_cycles_marks_reverse_transition(self):
        events = [
            {
                "trader_uid": "trader-1",
                "source_order_id": "evt-open-long",
                "symbol": "ETHUSDT",
                "action": "open_long",
                "direction": "long",
                "qty": 1.0,
                "price": 200.0,
                "leverage": 4,
                "order_time": 1000,
                "raw_payload": {"id": 1},
            },
            {
                "trader_uid": "trader-1",
                "source_order_id": "evt-open-short",
                "symbol": "ETHUSDT",
                "action": "open_short",
                "direction": "short",
                "qty": 1.0,
                "price": 190.0,
                "leverage": 4,
                "order_time": 2000,
                "raw_payload": {"id": 2},
            },
        ]

        db.upsert_source_trader_events(events)
        db.rebuild_trader_position_cycles("trader-1")

        cycles = db.get_trader_position_cycles("trader-1")
        reasons = {(row["symbol"], row["direction"], row["close_reason"]) for row in cycles}
        self.assertIn(("ETHUSDT", "long", "reverse_transition"), reasons)
        self.assertIn(("ETHUSDT", "short", "still_open"), reasons)

    def test_refresh_execution_daily_and_research_scores(self):
        day_start_ms = int(time.mktime(time.strptime("2026-03-11", "%Y-%m-%d"))) * 1000
        db.insert_copy_order({
            "timestamp": day_start_ms + 1000,
            "trader_uid": "trader-1",
            "tracking_no": "open-1",
            "my_order_id": "",
            "symbol": "BTCUSDT",
            "direction": "long",
            "leverage": 5,
            "margin_usdt": 10.0,
            "source_price": 100.0,
            "exec_price": 100.0,
            "deviation_pct": 0.0,
            "action": "open",
            "status": "filled",
            "pnl": None,
            "notes": "",
            "exec_qty": 1.0,
            "platform": "live_binance",
        })
        db.insert_copy_order({
            "timestamp": day_start_ms + 2000,
            "trader_uid": "trader-1",
            "tracking_no": "open-2",
            "my_order_id": "",
            "symbol": "DOGEUSDT",
            "direction": "long",
            "leverage": 5,
            "margin_usdt": 0.0,
            "source_price": 0.2,
            "exec_price": 0.0,
            "deviation_pct": 0.0,
            "action": "open",
            "status": "skipped",
            "pnl": None,
            "notes": "[skip] invalid symbol",
            "exec_qty": 0.0,
            "platform": "live_binance",
        })
        db.insert_copy_order({
            "timestamp": day_start_ms + 3000,
            "trader_uid": "trader-1",
            "tracking_no": "close-1",
            "my_order_id": "",
            "symbol": "BTCUSDT",
            "direction": "long",
            "leverage": 0,
            "margin_usdt": 0.0,
            "source_price": 101.0,
            "exec_price": 101.0,
            "deviation_pct": 0.0,
            "action": "close",
            "status": "filled",
            "pnl": 1.0,
            "notes": "[Live Binance Signal] Close",
            "exec_qty": 1.0,
            "platform": "live_binance",
        })
        db.upsert_source_trader_events([
            {
                "trader_uid": "trader-1",
                "source_order_id": "evt-open",
                "symbol": "BTCUSDT",
                "action": "open_long",
                "direction": "long",
                "qty": 1.0,
                "price": 100.0,
                "leverage": 5,
                "order_time": day_start_ms + 1000,
                "raw_payload": {"id": 1},
            },
            {
                "trader_uid": "trader-1",
                "source_order_id": "evt-close",
                "symbol": "BTCUSDT",
                "action": "close_long",
                "direction": "long",
                "qty": 1.0,
                "price": 101.0,
                "leverage": 5,
                "order_time": day_start_ms + 3000,
                "raw_payload": {"id": 2},
            },
        ])
        db.rebuild_trader_position_cycles("trader-1")

        daily_count = db.refresh_trader_execution_daily(["trader-1"])
        score_count = db.refresh_trader_research_scores(["trader-1"])

        daily_rows = db.get_trader_execution_daily("trader-1")
        score_rows = db.get_trader_research_scores("trader-1")
        self.assertEqual(1, daily_count)
        self.assertEqual(1, score_count)
        self.assertEqual(1, len(daily_rows))
        self.assertAlmostEqual(0.5, daily_rows[0]["open_fill_rate"], places=6)
        self.assertAlmostEqual(1.0, daily_rows[0]["close_completion_rate"], places=6)
        self.assertAlmostEqual(0.5, daily_rows[0]["invalid_symbol_rate"], places=6)
        self.assertEqual("live_binance", score_rows[0]["preferred_platform"])
        self.assertGreater(score_rows[0]["close_reliability_score"], 0.0)


if __name__ == "__main__":
    unittest.main()
