import os
import shutil
import sys
import time
import types
import unittest

if "dotenv" not in sys.modules:
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv_stub

for module_name in ("requests", "order_executor", "binance_scraper"):
    if module_name not in sys.modules:
        sys.modules[module_name] = types.ModuleType(module_name)

import config
import database as db
from copy_engine import _decide_take_profit_action, _estimate_position_pnl_roi


class CopyPositionSummaryTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = os.path.join(os.path.dirname(__file__), "_tmp")
        os.makedirs(self._tmpdir, exist_ok=True)
        self._db_file = os.path.join(self._tmpdir, f"tracker_{time.time_ns()}.db")
        self._old_db_path = config.DB_PATH
        config.DB_PATH = self._db_file
        db.init_db()

    def tearDown(self):
        config.DB_PATH = self._old_db_path
        if os.path.exists(self._db_file):
            os.remove(self._db_file)

    def _insert_order(self, **kwargs):
        payload = {
            "timestamp": kwargs.get("timestamp", 0),
            "trader_uid": kwargs.get("trader_uid", "trader-1"),
            "tracking_no": kwargs.get("tracking_no", "ord"),
            "my_order_id": kwargs.get("my_order_id", ""),
            "symbol": kwargs.get("symbol", "BTCUSDT"),
            "direction": kwargs.get("direction", "long"),
            "leverage": kwargs.get("leverage", 10),
            "margin_usdt": kwargs.get("margin_usdt", 0.0),
            "source_price": kwargs.get("source_price", 100.0),
            "exec_price": kwargs.get("exec_price", 100.0),
            "deviation_pct": kwargs.get("deviation_pct", 0.0),
            "action": kwargs.get("action", "open"),
            "status": kwargs.get("status", "filled"),
            "pnl": kwargs.get("pnl"),
            "notes": kwargs.get("notes", ""),
            "exec_qty": kwargs.get("exec_qty", 0.0),
            "platform": kwargs.get("platform", "bitget"),
        }
        db.insert_copy_order(payload)

    def test_active_summary_tracks_partial_close(self):
        self._insert_order(timestamp=1, tracking_no="o1", exec_qty=10.0, margin_usdt=100.0, exec_price=100.0)
        self._insert_order(timestamp=2, tracking_no="o2", exec_qty=5.0, margin_usdt=50.0, exec_price=110.0)
        self._insert_order(timestamp=3, tracking_no="c1", action="close", exec_qty=4.0, exec_price=115.0, pnl=12.0)

        items = db.get_active_copy_position_summaries("bitget")
        self.assertEqual(1, len(items))
        pos = items[0]
        self.assertAlmostEqual(11.0, pos["remaining_qty"], places=6)
        self.assertAlmostEqual(15.0, pos["cycle_open_qty"], places=6)
        self.assertAlmostEqual(110.0, pos["remaining_margin"], places=6)
        self.assertAlmostEqual((100.0 * 10.0 + 110.0 * 5.0) / 15.0, pos["avg_entry_price"], places=6)

    def test_active_summary_resets_after_cycle_is_closed(self):
        self._insert_order(timestamp=1, tracking_no="o1", exec_qty=10.0, margin_usdt=100.0, exec_price=100.0)
        self._insert_order(timestamp=2, tracking_no="c1", action="close", exec_qty=10.0, exec_price=101.0, pnl=5.0)
        self._insert_order(timestamp=3, tracking_no="o2", exec_qty=3.0, margin_usdt=30.0, exec_price=120.0)

        items = db.get_active_copy_position_summaries("bitget")
        self.assertEqual(1, len(items))
        pos = items[0]
        self.assertAlmostEqual(3.0, pos["remaining_qty"], places=6)
        self.assertAlmostEqual(3.0, pos["cycle_open_qty"], places=6)
        self.assertAlmostEqual(30.0, pos["remaining_margin"], places=6)
        self.assertAlmostEqual(120.0, pos["avg_entry_price"], places=6)


class TakeProfitDecisionTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "stop_loss_pct": 0.06,
            "tp1_roi_pct": 0.08,
            "tp1_close_pct": 0.30,
            "tp2_roi_pct": 0.15,
            "tp2_close_pct": 0.30,
            "tp3_roi_pct": 0.25,
            "tp3_close_pct": 0.40,
            "breakeven_buffer_pct": 0.005,
            "trail_callback_pct": 0.06,
        }

    def test_estimate_position_pnl_roi_for_long_and_short(self):
        long_metrics = _estimate_position_pnl_roi(100.0, 110.0, 2.0, 50.0, "long")
        short_metrics = _estimate_position_pnl_roi(110.0, 100.0, 2.0, 50.0, "short")
        self.assertAlmostEqual(20.0, long_metrics["pnl"], places=6)
        self.assertAlmostEqual(0.4, long_metrics["roi"], places=6)
        self.assertAlmostEqual(20.0, short_metrics["pnl"], places=6)
        self.assertAlmostEqual(0.4, short_metrics["roi"], places=6)

    def test_tp1_tp2_tp3_progression(self):
        tp1 = _decide_take_profit_action({"remaining_qty": 10.0, "cycle_open_qty": 10.0, "roi": 0.09}, {}, self.settings)
        self.assertEqual("System TP1", tp1["action"]["label"])
        self.assertAlmostEqual(3.0, tp1["action"]["qty"], places=6)
        self.assertEqual(1, tp1["action"]["next_state"]["stage"])
        self.assertAlmostEqual(0.005, tp1["action"]["next_state"]["locked_roi_pct"], places=6)

        tp2 = _decide_take_profit_action(
            {"remaining_qty": 7.0, "cycle_open_qty": 10.0, "roi": 0.16},
            tp1["action"]["next_state"],
            self.settings,
        )
        self.assertEqual("System TP2", tp2["action"]["label"])
        self.assertAlmostEqual(3.0, tp2["action"]["qty"], places=6)
        self.assertEqual(2, tp2["action"]["next_state"]["stage"])
        self.assertEqual(1, tp2["action"]["next_state"]["trail_active"])
        self.assertAlmostEqual(0.06, tp2["action"]["next_state"]["locked_roi_pct"], places=6)

        tp3 = _decide_take_profit_action(
            {"remaining_qty": 4.0, "cycle_open_qty": 10.0, "roi": 0.27},
            tp2["action"]["next_state"],
            self.settings,
        )
        self.assertEqual("System TP3", tp3["action"]["label"])
        self.assertEqual("close_all", tp3["action"]["kind"])

    def test_trail_exit_after_tp2_peak_pullback(self):
        decision = _decide_take_profit_action(
            {"remaining_qty": 4.0, "cycle_open_qty": 10.0, "roi": 0.13},
            {"stage": 2, "peak_roi": 0.20, "locked_roi_pct": 0.06, "trail_active": 1},
            self.settings,
        )
        self.assertEqual("System Trail Exit", decision["action"]["label"])
        self.assertEqual("close_all", decision["action"]["kind"])

    def test_stop_loss_has_priority_when_roi_breaks_floor(self):
        decision = _decide_take_profit_action(
            {"remaining_qty": 10.0, "cycle_open_qty": 10.0, "roi": -0.07},
            {},
            self.settings,
        )
        self.assertEqual("System Stop Loss", decision["action"]["label"])
        self.assertEqual("close_all", decision["action"]["kind"])


if __name__ == "__main__":
    unittest.main()
