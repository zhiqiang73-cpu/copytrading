import os
import shutil
import sys
import time
import types
import unittest
from unittest import mock

if "dotenv" not in sys.modules:
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv_stub

import config
import database as db
import copy_engine


class CopyEngineTradeGuardTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = os.path.join(os.path.dirname(__file__), "_tmp")
        os.makedirs(self._tmpdir, exist_ok=True)
        self._db_file = os.path.join(self._tmpdir, f"copy_engine_{time.time_ns()}.db")
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
            "leverage": kwargs.get("leverage", 1),
            "margin_usdt": kwargs.get("margin_usdt", 7.0),
            "source_price": kwargs.get("source_price", 100.0),
            "exec_price": kwargs.get("exec_price", 100.0),
            "deviation_pct": kwargs.get("deviation_pct", 0.0),
            "action": kwargs.get("action", "open"),
            "status": kwargs.get("status", "filled"),
            "pnl": kwargs.get("pnl"),
            "notes": kwargs.get("notes", ""),
            "exec_qty": kwargs.get("exec_qty", 1.0),
            "platform": kwargs.get("platform", "live_bitget"),
        }
        db.insert_copy_order(payload)

    def test_skips_opposite_open_for_same_trader_symbol(self):
        engine = copy_engine.CopyEngine(profile="live")
        self._insert_order(
            trader_uid="trader-1",
            tracking_no="open-short",
            symbol="XRPUSDT",
            direction="short",
            platform="live_bitget",
            exec_qty=5.0,
            exec_price=1.4,
            source_price=1.4,
        )

        order = {
            "order_id": "open-long",
            "symbol": "XRPUSDT",
            "action": "open_long",
            "direction": "long",
            "price": 1.41,
            "qty": 10.0,
            "leverage": 1,
            "order_time": 2,
        }

        with mock.patch.object(engine, "_execute_open_for_platform") as open_mock:
            engine._process_binance_order(
                settings={},
                bg_creds={"ak": "ak", "sk": "sk", "pp": "pp"},
                bn_creds=None,
                pid="trader-1",
                order=order,
                bg_fallback_margin=10.0,
                bg_tol=0.05,
                bg_follow_ratio=0.4,
                bg_available_usdt=100.0,
                bg_allow_open=True,
                bg_guard_note="",
                bn_fallback_margin=0.0,
                bn_tol=0.05,
                bn_follow_ratio=0.0,
                bn_available_usdt=0.0,
                bn_allow_open=False,
                bn_guard_note="",
            )

        open_mock.assert_not_called()
        rows = db.get_copy_orders(limit=5, platforms=["live_bitget"])
        self.assertEqual("skipped", rows[0]["status"])
        self.assertIn("hedge disabled", rows[0]["notes"])

    def test_reconciles_close_when_exchange_already_flat(self):
        engine = copy_engine.CopyEngine(profile="live")
        self._insert_order(
            trader_uid="trader-1",
            tracking_no="open-short",
            symbol="BTCUSDT",
            direction="short",
            platform="live_bitget",
            exec_qty=0.0001,
            exec_price=70000.0,
            source_price=70000.0,
        )

        with mock.patch.object(copy_engine, "_price_ok", return_value=(True, 70010.0, 0.0)),              mock.patch.object(copy_engine.order_executor, "close_partial_position", side_effect=ValueError("HTTP 400 | code=22002 | ??????")),              mock.patch.object(engine, "_get_exchange_position_qty", return_value=0.0):
            ok = engine._execute_close_for_platform(
                platform="live_bitget",
                api_creds=("ak", "sk", "pp"),
                pid="trader-1",
                order_id="close-short",
                symbol="BTCUSDT",
                direction="short",
                price=70079.1,
                order_pnl=0.0,
                tol=0.05,
            )

        self.assertTrue(ok)
        rows = db.get_copy_orders(limit=5, platforms=["live_bitget"])
        self.assertEqual("filled", rows[0]["status"])
        self.assertIn("exchange already flat", rows[0]["notes"])


if __name__ == "__main__":
    unittest.main()
