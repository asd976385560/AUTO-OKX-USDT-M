import sys,unittest
from pathlib import Path
from unittest import mock
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core import order_executor as executor

class FillIdentityTests(unittest.TestCase):
    def test_fills_keep_complete_identity_without_guessing(self):
        fills=[{'ordId':'a','fillSz':'1','fillPx':'100','fillTime':'1788580000000'},
               {'ordId':'a','fillSz':'2','fillPx':'101','fillTime':'1788580001000'}]
        result=executor._avg_fill(fills)
        self.assertEqual(result['ord_ids'],['a'])
        self.assertEqual(result['fill_sz'],3)
        fills[1]['ordId']='b'
        self.assertEqual(executor._avg_fill(fills)['ord_ids'],['a','b'])
        fills[1].pop('ordId')
        self.assertEqual(executor._avg_fill(fills)['ord_ids'],[])

    def test_order_status_and_filtered_history_keep_identity(self):
        row={'ordId':'a','state':'filled','accFillSz':'1','avgPx':'100','uTime':'1788580001000',
             'cTime':'1788580000000','posSide':'long','reduceOnly':'true'}
        self.assertEqual(executor._fill_from_order(row)['ord_ids'],['a'])
        old={**row,'ordId':'old','cTime':'1688580000000'}
        opposite={**row,'ordId':'opposite','posSide':'short'}
        with mock.patch.object(executor.ox,'get_orders_history',return_value=[old,opposite,row]):
            result=executor._find_orders_since('TEST-USDT-SWAP','live','long',1788580000000,True)
        self.assertEqual(result['ord_ids'],['a'])
        self.assertEqual(result['fill_sz'],1)

if __name__=='__main__':unittest.main()
