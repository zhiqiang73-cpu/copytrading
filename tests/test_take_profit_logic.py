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

for module_name in ("requests", "order_executor", "binance_scraper", "binance_executor"):
    if module_name not in sys.modules:
        sys.modules[module_name] = types.ModuleType(module_name)

import config
import database as db
from copy_engine import _decide_take_profit_action, _estimate_position_pnl_roi, _pick_maker_limit_price


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

    def test_live_profile_settings_inherit_non_secret_defaults_from_sim_profile(self):
        db.update_copy_settings(
            total_capital=321.0,
            follow_ratio_pct=0.012,
            binance_traders='{"p1": {"nickname": "Trader 1", "copy_enabled": true}}',
        )

        settings = db.get_copy_settings_profile("live")
        self.assertAlmostEqual(321.0, settings["total_capital"], places=6)
        self.assertAlmostEqual(0.012, settings["follow_ratio_pct"], places=6)
        self.assertIn("p1", settings["binance_traders"])

    def test_live_profile_trader_selection_is_shared_with_sim_profile(self):
        db.update_copy_settings(
            enabled_traders='["sim-u"]',
            binance_traders='{"sim-p1": {"nickname": "Sim Trader", "copy_enabled": false}}',
        )
        db.update_copy_settings_profile(
            "live",
            enabled_traders='["live-u"]',
            binance_traders='{"live-p1": {"nickname": "Live Trader", "copy_enabled": true}}',
        )

        settings = db.get_copy_settings_profile("live")
        self.assertEqual('["sim-u"]', settings["enabled_traders"])
        self.assertIn("sim-p1", settings["binance_traders"])
        self.assertNotIn("live-p1", settings["binance_traders"])

    def test_parse_copy_settings_payload_keeps_explicit_empty_binance_traders(self):
        import web

        normalized = web._parse_copy_settings_payload(
            {
                "binance_traders": {},
                "enabled_traders": [],
            },
            {
                "binance_traders": {"old-p1": {"nickname": "Old Trader", "copy_enabled": True}},
                "enabled_traders": ["old-u"],
            },
        )

        self.assertEqual("{}", normalized["binance_traders"])

    def test_get_copy_orders_can_filter_by_profile_platforms(self):
        self._insert_order(timestamp=1, tracking_no="sim-open", platform="bitget", exec_qty=1.0)
        self._insert_order(timestamp=2, tracking_no="live-open", platform="live_bitget", exec_qty=2.0)

        live_rows = db.get_copy_orders(limit=10, offset=0, platforms=["live_bitget"])
        sim_rows = db.get_copy_orders(limit=10, offset=0, platforms=["bitget"])

        self.assertEqual(["live-open"], [row["tracking_no"] for row in live_rows])
        self.assertEqual(["sim-open"], [row["tracking_no"] for row in sim_rows])

    def test_live_loop_uses_live_runtime_for_balance_sync(self):
        order_executor_stub = sys.modules["order_executor"]
        binance_executor_stub = sys.modules["binance_executor"]
        binance_scraper_stub = sys.modules["binance_scraper"]
        calls = []

        class _Ctx:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                calls.append(self.payload)
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        order_executor_stub.use_runtime = lambda simulated=None: _Ctx(("bitget_runtime", simulated))
        order_executor_stub.get_account_balance = lambda *args, **kwargs: {"available": 10, "_posMode": "2"}

        binance_executor_stub.use_runtime = lambda base_url=None: _Ctx(("binance_runtime", base_url))
        binance_executor_stub.get_account_balance = lambda *args, **kwargs: {"availableBalance": 20, "balance": 20}

        binance_scraper_stub.fetch_latest_orders = lambda *args, **kwargs: []

        db.update_copy_settings_profile(
            "live",
            engine_enabled=1,
            api_key="live-ak",
            api_secret="live-sk",
            api_passphrase="live-pp",
            binance_api_key="live-bn-ak",
            binance_api_secret="live-bn-sk",
        )
        db.update_shared_copy_settings(binance_traders='{"pid-1": {"copy_enabled": true}}')

        from copy_engine import CopyEngine

        engine = CopyEngine("live")
        engine._last_bn_metadata_refresh = time.time()
        engine._loop_binance_once()

        self.assertIn(("bitget_runtime", False), calls)
        self.assertIn(("binance_runtime", config.BINANCE_LIVE_BASE_URL), calls)


    def test_live_loop_processes_open_signal_with_settings_payload(self):
        order_executor_stub = sys.modules["order_executor"]
        binance_executor_stub = sys.modules["binance_executor"]
        binance_scraper_stub = sys.modules["binance_scraper"]
        seen_settings = []

        class _Ctx:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        order_executor_stub.use_runtime = lambda simulated=None: _Ctx()
        order_executor_stub.get_account_balance = lambda *args, **kwargs: {"available": 10, "_posMode": "2"}

        binance_executor_stub.use_runtime = lambda base_url=None: _Ctx()
        binance_executor_stub.get_account_balance = lambda *args, **kwargs: {"availableBalance": 20, "balance": 20}

        binance_scraper_stub.fetch_latest_orders = lambda *args, **kwargs: [{
            "order_id": "ord-1",
            "symbol": "ETHUSDT",
            "action": "open_long",
            "direction": "long",
            "qty": 0.5,
            "price": 2000.0,
            "order_time": 1,
            "pnl": 0.0,
            "leverage": 1,
        }]

        db.update_copy_settings_profile(
            "live",
            engine_enabled=1,
            api_key="live-ak",
            api_secret="live-sk",
            api_passphrase="live-pp",
            binance_api_key="live-bn-ak",
            binance_api_secret="live-bn-sk",
            total_capital=100.0,
            binance_total_capital=100.0,
            follow_ratio_pct=0.1,
            binance_follow_ratio_pct=0.1,
        )
        db.update_shared_copy_settings(binance_traders='{"pid-1": {"copy_enabled": true}}')

        from copy_engine import CopyEngine

        engine = CopyEngine("live")
        engine._last_bn_metadata_refresh = time.time()
        engine._evaluate_open_guard = lambda platform, wallet_balance, settings: (True, "")
        engine._execute_open_for_platform = lambda **kwargs: seen_settings.append(kwargs["settings"])
        engine._loop_binance_once()

        self.assertEqual(2, len(seen_settings))
        self.assertTrue(all(s.get("engine_enabled") == 1 for s in seen_settings))


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


