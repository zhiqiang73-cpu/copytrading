import os
import sys
import time
import unittest
from unittest import mock

sys.modules.pop("binance_executor", None)
sys.modules.pop("requests", None)
import requests  # noqa: F401
import binance_executor
import config
import database as db


class LivePlatformBaselineRepairTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = os.path.join(os.path.dirname(__file__), "_tmp")
        os.makedirs(self._tmpdir, exist_ok=True)
        self._db_file = os.path.join(self._tmpdir, f"live_runtime_{time.time_ns()}.db")
        self._old_db_path = config.DB_PATH
        config.DB_PATH = self._db_file
        db.init_db()

    def tearDown(self):
        config.DB_PATH = self._old_db_path
        if os.path.exists(self._db_file):
            os.remove(self._db_file)

    def _insert_filled_open(self, platform: str, day: str) -> None:
        day_start_ms = int(time.mktime(time.strptime(day, "%Y-%m-%d"))) * 1000
        db.insert_copy_order({
            "timestamp": day_start_ms + 1000,
            "trader_uid": "trader-1",
            "tracking_no": "ord-1",
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
            "exec_qty": 0.1,
            "platform": platform,
        })

    def test_resets_polluted_live_baseline_without_filled_orders(self):
        day = "2026-03-10"
        db.upsert_platform_daily_equity("live_bitget", day, 9696.78840696406)

        repaired = db.upsert_platform_daily_equity("live_bitget", day, 39.67977056)

        self.assertTrue(repaired["baseline_reset"])
        self.assertAlmostEqual(39.67977056, repaired["start_equity"], places=6)
        self.assertAlmostEqual(0.0, repaired["day_pnl"], places=6)

    def test_keeps_live_baseline_when_filled_open_exists(self):
        day = "2026-03-10"
        db.upsert_platform_daily_equity("live_bitget", day, 9696.78840696406)
        self._insert_filled_open("live_bitget", day)

        repaired = db.upsert_platform_daily_equity("live_bitget", day, 39.67977056)

        self.assertFalse(repaired["baseline_reset"])
        self.assertAlmostEqual(9696.78840696406, repaired["start_equity"], places=6)


class BinancePmFallbackTests(unittest.TestCase):
    def setUp(self):
        binance_executor._API_MODE_BY_KEY.clear()
        binance_executor._POSITION_MODE_BY_KEY.clear()

    def tearDown(self):
        binance_executor._API_MODE_BY_KEY.clear()
        binance_executor._POSITION_MODE_BY_KEY.clear()

    def test_set_position_mode_falls_back_to_papi_um_endpoint(self):
        calls = []

        def fake_request(api_key, api_secret, method, endpoint, params=None, base_url=None, max_retries=3):
            calls.append((method, endpoint, base_url))
            if endpoint == "/fapi/v1/positionSide/dual":
                raise ValueError("HTTP 401 | code=-2015 | Invalid API-key, IP, or permissions for action")
            if endpoint == "/papi/v1/um/positionSide/dual":
                return {"msg": "success"}
            raise AssertionError(endpoint)

        with mock.patch.object(binance_executor, "_request", side_effect=fake_request), \
             mock.patch.object(binance_executor, "_resolve_pm_base_candidates", return_value=["https://papi.binance.com"]):
            result = binance_executor.set_position_mode("ak", "sk", dual_side=True)

        self.assertEqual("success", result["msg"])
        self.assertEqual([
            ("POST", "/fapi/v1/positionSide/dual", None),
            ("POST", "/papi/v1/um/positionSide/dual", "https://papi.binance.com"),
        ], calls)

    def test_place_market_order_falls_back_to_papi_order_endpoint(self):
        calls = []

        def fake_request(api_key, api_secret, method, endpoint, params=None, base_url=None, max_retries=3):
            calls.append((method, endpoint, base_url, dict(params or {})))
            if endpoint == "/fapi/v1/order":
                raise ValueError("HTTP 401 | code=-2015 | Invalid API-key, IP, or permissions for action")
            if endpoint == "/papi/v1/um/order":
                return {"orderId": 123456}
            raise AssertionError(endpoint)

        with mock.patch.object(binance_executor, "_request", side_effect=fake_request), \
             mock.patch.object(binance_executor, "_resolve_pm_base_candidates", return_value=["https://papi.binance.com"]), \
             mock.patch.object(binance_executor, "get_position_mode", return_value={"mode": "hedge", "dualSidePosition": True}), \
             mock.patch.object(binance_executor, "set_symbol_leverage", return_value={}), \
             mock.patch.object(binance_executor, "set_margin_type", return_value={}), \
             mock.patch.object(binance_executor, "get_ticker_price", return_value=100.0), \
             mock.patch.object(binance_executor, "get_symbol_filters", return_value={"minQty": 0.001, "stepSize": 0.001, "tickSize": 0.1}):
            result = binance_executor.place_market_order(
                "ak", "sk", "BTCUSDT", "long", 5, "isolated", 50.0, current_price=100.0
            )

        self.assertEqual(123456, result["orderId"])
        self.assertEqual("2.500", result["_calculated_size"])
        self.assertEqual("/fapi/v1/order", calls[0][1])
        self.assertEqual("/papi/v1/um/order", calls[1][1])
        self.assertEqual("https://papi.binance.com", calls[1][2])


