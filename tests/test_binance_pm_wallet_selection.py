import sys
import unittest
from unittest import mock

sys.modules.pop("binance_executor", None)
import binance_executor


class BinancePmWalletSelectionTests(unittest.TestCase):
    def setUp(self):
        binance_executor._API_MODE_BY_KEY.clear()
        binance_executor._POSITION_MODE_BY_KEY.clear()

    def tearDown(self):
        binance_executor._API_MODE_BY_KEY.clear()
        binance_executor._POSITION_MODE_BY_KEY.clear()

    def test_prefers_pm_total_equity_over_um_subaccount_available(self):
        def fake_request(api_key, api_secret, method, endpoint, params=None, base_url=None, max_retries=3):
            if endpoint.startswith("/fapi/"):
                raise ValueError("fapi unavailable")
            if endpoint == "/papi/v1/account":
                return {
                    "accountEquity": "232.42244917",
                    "actualEquity": "232.44569374",
                    "totalAvailableBalance": "0.0",
                    "uniMMR": "132.79430053",
                }
            if endpoint == "/papi/v1/um/account":
                return {
                    "assets": [
                        {"asset": "USDT", "crossWalletBalance": "0.07550501", "crossUnPnl": "6.63013460"},
                    ]
                }
            if endpoint == "/papi/v1/cm/account":
                return {"assets": []}
            if endpoint == "/papi/v1/balance":
                return [{"asset": "USDT"}]
            raise ValueError(endpoint)

        with mock.patch.object(binance_executor, "_request", side_effect=fake_request), \
             mock.patch.object(binance_executor, "_resolve_pm_base_candidates", return_value=["https://papi.binance.com"]):
            result = binance_executor.get_account_balance("ak", "sk")

        self.assertEqual("/papi/v1/account", result["_endpoint"])
        self.assertAlmostEqual(232.44569374, result["balance"], places=6)
        self.assertAlmostEqual(0.0, result["availableBalance"], places=6)


if __name__ == "__main__":
    unittest.main()
