import unittest
from unittest import mock

import order_executor


class BitgetOrderExecutorTests(unittest.TestCase):
    def setUp(self):
        order_executor._SYMBOL_RULES.clear()

    def tearDown(self):
        order_executor._SYMBOL_RULES.clear()

    def test_calc_size_aligns_to_symbol_step_and_min_qty(self):
        with mock.patch.object(order_executor, 'get_symbol_rules', return_value={'sizeMultiplier': '1'}),              mock.patch.object(order_executor, 'get_min_order_requirements', return_value={'requiredQtyStr': '5'}):
            size = order_executor._calc_size(
                7.142857142857143,
                1,
                1.3979,
                symbol='XRPUSDT',
            )

        self.assertEqual('5', size)

    def test_place_market_order_reconciles_fill_after_45111_error(self):
        with mock.patch.object(order_executor, 'set_symbol_leverage', return_value={}),              mock.patch.object(order_executor, '_request', side_effect=ValueError('HTTP 400 | code=45111 | trigger min qty')),              mock.patch.object(order_executor, 'get_order_detail', return_value={
                 'state': 'filled',
                 'size': '5',
                 'priceAvg': '1.3979',
                 'orderId': 'oid-1',
                 'clientOid': 'cid-1',
             }),              mock.patch.object(order_executor, 'get_symbol_rules', return_value={'sizeMultiplier': '1'}),              mock.patch.object(order_executor, 'get_min_order_requirements', return_value={'requiredQtyStr': '5'}):
            result = order_executor.place_market_order(
                'ak',
                'sk',
                'pp',
                'XRPUSDT',
                'short',
                1,
                'isolated',
                7.142857142857143,
                current_price=1.3979,
                client_oid='cid-1',
            )

        self.assertEqual('oid-1', result['orderId'])
        self.assertEqual('5', result['_calculated_size'])
        self.assertTrue(result['_reconciled_after_error'])


if __name__ == '__main__':
    unittest.main()