class BinancePositionModeOrderTests(unittest.TestCase):
    def setUp(self):
        binance_executor._API_MODE_BY_KEY.clear()
        binance_executor._POSITION_MODE_BY_KEY.clear()

    def tearDown(self):
        binance_executor._API_MODE_BY_KEY.clear()
        binance_executor._POSITION_MODE_BY_KEY.clear()

    def test_get_position_mode_normalizes_dual_side_response(self):
        with mock.patch.object(binance_executor, "_request_with_pm_fallback", return_value={"dualSidePosition": "false"}):
            result = binance_executor.get_position_mode("ak", "sk", force_refresh=True)

        self.assertEqual("oneway", result["mode"])
        self.assertFalse(result["dualSidePosition"])
        self.assertEqual("oneway", binance_executor._get_cached_position_mode("ak"))

    def test_place_market_order_omits_position_side_in_oneway_mode(self):
        calls = []

        def fake_request_with_pm_fallback(api_key, api_secret, method, endpoint, params=None, pm_endpoints=(), max_retries=3):
            calls.append((method, endpoint, dict(params or {}), tuple(pm_endpoints), max_retries))
            return {"orderId": 123}

        with mock.patch.object(binance_executor, "_request_with_pm_fallback", side_effect=fake_request_with_pm_fallback),              mock.patch.object(binance_executor, "get_position_mode", return_value={"mode": "oneway", "dualSidePosition": False}),              mock.patch.object(binance_executor, "set_symbol_leverage", return_value={}),              mock.patch.object(binance_executor, "set_margin_type", return_value={}),              mock.patch.object(binance_executor, "get_symbol_filters", return_value={"minQty": 0.001, "stepSize": 0.001, "tickSize": 0.1}):
            result = binance_executor.place_market_order(
                "ak", "sk", "BTCUSDT", "long", 5, "isolated", 50.0, current_price=100.0
            )

        self.assertEqual(123, result["orderId"])
        params = calls[0][2]
        self.assertEqual("BUY", params["side"])
        self.assertNotIn("positionSide", params)
        self.assertNotIn("reduceOnly", params)

    def test_close_partial_position_uses_reduce_only_in_oneway_mode(self):
        calls = []

        def fake_request_with_pm_fallback(api_key, api_secret, method, endpoint, params=None, pm_endpoints=(), max_retries=3):
            calls.append(dict(params or {}))
            return {"orderId": 456}

        with mock.patch.object(binance_executor, "_request_with_pm_fallback", side_effect=fake_request_with_pm_fallback),              mock.patch.object(binance_executor, "get_position_mode", return_value={"mode": "oneway", "dualSidePosition": False}):
            result = binance_executor.close_partial_position("ak", "sk", "BTCUSDT", "long", "1.250")

        self.assertEqual(456, result["orderId"])
        params = calls[0]
        self.assertEqual("SELL", params["side"])
        self.assertEqual("true", params["reduceOnly"])
        self.assertNotIn("positionSide", params)

    def test_place_market_order_refreshes_mode_after_4061(self):
        calls = []

        def fake_request_with_pm_fallback(api_key, api_secret, method, endpoint, params=None, pm_endpoints=(), max_retries=3):
            calls.append((dict(params or {}), max_retries))
            if len(calls) == 1:
                raise ValueError("HTTP 400 | code=-4061 | Order's position side does not match user's setting")
            return {"orderId": 789}

        with mock.patch.object(binance_executor, "_request_with_pm_fallback", side_effect=fake_request_with_pm_fallback),              mock.patch.object(binance_executor, "get_position_mode", side_effect=[
                 {"mode": "hedge", "dualSidePosition": True},
                 {"mode": "oneway", "dualSidePosition": False},
             ]),              mock.patch.object(binance_executor, "set_symbol_leverage", return_value={}),              mock.patch.object(binance_executor, "set_margin_type", return_value={}),              mock.patch.object(binance_executor, "get_symbol_filters", return_value={"minQty": 0.001, "stepSize": 0.001, "tickSize": 0.1}):
            result = binance_executor.place_market_order(
                "ak", "sk", "BTCUSDT", "long", 5, "isolated", 50.0, current_price=100.0
            )

        self.assertEqual(789, result["orderId"])
        first_payload, first_retries = calls[0]
        second_payload, second_retries = calls[1]
        self.assertEqual("LONG", first_payload["positionSide"])
        self.assertNotIn("positionSide", second_payload)
        self.assertEqual(1, second_retries)


