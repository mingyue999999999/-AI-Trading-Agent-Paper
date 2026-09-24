import importlib.util, pathlib, unittest
from unittest import mock
P=pathlib.Path(__file__).parents[1]/"bot.py"; S=importlib.util.spec_from_file_location("bot",P); bot=importlib.util.module_from_spec(S); S.loader.exec_module(bot)
class TestBot(unittest.TestCase):
    def bars(self, drift=1.0, n=260):
        out=[]; p=100
        for i in range(n):
            o=p; p*=drift; out.append([i*1000,o,max(o,p)*1.01,min(o,p)*.99,p,100+i,i*1000+999])
        return out
    def test_bull_regime(self): self.assertEqual(bot.features(self.bars(1.003))["regime"],"BULL_TREND")
    def test_signal_bounded(self):
        s,_=bot.signal(bot.features(self.bars(1.003))); self.assertTrue(-1<=s<=1)
    def test_position_sizing_cap(self):
        c=bot.load_json(pathlib.Path(__file__).parents[1]/"config.json"); a=bot.new_account(c); b=self.bars(1.004)
        bot.process_bar(a,c,"BTCUSDT",b,{"BTCUSDT":b[-1][1]},1)
        self.assertLessEqual(10000-a["cash"],10000*c["max_position_pct"]+5)
    def test_live_fill_uses_observed_price_and_time(self):
        c=bot.load_json(pathlib.Path(__file__).parents[1]/"config.json")
        a=bot.new_account(c); b=self.bars(1.004)
        with mock.patch.object(bot,"signal",return_value=(0.8,{})):
            bot.process_bar(a,c,"BTCUSDT",b,{"BTCUSDT":125.0},execution_price=125.0,
                            execution_time=999999,decision_bar=b[-1][0],live=True)
        trade=a["trades"][-1]
        self.assertAlmostEqual(trade["price"],125.0*(1+c["slippage_bps"]/10000))
        self.assertEqual(trade["time"],999999)
        self.assertEqual(trade["decision_bar_epoch_ms"],b[-1][0])
        self.assertEqual(a["last_bar"]["BTCUSDT"],b[-1][0])

    def test_trailing_stop_activates_next_bar(self):
        c=bot.load_json(pathlib.Path(__file__).parents[1]/"config.json")
        a=bot.new_account(c); b=self.bars(1.0)
        a["cash"]=9900
        a["positions"]={"BTCUSDT":{"qty":1,"entry":100,"cost":100,"stop":90,"target":200,"highest":100}}
        b[-1][1],b[-1][2],b[-1][3],b[-1][4]=100,120,95,110
        with mock.patch.object(bot,"signal",return_value=(0.2,{})):
            bot.process_bar(a,c,"BTCUSDT",b,{"BTCUSDT":110})
        self.assertIn("BTCUSDT",a["positions"])
        self.assertGreater(a["positions"]["BTCUSDT"]["stop"],95)

    def test_record_equity_updates_peak_without_new_bar(self):
        c=bot.load_json(pathlib.Path(__file__).parents[1]/"config.json")
        a=bot.new_account(c)
        a["cash"]=9900
        a["peak_equity"]=10010
        a["positions"]={"BTCUSDT":{"qty":1,"entry":100}}
        eq=bot.record_equity(a,{"BTCUSDT":125},c["initial_cash"],timestamp_ms=123456)
        self.assertEqual(eq,10025)
        self.assertEqual(a["peak_equity"],10025)
        self.assertEqual(a["equity_curve"][-1],[123456,10025])
        bot.record_equity(a,{"BTCUSDT":90},c["initial_cash"],timestamp_ms=123457)
        self.assertEqual(a["peak_equity"],10025)

    def live_position(self, stop=90, target=120):
        c=bot.load_json(pathlib.Path(__file__).parents[1]/"config.json")
        a=bot.new_account(c); a["cash"]=9900
        a["positions"]={"BTCUSDT":{"qty":1,"entry":100,"cost":100,"stop":stop,
                                    "target":target,"highest":100}}
        return c,a,self.bars(1.0)

    def test_live_stop_runs_without_a_new_signal_bar(self):
        c,a,b=self.live_position()
        a["last_bar"]["BTCUSDT"]=b[-1][0]
        out=bot.process_live_position(a,c,"BTCUSDT",b,{"BTCUSDT":80},80,123456,b[-1][0])
        self.assertEqual(out["action"],"SELL:ATR_STOP")
        self.assertNotIn("BTCUSDT",a["positions"])
        self.assertEqual(a["trades"][-1]["execution_price_source"],"live_orderbook_mid")
        self.assertAlmostEqual(a["trades"][-1]["price"],80*(1-c["slippage_bps"]/10000))

    def test_live_target_and_trail_use_only_observed_price(self):
        c,a,b=self.live_position(target=110)
        out=bot.process_live_position(a,c,"BTCUSDT",b,{"BTCUSDT":112},112,123456,b[-1][0])
        self.assertEqual(out["action"],"SELL:TAKE_PROFIT")
        c,a,b=self.live_position(target=200)
        out=bot.process_live_position(a,c,"BTCUSDT",b,{"BTCUSDT":115},115,123456,b[-1][0])
        self.assertEqual(out["action"],"HOLD:RISK_CHECKED")
        self.assertGreater(a["positions"]["BTCUSDT"]["stop"],90)

    def test_live_bar_signal_does_not_replay_historical_high_low(self):
        c,a,b=self.live_position(stop=90,target=120)
        b[-1][2],b[-1][3]=130,80
        with mock.patch.object(bot,"signal",return_value=(0.2,{})):
            out=bot.process_bar(a,c,"BTCUSDT",b,{"BTCUSDT":100},execution_price=100,
                                execution_time=123456,decision_bar=b[-1][0],live=True)
        self.assertEqual(out["action"],"HOLD")
        self.assertIn("BTCUSDT",a["positions"])

    def test_live_exit_cannot_reenter_on_same_run(self):
        c,a,b=self.live_position()
        a["last_bar"]["BTCUSDT"]=b[-1][0]-1000
        gate={"available":True,"reference_mid":80,"price_epoch_ms":1800000000000}
        with mock.patch.object(bot,"load_json",return_value=a), \
             mock.patch.object(bot,"fetch_history",return_value=b), \
             mock.patch.object(bot,"assess_okx_orderbook",return_value=gate), \
             mock.patch.object(bot,"settle_execution_ab"), \
             mock.patch.object(bot,"save_json"), \
             mock.patch.object(bot,"record_execution_ab"), \
             mock.patch.object(bot.time,"time",return_value=1800000000), \
             mock.patch.object(bot,"signal",return_value=(0.9,{})):
            bot.paper({**c,"symbols":["BTCUSDT"]})
        self.assertNotIn("BTCUSDT",a["positions"])
        self.assertEqual([t["side"] for t in a["trades"]],["SELL"])

    def test_safety_mode(self):
        c=bot.load_json(pathlib.Path(__file__).parents[1]/"config.json"); self.assertEqual(c["mode"],"paper")
if __name__=="__main__":unittest.main()