class MakerEntryDecisionTests(unittest.TestCase):
    def test_long_keeps_bid_when_spread_is_one_tick(self):
        price = _pick_maker_limit_price("long", 100.0, 100.1, 100.05, 0.1, 1)
        self.assertAlmostEqual(100.0, price, places=6)

    def test_long_can_improve_bid_inside_wider_spread(self):
        price = _pick_maker_limit_price("long", 100.0, 100.3, 100.15, 0.1, 1)
        self.assertAlmostEqual(100.1, price, places=6)

    def test_short_can_improve_ask_inside_wider_spread(self):
        price = _pick_maker_limit_price("short", 100.0, 100.3, 100.15, 0.1, 1)
        self.assertAlmostEqual(100.2, price, places=6)

    def test_price_falls_back_to_small_discount_without_quotes(self):
        price = _pick_maker_limit_price("long", 0.0, 0.0, 100.0, 0.0, 1)
        self.assertAlmostEqual(99.95, price, places=6)



class BinanceWalletMetricTests(unittest.TestCase):
    def test_extract_binance_live_wallet_metrics_falls_back_to_positions_unrealized(self):
        import web

        wallet_balance, available_balance, unrealized_pnl, base_wallet_balance = web._extract_binance_live_wallet_metrics(
            {
                "balance": "226.42",
                "availableBalance": "120.00",
            },
            [
                {"symbol": "ETHUSDT", "unRealizedProfit": "-1.5256"},
            ],
        )

        self.assertAlmostEqual(224.8944, wallet_balance, places=4)
        self.assertAlmostEqual(120.0, available_balance, places=6)
        self.assertAlmostEqual(-1.5256, unrealized_pnl, places=4)
        self.assertAlmostEqual(226.42, base_wallet_balance, places=6)
if __name__ == "__main__":
    unittest.main()