class BinancePmPreferenceTests(unittest.TestCase):
    def setUp(self):
        binance_executor._API_MODE_BY_KEY.clear()
        binance_executor._POSITION_MODE_BY_KEY.clear()

    def tearDown(self):
        binance_executor._API_MODE_BY_KEY.clear()
        binance_executor._POSITION_MODE_BY_KEY.clear()

    def test_account_balance_marks_pm_mode_when_papi_account_succeeds(self):
        calls = []

        def fake_request(api_key, api_secret, method, endpoint, params=None, base_url=None, max_retries=3):
            calls.append((endpoint, base_url))
            if endpoint == "/fapi/v2/account":
                raise ValueError("HTTP 401 | code=-2015 | Invalid API-key, IP, or permissions for action")
            if endpoint == "/fapi/v2/balance":
                raise ValueError("HTTP 401 | code=-2015 | Invalid API-key, IP, or permissions for action")
            if endpoint == "/papi/v1/account":
                return {"asset": "USDT", "balance": "12.34", "availableBalance": "5.67"}
            raise AssertionError(endpoint)

        with mock.patch.object(binance_executor, "_request", side_effect=fake_request), \
             mock.patch.object(binance_executor, "_resolve_pm_base_candidates", return_value=["https://papi.binance.com"]):
            result = binance_executor.get_account_balance("ak", "sk")

        self.assertEqual("/papi/v1/account", result["_endpoint"])
        self.assertEqual("pm", binance_executor._get_preferred_api_mode("ak"))
        self.assertEqual(("/fapi/v2/account", None), calls[0])
        self.assertEqual(("/fapi/v2/balance", None), calls[1])
        self.assertEqual(("/papi/v1/account", "https://papi.binance.com"), calls[2])

    def test_pm_preference_skips_fapi_for_order_endpoints_after_detection(self):
        calls = []
        cache_key = binance_executor._api_mode_cache_key("ak")
        binance_executor._API_MODE_BY_KEY[cache_key] = "pm"

        def fake_request(api_key, api_secret, method, endpoint, params=None, base_url=None, max_retries=3):
            calls.append((method, endpoint, base_url))
            if endpoint == "/papi/v1/um/order":
                return {"orderId": 789}
            raise AssertionError(endpoint)

        with mock.patch.object(binance_executor, "_request", side_effect=fake_request), \
             mock.patch.object(binance_executor, "_resolve_pm_base_candidates", return_value=["https://papi.binance.com"]), \
             mock.patch.object(binance_executor, "get_position_mode", return_value={"mode": "hedge", "dualSidePosition": True}), \
             mock.patch.object(binance_executor, "set_symbol_leverage", return_value={}), \
             mock.patch.object(binance_executor, "set_margin_type", return_value={}), \
             mock.patch.object(binance_executor, "get_ticker_price", return_value=100.0), \
             mock.patch.object(binance_executor, "get_symbol_filters", return_value={"minQty": 0.001, "stepSize": 0.001, "tickSize": 0.1}):
            result = binance_executor.place_market_order(
                "ak", "sk", "BTCUSDT", "long", 5, "isolated", 50.0, current_price=100.0
            )

        self.assertEqual(789, result["orderId"])
        self.assertEqual([("POST", "/papi/v1/um/order", "https://papi.binance.com")], calls)



