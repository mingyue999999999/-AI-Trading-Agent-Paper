import copy
import io
import json
import unittest
from unittest import mock
import test_execution_timing as timing
bot=timing.bot


class ValuationTests(unittest.TestCase):
    setUp=timing.ExecutionTimingTests.setUp
    gate=timing.ExecutionTimingTests.gate
    run_paper=timing.ExecutionTimingTests.run_paper

    def test_unknown_price_does_not_fake_entry_value_or_update_peak(self):
        before=copy.deepcopy(self.a)
        report=self.run_paper(lambda *args:self.gate(age=40),lambda *args:None)
        self.assertIsNone(report['metrics']['equity'])
        self.assertIsNone(report['metrics']['net_return_pct'])
        self.assertEqual(report['metrics']['valuation_status'],'UNAVAILABLE')
        self.assertEqual(self.a['peak_equity'],before['peak_equity'])
        self.assertEqual(self.a['equity_curve'],before['equity_curve'])
        self.assertEqual(self.a['trades'],before['trades'])

    def test_unknown_other_position_cannot_disable_known_stop(self):
        report=self.run_paper(lambda s,*args:self.gate(age=40) if s=='ETH' else self.gate(),lambda *args:None)
        self.assertEqual([t['symbol'] for t in self.a['trades']],['BTCUSDT'])
        self.assertIn('ETHUSDT',self.a['positions'])
        self.assertIsNone(report['metrics']['equity'])

    def test_fixed_stop_survives_history_failure(self):
        with mock.patch.object(bot,'fetch_history',side_effect=RuntimeError('history unavailable')), \
             mock.patch.object(bot,'load_json',return_value=self.a), \
             mock.patch.object(bot,'assess_okx_orderbook',side_effect=lambda *args:self.gate()), \
             mock.patch.object(bot,'settle_execution_ab'), \
             mock.patch.object(bot,'save_json'), \
             mock.patch.object(bot.time,'time',return_value=self.clock), \
             mock.patch('sys.stdout',new_callable=io.StringIO):
            bot.paper(self.c)
        self.assertEqual(len(self.a['trades']),2)
        self.assertEqual(self.a['positions'],{})

    def test_missing_mark_blocks_new_entries(self):
        with mock.patch.object(bot,'signal',return_value=(.9,{})):
            out=bot.process_bar(self.a,self.c,'SOLUSDT',self.b,{'SOLUSDT':100},
                execution_price=100,execution_time=1800000000000,decision_bar=259000,live=True)
        self.assertEqual(out['action'],'SKIP_PORTFOLIO_VALUATION')
        self.assertEqual(self.a['trades'],[])


class SourcesTests(unittest.TestCase):
    def setUp(self):
        bot.SOURCE_EVENTS.clear()
        bot.SOURCE_BLOCKS.clear()
        bot.SOURCE_SKIP_REPORTED.clear()

    def response(self,obj):return io.StringIO(json.dumps(obj))

    def http_error(self,code):
        return bot.urllib.error.HTTPError("https://blocked.example",code,"blocked",{},None)

    def test_empty_bybit_falls_back_to_valid_okx(self):
        replies=[OSError('451'),self.response({'retCode':0,'result':{'list':[]}}),
                 self.response({'code':'0','data':[['0','100','102','99','101','10','0','0','1']]})]
        with mock.patch.object(bot.urllib.request,'urlopen',side_effect=replies) as fetch:
            out=bot.fetch_klines('BTCUSDT')
        self.assertEqual(out[0][4],101)
        self.assertEqual(fetch.call_count,3)

    def test_permanent_access_errors_use_process_cooldown(self):
        replies=[self.http_error(451),self.http_error(403),
                 self.response({'code':'0','data':[['0','100','102','99','101','10','0','0','1']]}),
                 self.response({'code':'0','data':[['0','100','102','99','101','10','0','0','1']]})]
        with mock.patch.object(bot.urllib.request,'urlopen',side_effect=replies) as fetch:
            self.assertEqual(bot.fetch_klines('BTCUSDT')[0][4],101)
            self.assertEqual(bot.fetch_klines('BTCUSDT',end_time=1)[0][4],101)
        self.assertEqual(fetch.call_count,4)
        self.assertEqual([e['status'] for e in bot.SOURCE_EVENTS if e['source']=='Binance'],
                         ['ERROR','SKIPPED'])
        self.assertEqual([e['status'] for e in bot.SOURCE_EVENTS if e['source']=='Bybit'],
                         ['ERROR','SKIPPED'])

    def test_valid_binance_needs_no_fallback(self):
        with mock.patch.object(bot.urllib.request,'urlopen',return_value=self.response([[0,100,102,99,101,10,1000]])) as fetch:
            self.assertEqual(bot.fetch_klines('BTCUSDT')[0][4],101)
        self.assertEqual(fetch.call_count,1)

    def test_all_empty_sources_fail_closed(self):
        replies=[self.response([]),self.response({'retCode':0,'result':{'list':[]}}),self.response({'code':'0','data':[]})]
        with mock.patch.object(bot.urllib.request,'urlopen',side_effect=replies):
            with self.assertRaises(ValueError):bot.fetch_klines('BTCUSDT')

    def test_invalid_prices_and_no_progress_rejected(self):
        for bad in (float('nan'),float('inf'),0):
            with self.assertRaises(ValueError):bot.validated_candles([[0,bad,102,99,101,10,1000]])
        with mock.patch.object(bot,'fetch_klines',return_value=[[100,100,102,99,101,10,1000],[200,101,103,100,102,10,2000]]), \
             mock.patch.object(bot.time,'sleep'):
            with self.assertRaisesRegex(ValueError,'non-advancing'):bot.fetch_history('BTCUSDT','4h',5)


if __name__=='__main__':unittest.main()
