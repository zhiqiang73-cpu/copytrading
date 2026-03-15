import json
import os
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
import copy_engine
import database as db


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class CopyEngineSyncOpenTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = os.path.join(os.path.dirname(__file__), "_tmp")
        os.makedirs(self._tmpdir, exist_ok=True)
        self._db_file = os.path.join(self._tmpdir, f"sync_open_{time.time_ns()}.db")
        self._old_db_path = config.DB_PATH
        config.DB_PATH = self._db_file
        db.init_db()

    def tearDown(self):
        config.DB_PATH = self._old_db_path
        if os.path.exists(self._db_file):
            os.remove(self._db_file)

    def _insert_source_event(
        self,
        *,
        trader_uid: str,
        source_order_id: str,
        symbol: str,
        action: str,
        direction: str,
        qty: float,
        price: float,
        leverage: int,
        order_time: int,
    ) -> None:
        db.upsert_source_trader_events([
            {
                "trader_uid": trader_uid,
                "source_order_id": source_order_id,
                "symbol": symbol,
                "action": action,
                "direction": direction,
                "qty": qty,
                "price": price,
                "leverage": leverage,
                "order_time": order_time,
                "raw_payload": {"source_order_id": source_order_id},
            }
        ])

    def test_source_position_summary_retains_open_snapshot_fields(self):
        self._insert_source_event(
            trader_uid="trader-1",
            source_order_id="src-open-1",
            symbol="RIVERUSDT",
            action="open_long",
            direction="long",
            qty=10.0,
            price=100.0,
            leverage=5,
            order_time=1000,
        )
        self._insert_source_event(
            trader_uid="trader-1",
            source_order_id="src-open-2",
            symbol="RIVERUSDT",
            action="open_long",
            direction="long",
            qty=5.0,
            price=110.0,
            leverage=5,
            order_time=2000,
        )
        self._insert_source_event(
            trader_uid="trader-1",
            source_order_id="src-close-1",
            symbol="RIVERUSDT",
            action="close_long",
            direction="long",
            qty=3.0,
            price=120.0,
            leverage=5,
            order_time=3000,
        )

        rows = db.get_source_position_summaries("trader-1")
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertAlmostEqual(12.0, row["remaining_qty"], places=6)
        self.assertAlmostEqual((10.0 * 100.0 + 5.0 * 110.0) / 15.0, row["avg_entry_price"], places=6)
        self.assertAlmostEqual(248.0, row["remaining_margin"], places=6)
        self.assertEqual(120.0, row["price"])
        self.assertEqual("src-close-1", row["last_source_order_id"])
        self.assertEqual(3000, row["last_event_time"])
        self.assertEqual(3000, row["last_close_time"])

    def test_loop_syncs_pending_source_open_position_once(self):
        self._insert_source_event(
            trader_uid="trader-1",
            source_order_id="src-open-1",
            symbol="ETHUSDT",
            action="open_long",
            direction="long",
            qty=0.5,
            price=2000.0,
            leverage=1,
            order_time=1000,
        )

        db.update_copy_settings_profile(
            "live",
            engine_enabled=1,
            api_key="live-ak",
            api_secret="live-sk",
            api_passphrase="live-pp",
            total_capital=100.0,
            follow_ratio_pct=0.1,
        )
        db.update_shared_copy_settings(
            binance_traders=json.dumps(
                {
                    "trader-1": {
                        "nickname": "sync-me",
                        "copy_enabled": True,
                        "sync_open_positions_pending": True,
                    }
                },
                ensure_ascii=False,
            )
        )

        engine = copy_engine.CopyEngine(profile="live")
        engine._last_bn_metadata_refresh = time.time()
        engine._manage_protective_exits = lambda *args, **kwargs: None
        engine._evaluate_open_guard = lambda platform, wallet_balance, settings: (True, "")
        open_calls: list[dict] = []

        with mock.patch.object(copy_engine.order_executor, "use_runtime", return_value=_Ctx()), \
             mock.patch.object(copy_engine.order_executor, "get_account_balance", return_value={"available": 100.0, "_posMode": "2"}), \
             mock.patch.object(copy_engine.binance_scraper, "fetch_latest_orders", return_value=[]), \
             mock.patch.object(engine, "_execute_open_for_platform", side_effect=lambda **kwargs: open_calls.append(kwargs)):
            engine._loop_binance_once()
            engine._loop_binance_once()

        self.assertEqual(1, len(open_calls))
        self.assertEqual("trader-1", open_calls[0]["pid"])
        self.assertEqual("ETHUSDT", open_calls[0]["symbol"])
        self.assertEqual("long", open_calls[0]["direction"])
        self.assertTrue(str(open_calls[0]["order_id"]).startswith("SYNC_"))

        settings = db.get_copy_settings_profile("live")
        traders = settings.get("binance_traders") or {}
        if isinstance(traders, str):
            traders = json.loads(traders)
        trader_row = traders["trader-1"]
        self.assertFalse(trader_row["sync_open_positions_pending"])
        self.assertEqual("submitted", trader_row["last_sync_open_status"])
        self.assertEqual(1, trader_row["last_sync_open_position_count"])
        self.assertEqual(1, trader_row["last_sync_open_attempt_count"])


if __name__ == "__main__":
    unittest.main()
