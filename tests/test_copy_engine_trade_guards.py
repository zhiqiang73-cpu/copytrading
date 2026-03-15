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

    def test_reverses_opposite_open_for_same_trader_symbol(self):
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

        with mock.patch.object(engine, "_execute_open_for_platform") as open_mock,              mock.patch.object(engine, "_execute_close_for_platform", return_value=True) as close_mock:
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

        close_mock.assert_called_once()
        self.assertEqual("short", close_mock.call_args.kwargs["direction"])
        self.assertEqual("reverse_reconcile_close", close_mock.call_args.kwargs["close_reason"])
        open_mock.assert_called_once()
        state = db.get_copy_position_state("live_bitget", "trader-1", "XRPUSDT", "short")
        self.assertEqual("reverse_reconcile_close", state["last_system_action"])

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

        with mock.patch.object(copy_engine, "_price_ok", return_value=(True, 70010.0, 0.0)),              mock.patch.object(copy_engine.order_executor, "close_partial_position", side_effect=ValueError("HTTP 400 | code=22002 | no position")),              mock.patch.object(engine, "_get_exchange_position_qty", return_value=0.0):
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

    def test_reconciles_bitget_no_position_even_if_lookup_is_stale(self):
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

        with mock.patch.object(copy_engine, "_price_ok", return_value=(True, 70010.0, 0.0)),              mock.patch.object(copy_engine.order_executor, "close_partial_position", side_effect=ValueError("HTTP 400 | code=22002 | no position to close")),              mock.patch.object(engine, "_get_exchange_position_qty", return_value=0.0001):
            ok = engine._execute_close_for_platform(
                platform="live_bitget",
                api_creds=("ak", "sk", "pp"),
                pid="trader-1",
                order_id="close-short-stale",
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
        self.assertIn("lookup reported qty", rows[0]["notes"])

    def test_close_path_continues_when_price_mismatch(self):
        engine = copy_engine.CopyEngine(profile="live")
        self._insert_order(
            trader_uid="trader-1",
            tracking_no="open-long",
            symbol="ETHUSDT",
            direction="long",
            platform="live_bitget",
            exec_qty=0.01,
            exec_price=2500.0,
            source_price=2500.0,
        )

        with mock.patch.object(copy_engine, "_price_ok", return_value=(False, 2550.0, 0.02)),              mock.patch.object(copy_engine.order_executor, "close_partial_position", return_value={}):
            ok = engine._execute_close_for_platform(
                platform="live_bitget",
                api_creds=("ak", "sk", "pp"),
                pid="trader-1",
                order_id="close-long-drift",
                symbol="ETHUSDT",
                direction="long",
                price=2500.0,
                order_pnl=0.0,
                tol=0.005,
            )

        self.assertTrue(ok)
        rows = db.get_copy_orders(limit=5, platforms=["live_bitget"])
        self.assertIn("price drift", rows[0]["notes"])

    def test_source_reconcile_closes_stale_local_position_after_source_flat(self):
        engine = copy_engine.CopyEngine(profile="live")
        engine._reconcile_wait_ms = 0
        engine._reconcile_wait_polls = 1
        self._insert_order(
            trader_uid="trader-1",
            tracking_no="open-short",
            symbol="PIXELUSDT",
            direction="short",
            platform="live_bitget",
            exec_qty=10.0,
            exec_price=0.11,
            source_price=0.11,
        )
        db.upsert_source_trader_events([
            {
                "trader_uid": "trader-1",
                "source_order_id": "src-open",
                "symbol": "PIXELUSDT",
                "action": "open_short",
                "direction": "short",
                "qty": 10.0,
                "price": 0.11,
                "leverage": 5,
                "order_time": 1000,
                "raw_payload": {"k": 1},
            },
            {
                "trader_uid": "trader-1",
                "source_order_id": "src-close",
                "symbol": "PIXELUSDT",
                "action": "close_short",
                "direction": "short",
                "qty": 10.0,
                "price": 0.10,
                "leverage": 5,
                "order_time": 2000,
                "raw_payload": {"k": 2},
            },
        ])

        with mock.patch.object(engine, "_execute_close_for_platform", return_value=True) as close_mock:
            engine._reconcile_source_positions(["trader-1"], ("ak", "sk", "pp"), None, 0.05, 0.05)
            engine._reconcile_source_positions(["trader-1"], ("ak", "sk", "pp"), None, 0.05, 0.05)

        close_mock.assert_called_once()
        self.assertEqual("reconcile_close", close_mock.call_args.kwargs["close_reason"])
        self.assertTrue(close_mock.call_args.kwargs["force_close"])
        state = db.get_copy_position_state("live_bitget", "trader-1", "PIXELUSDT", "short")
        self.assertEqual("reconcile_close", state["last_system_action"])

    def test_source_reconcile_ignores_positions_when_source_still_open(self):
        engine = copy_engine.CopyEngine(profile="live")
        engine._reconcile_wait_ms = 0
        engine._reconcile_wait_polls = 1
        self._insert_order(
            trader_uid="trader-1",
            tracking_no="open-short",
            symbol="PIXELUSDT",
            direction="short",
            platform="live_bitget",
            exec_qty=10.0,
            exec_price=0.11,
            source_price=0.11,
        )
        db.upsert_source_trader_events([
            {
                "trader_uid": "trader-1",
                "source_order_id": "src-open",
                "symbol": "PIXELUSDT",
                "action": "open_short",
                "direction": "short",
                "qty": 10.0,
                "price": 0.11,
                "leverage": 5,
                "order_time": 1000,
                "raw_payload": {"k": 1},
            }
        ])

        with mock.patch.object(engine, "_execute_close_for_platform", return_value=True) as close_mock:
            engine._reconcile_source_positions(["trader-1"], ("ak", "sk", "pp"), None, 0.05, 0.05)
            engine._reconcile_source_positions(["trader-1"], ("ak", "sk", "pp"), None, 0.05, 0.05)

        close_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
