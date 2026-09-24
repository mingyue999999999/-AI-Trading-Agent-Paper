import copy
import unittest
from unittest.mock import patch
import execution_quality as q


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        q.historical_close.cache_clear()
        q.contract_units.cache_clear()
        q._label_requests = 0

    def book(self, now=10000, bid_size=40, ask_size=20):
        return {"ts": str(now*1000), "bids": [["100.00", str(bid_size), "0", "1"]]*10,
                "asks": [["100.02", str(ask_size), "0", "1"]]*10}

    def candle(self, symbol, market, target):
        return {"price": {1000:100, 1300:101, 1900:102, 2800:99}[target],
                "price_epoch_ms": target//60*60000, "method": "last_closed_minute_at_or_before_target"}

    def test_direction_and_contract_notional(self):
        def api(path, **kwargs):
            if "instruments" in path:
                return [{"ctType":"linear", "ctValCcy":"BTC", "settleCcy":"USDT", "ctVal":"0.01", "ctMult":"1"}]
            return [self.book()]
        with patch.object(q, "okx", side_effect=api), patch.object(q.time, "time", return_value=10000):
            buy = q.assess_okx_orderbook("BTC", "BUY")
            short = q.assess_okx_orderbook("BTC", "SHORT")
            swap = q.assess_okx_orderbook("BTC", "LONG", "SWAP")
        self.assertTrue(buy["passes"])
        self.assertFalse(short["passes"])
        self.assertAlmostEqual(swap["bid_depth_usd"], buy["bid_depth_usd"]*.01)
        self.assertFalse(swap["passes"])

    def test_stale_book_unavailable(self):
        with patch.object(q, "okx", return_value=[self.book(now=9900)]), patch.object(q.time,"time",return_value=10000):
            self.assertFalse(q.assess_okx_orderbook("BTC", "BUY")["available"])

    def test_late_poll_fills_each_target_not_current_price(self):
        a = {"cash": 9123, "positions": {"ETH": {"qty":1}}, "execution_ab_signals": [{"epoch":1000,"symbol":"BTC","side":"BUY","reference_price":111,
             "gate":{"market":"SPOT"}, "outcomes":{"5m":{"directional_return_pct":50},"15m":{"directional_return_pct":50}}}]}
        financial = copy.deepcopy({k:v for k,v in a.items() if k != "execution_ab_signals"})
        with patch.object(q.time,"time",return_value=6000), patch.object(q,"historical_close",side_effect=self.candle) as get:
            q.settle_execution_ab(a,"BTC",999999)
            first = copy.deepcopy(a)
            q.settle_execution_ab(a,"BTC",1)
        s = a["execution_ab_signals"][0]
        self.assertEqual(a, first)
        self.assertEqual(get.call_count, 4)
        self.assertEqual(financial, {k:v for k,v in a.items() if k != "execution_ab_signals"})
        self.assertEqual(len(s["invalidated_legacy_outcomes"]), 2)
        for h, expected in (("5m",1),("15m",2),("30m",-1)):
            self.assertAlmostEqual(s["outcomes"][h]["directional_return_pct"],expected)
            self.assertFalse(s["outcomes"][h]["ab_eligible"])

    def test_missing_data_quarantines_every_legacy_record(self):
        a={"execution_ab_signals":[{"epoch":1000,"symbol":"BTC","side":"LONG","gate":{},"outcomes":{"5m":{"x":1}}} for _ in range(3)]}
        with patch.object(q.time,"time",return_value=6000), patch.object(q,"historical_close",side_effect=ValueError("missing")) as get:
            q.settle_execution_ab(a,"BTC",999)
        self.assertEqual(get.call_count,1)
        self.assertTrue(all(s["outcomes"] == {} and s["invalidated_legacy_outcomes"] for s in a["execution_ab_signals"]))

    def test_minute_close_no_future_and_exact_target_required(self):
        row=["1200000","100","101","99","100.5","1","1","1","1"]
        with patch.object(q,"okx",return_value=[row]), patch.object(q.time,"time",return_value=9999):
            p=q.historical_close("BTC","SPOT",1300)
            self.assertEqual(p["price_epoch_ms"],1260000)
            self.assertEqual(p["lag_seconds"],40)
        q.historical_close.cache_clear()
        row[8]="0"
        with patch.object(q,"okx",return_value=[row]):
            with self.assertRaises(ValueError): q.historical_close("BTC","SPOT",1300)
        row[8]="1"; row[0]="1260000"
        with patch.object(q,"okx",return_value=[row]):
            with self.assertRaises(ValueError): q.historical_close("BTC","SPOT",1300)

    def test_current_short_observation_is_eligible(self):
        a={}
        gate={"market":"SWAP","available":True,"schema_version":2,"reference_mid":100,"price_epoch_ms":1000000,"passes":False}
        with patch.object(q.time,"time",return_value=1000): q.record_execution_ab(a,"BTC","SHORT",999,60,gate)
        with patch.object(q.time,"time",return_value=1310), patch.object(q,"historical_close",side_effect=self.candle): q.settle_execution_ab(a,"BTC")
        out=a["execution_ab_signals"][0]["outcomes"]
        self.assertEqual(set(out),{"5m"})
        self.assertTrue(out["5m"]["ab_eligible"])
        self.assertAlmostEqual(out["5m"]["directional_return_pct"],-1)


if __name__ == "__main__": unittest.main()
