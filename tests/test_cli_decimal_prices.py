# -*- coding: utf-8 -*-
import math
import unittest
from unittest import mock
import _okxorder as ox

class DecimalWireTests(unittest.TestCase):
    def setUp(self):
        self.env=mock.patch.dict("os.environ",{"OKX_EXECUTOR_DRYRUN":"0"});self.env.start();self.addCleanup(self.env.stop)

    def capture(self,method,*args,**kwargs):
        with mock.patch.object(ox,"okx_json",return_value={"code":"0","data":[{"sCode":"0","algoId":"A","ordId":"O"}]}) as call:
            method(*args,**kwargs)
        return call.call_args.args

    def test_failed_floki_stop_keeps_value_without_exponent(self):
        args=self.capture(ox.place_market_open,"FLOKI-USDT-SWAP","long",100,"live",sl_trigger_px=2.38e-5)
        price=args[args.index("--slTriggerPx")+1]
        self.assertEqual(price,"0.0000238");self.assertEqual(float(price),2.38e-5)
        self.assertIn("--slOrdPx=-1",args)

    def test_sl_tp_and_oco_prices_are_plain_decimal(self):
        calls=[(ox.place_algo_sl,("X-USDT-SWAP","long",1,2.38e-5,"live"),{}),
               (ox.place_algo_tp,("X-USDT-SWAP","long",1,2.527e-5,"live"),{}),
               (ox.place_algo_protection,("X-USDT-SWAP","long",1,2.38e-5,"live"),{"tp_trigger_px":2.527e-5})]
        for method,args,kwargs in calls:
            with self.subTest(method=method.__name__):
                sent=self.capture(method,*args,**kwargs)
                for flag in ("--slTriggerPx","--tpTriggerPx"):
                    if flag in sent:self.assertNotIn("e",sent[sent.index(flag)+1].lower())

    def test_amend_fields_and_small_size_preserve_exact_values(self):
        args=self.capture(ox.amend_algo_protection,"X-USDT-SWAP","A","live",new_sl_trigger_px=2.38e-5,new_tp_trigger_px=2.527e-5,new_sz=1e-7)
        self.assertEqual(args[args.index("--newSz")+1],"0.0000001")
        self.assertEqual(args[args.index("--newSlTriggerPx")+1],"0.0000238")
        self.assertIn("--newSlOrdPx=-1",args)

    def test_normal_decimal_representation_stays_compatible(self):
        for value in (1,1.0,8.6,7800,0.27):self.assertEqual(ox._format_cli_decimal(value),str(value))

    def test_nonfinite_and_pathological_exponents_do_not_reach_cli(self):
        with mock.patch.object(ox,"okx_json") as call:
            for value in (math.nan,math.inf,-math.inf,"1e-999999"):
                with self.subTest(value=value),self.assertRaises(ValueError):
                    ox.place_algo_sl("X-USDT-SWAP","long",1,value,"live")
        call.assert_not_called()

if __name__=="__main__":unittest.main()
