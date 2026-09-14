import copy
from decimal import Decimal
from pathlib import Path
import sys
import unittest
from unittest import mock
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import order_executor as executor

def fills(sizes):
    return [{'ordId':'exact-order','fillSz':s,'fillPx':'0.07242',
             'fillPnl':'0','fillTime':str(1789200601984+i)} for i,s in enumerate(sizes)]

class FillSizeDecimalTests(unittest.TestCase):
    def test_lab_real_fill_partition_and_wire_quantity(self):
        self.assert_exact_wire(['30','0.1','0.1','0.2'], '30.4')

    def test_cgnx_real_fill_partition_and_wire_quantity(self):
        self.assert_exact_wire(['0.1','0.1','0.2','0.1','0.1','0.1','0.1','0.1','0.1'], '1')

    def assert_exact_wire(self, sizes, expected):
        rows=fills(sizes)
        original=copy.deepcopy(rows)
        result=executor._avg_fill(rows)
        self.assertEqual(Decimal(str(result['fill_sz'])),Decimal(expected))
        self.assertEqual(result['ord_ids'],['exact-order'])
        self.assertEqual(result['fill_ts'],'2026-09-12 16:10:01')
        self.assertEqual(rows,original)
        self.assertAlmostEqual(result['fill_px'],0.07242)
        with mock.patch.object(executor.ox,'is_dryrun',return_value=False), mock.patch.object(executor.ox,'_call',return_value={'ok':True}) as call:
            executor.ox.place_algo_tp('LAB-USDT-SWAP','short',result['fill_sz'],0.07021,'live')
        args=call.call_args.args
        self.assertEqual(Decimal(args[args.index('--sz')+1]),Decimal(expected))
        self.assertIn('--reduceOnly',args)

    def test_small_decimal_fills_preserve_exact_total(self):
        self.assertEqual(executor._avg_fill(fills(['0.1','0.2']))['fill_sz'],0.3)

    def test_quantity_is_not_rounded_to_an_assumed_lot(self):
        self.assertEqual(executor._avg_fill(fills(['0.07','0.08']))['fill_sz'],0.15)

    def test_empty_and_missing_sizes_keep_existing_zero_behavior(self):
        self.assertEqual(executor._avg_fill([])['fill_sz'],0.0)
        self.assertEqual(executor._avg_fill([{'fillPx':'5'}])['fill_sz'],0.0)

    def test_multi_order_history_has_same_decimal_sum(self):
        rows=[{'ordId':str(i),'state':'filled','accFillSz':s,'avgPx':'0.07242','pnl':'0',
               'cTime':'1789200601984','uTime':'1789200601984','posSide':'short','reduceOnly':'true'}
              for i,s in enumerate(['30','0.1','0.1','0.2'])]
        old={**rows[0],'ordId':'old','cTime':'1689200601984'}
        opposite={**rows[0],'ordId':'opposite','posSide':'long'}
        with mock.patch.object(executor.ox,'get_orders_history',return_value=[old,opposite,*rows]):
            result=executor._find_orders_since('LAB-USDT-SWAP','live','short',1789200601984,True)
        self.assertEqual(result['fill_sz'],30.4)
        self.assertEqual(result['ord_ids'],['0','1','2','3'])

    def test_partial_terminal_order_retains_actual_fill_quantity(self):
        result=executor._fill_from_order({'ordId':'partial','state':'canceled','sz':'2',
                 'accFillSz':'0.3','avgPx':'5','uTime':'1789200601984'})
        self.assertEqual(result['fill_sz'],0.3)
        self.assertTrue(result['partial'])

if __name__=='__main__':unittest.main()