class BinanceBalanceNormalizationTests(unittest.TestCase):
    def setUp(self):
        binance_executor._API_MODE_BY_KEY.clear()
        binance_executor._POSITION_MODE_BY_KEY.clear()

    def tearDown(self):
        binance_executor._API_MODE_BY_KEY.clear()
        binance_executor._POSITION_MODE_BY_KEY.clear()

    def test_account_balance_prefers_total_margin_balance_for_realtime_equity(self):
        def fake_request(api_key, api_secret, method, endpoint, params=None, base_url=None, max_retries=3):
            if endpoint == "/fapi/v2/account":
                return {
                    "asset": "USDT",
                    "totalWalletBalance": "226.42",
                    "totalMarginBalance": "224.8944",
                    "availableBalance": "120.00",
                }
            if endpoint == "/fapi/v2/balance":
                return []
            raise AssertionError(endpoint)

        with mock.patch.object(binance_executor, "_request", side_effect=fake_request), \
             mock.patch.object(binance_executor, "_resolve_pm_base_candidates", return_value=[]):
            result = binance_executor.get_account_balance("ak", "sk")

        self.assertAlmostEqual(224.8944, result["balance"], places=6)
        self.assertAlmostEqual(120.0, result["availableBalance"], places=6)

    def test_account_balance_prefers_pm_account_candidate_with_virtual_max_withdraw(self):
        def fake_request(api_key, api_secret, method, endpoint, params=None, base_url=None, max_retries=3):
            if endpoint.startswith("/fapi/"):
                raise ValueError("fapi unavailable")
            if endpoint == "/papi/v1/account":
                return {
                    "accountEquity": "228.25",
                    "accountInitialMargin": "96.00",
                    "totalOpenOrderInitialMargin": "0",
                    "virtualMaxWithdrawAmount": "132.25",
                }
            if endpoint == "/papi/v1/balance":
                return [{
                    "asset": "USDT",
                    "totalWalletBalance": "228.25",
                    "crossMarginFree": "120.00",
                }]
            raise ValueError(endpoint)

        with mock.patch.object(binance_executor, "_request", side_effect=fake_request),              mock.patch.object(binance_executor, "_resolve_pm_base_candidates", return_value=["https://papi.binance.com"]):
            result = binance_executor.get_account_balance("ak", "sk")

        self.assertEqual("/papi/v1/account", result["_endpoint"])
        self.assertAlmostEqual(228.25, result["balance"], places=6)
        self.assertAlmostEqual(132.25, result["availableBalance"], places=6)

    def test_account_balance_uses_cross_margin_free_from_papi_balance_rows(self):
        def fake_request(api_key, api_secret, method, endpoint, params=None, base_url=None, max_retries=3):
            if endpoint.startswith("/fapi/"):
                raise ValueError("fapi unavailable")
            if endpoint == "/papi/v1/account":
                raise ValueError("pm account unavailable")
            if endpoint == "/papi/v1/balance":
                return [{
                    "asset": "USDT",
                    "totalWalletBalance": "228.25",
                    "crossMarginFree": "132.25",
                    "umUnrealizedPNL": "4.50",
                }]
            raise ValueError(endpoint)

        with mock.patch.object(binance_executor, "_request", side_effect=fake_request),              mock.patch.object(binance_executor, "_resolve_pm_base_candidates", return_value=["https://papi.binance.com"]):
            result = binance_executor.get_account_balance("ak", "sk")

        self.assertEqual("/papi/v1/balance", result["_endpoint"])
        self.assertAlmostEqual(228.25, result["balance"], places=6)
        self.assertAlmostEqual(132.25, result["availableBalance"], places=6)
if __name__ == "__main__":
    unittest.main()
