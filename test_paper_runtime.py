import copy
import contextlib
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError
import paper_runtime as rt
import execution_quality as quality
import state_guard

ROOT=pathlib.Path(__file__).parent

def module(path,name):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod

spot=module('现货/现货机器人.py','spot_runtime_tests')
futures=module('合约/合约机器人.py','futures_runtime_tests')


class RuntimeTests(unittest.TestCase):
    def setUp(self):rt._blocked_until.clear()

    def gate(self,price=100,age=0):
        return {'available':True,'reference_mid':price,'price_epoch_ms':int((rt.time.time()-age)*1000),'passes':True}

    def test_rate_limit_respected_without_minute_sleep(self):
        error=HTTPError('https://example.test/x',429,'limited',{'Retry-After':'60'},None)
        with mock.patch.object(rt.urllib.request,'urlopen',side_effect=error) as fetch, mock.patch.object(rt.time,'sleep') as sleep:
            with self.assertRaises(HTTPError):rt.bounded_http_get('https://example.test/x')
            with self.assertRaisesRegex(RuntimeError,'cooldown'):rt.bounded_http_get('https://example.test/y')
        self.assertEqual(fetch.call_count,1);sleep.assert_not_called()

    def test_forbidden_host_cache_is_process_scoped_and_expires(self):
        error=HTTPError('https://example.test/x',403,'denied',{},None)
        with mock.patch.object(rt.time,'monotonic',return_value=100), mock.patch.object(rt.urllib.request,'urlopen',side_effect=error):
            with self.assertRaises(HTTPError):rt.bounded_http_get('https://example.test/x')
        with mock.patch.object(rt.time,'monotonic',return_value=401), mock.patch.object(rt.urllib.request,'urlopen',return_value=io.BytesIO(b'{}')):
            self.assertEqual(rt.bounded_http_get('https://example.test/x'),'{}')

    def test_slow_history_honours_retry_after_without_losing_coverage(self):
        clock=[100.0]
        error=HTTPError('https://api.coingecko.com/x',429,'limited',{'Retry-After':'60'},None)
        with mock.patch.object(rt.time,'monotonic',side_effect=lambda:clock[0]), \
             mock.patch.object(rt.time,'sleep',side_effect=lambda s:clock.__setitem__(0,clock[0]+s)) as sleep, \
             mock.patch.object(rt.urllib.request,'urlopen',side_effect=[error,io.BytesIO(b'{}')]) as fetch:
            self.assertEqual(spot.http_get('https://api.coingecko.com/x'),'{}')
        self.assertEqual(fetch.call_count,2);sleep.assert_called_once_with(60)

    def test_waiting_host_cooldown_is_counted_in_slow_budget(self):
        clock=[100.0];rt._blocked_until['api.coingecko.com']=160
        with mock.patch.object(rt.time,'monotonic',side_effect=lambda:clock[0]), \
             mock.patch.object(rt.time,'sleep',side_effect=lambda s:clock.__setitem__(0,clock[0]+s)), \
             mock.patch.object(rt.urllib.request,'urlopen',return_value=io.BytesIO(b'{}')) as fetch:
            self.assertEqual(futures.http_get('https://api.coingecko.com/x'),'{}')
        self.assertEqual(clock[0],160);self.assertEqual(fetch.call_count,1)

    def test_oi_baseline_is_preserved_in_explicit_independent_account(self):
        a=spot.new_account();a['_oi_snapshots']={'BTC':{'oi':100,'ts':100}}
        other=spot.new_account();before=copy.deepcopy(other)
        with mock.patch.object(spot,'get_derivatives_bybit',return_value=None), \
             mock.patch.object(spot.time,'time',return_value=3700), \
             mock.patch.object(spot,'safe_json',side_effect=[{'data':[{'fundingRate':'0.0001'}]}, {'data':[{'oiUsd':'110'}]}]):
            observation=spot.get_derivatives('BTC',a)
        self.assertAlmostEqual(observation['oi_change_pct'],10)
        self.assertEqual(observation['oi_change_window_h'],1)
        self.assertEqual(a['_oi_snapshots']['BTC'],{'oi':110,'ts':3700})
        self.assertEqual(other,before)

    def test_unknown_snapshot_retains_financial_history_and_peak(self):
        a=spot.new_account();a['cash']=9900
        a['positions']={'BTC':{'quantity':1,'entry_price':100}}
        before=copy.deepcopy(a)
        eq,_=rt.snapshot(a,{'BTC':self.gate(age=31)})
        self.assertIsNone(eq);self.assertIsNone(a['last_equity'])
        for key in ('cash','positions','trades','peak_equity'):self.assertEqual(a[key],before[key])
        self.assertEqual(a['runtime']['valuation_status'],'UNAVAILABLE')

    def test_mark_source_never_falls_back_to_spot(self):
        with mock.patch.object(rt,'mark_quote',side_effect=ValueError('missing')):
            self.assertIsNone(futures.get_okx_mark_price('BTC',99999))
        with mock.patch.object(rt,'okx',return_value=[{'markPx':'100','ts':'0'}]):
            with self.assertRaises(ValueError):rt.mark_quote('BTC')

    def test_spot_fast_stop_before_any_slow_analysis_and_no_reopen(self):
        a=spot.new_account();a['cash']=9900
        a['positions']={'BTC':dict(quantity=1,entry_price=100,entry_fee=.1,highest_price=100,stop_loss=90,take_profit=120)}
        with mock.patch.object(rt,'execution_quote',return_value=self.gate(80)),mock.patch.object(spot,'save_account'):
            exited,_=spot.fast_risk(a)
        self.assertEqual(exited,{'BTC'});self.assertEqual(len(a['trades']),1)
        self.assertEqual(a['trades'][0]['side'],'SELL')

    def test_snapshot_does_not_double_deduct_recorded_costs(self):
        a=spot.new_account();a['cash']=9899;a['fees_paid']=1
        a['positions']={'BTC':dict(quantity=1,entry_price=100)}
        eq,_=rt.snapshot(a,{'BTC':self.gate(110)})
        self.assertEqual(eq,10009)
        rt.snapshot(a,{'BTC':self.gate(108)})
        self.assertEqual(a['peak_equity'],10009)
        self.assertGreater(a['max_observed_drawdown'],0)

    def futures_account(self):
        a=futures.new_futures_account();a['cash']=9900
        a['positions']={'BTC':dict(side='LONG',quantity=1,entry_price=100,margin=100,initial_margin=100,
              entry_fee=0,funding_pnl=0,last_funding_ts=0,best_price=100,stop_price=90,take_profit=120)}
        return a

    def test_futures_close_does_not_double_count_margin_in_new_day(self):
        a=self.futures_account()
        futures.close_futures(a,'BTC',105,'test',None)
        self.assertEqual(a['positions'],{})
        daily=a['daily'][futures.utc_day()]
        self.assertAlmostEqual(daily['start_equity'],a['cash'])
        self.assertAlmostEqual(a['realized_pnl'],a['cash']-10000)

    def test_missing_mark_still_allows_observed_stop_not_fake_liquidation(self):
        a=self.futures_account()
        with mock.patch.object(futures,'apply_new_funding') as funding:
            result=futures.manage_futures(a,'BTC',80,None,None)
        funding.assert_not_called()
        self.assertTrue(result.startswith('CLOSE LONG'))
        self.assertEqual(a['liquidations'],0)

    def test_missing_other_mark_blocks_entry_not_closing(self):
        a=self.futures_account()
        allowed,_=futures.entry_allowed(a,{})
        self.assertFalse(allowed)
        futures.close_futures(a,'BTC',105,'test',None)
        self.assertEqual(a['positions'],{})

    def test_fast_checkpoint_survives_analysis_source_failure(self):
        a=spot.new_account()
        with mock.patch.object(spot,'load_account',return_value=a), \
             mock.patch.object(spot,'save_account') as save, \
             mock.patch.object(spot,'get_macro',side_effect=RuntimeError('slow source failed')), \
             mock.patch('sys.stdout',new_callable=io.StringIO):
            with self.assertRaises(RuntimeError):spot.run_paper()
        self.assertGreaterEqual(save.call_count,1)
        self.assertEqual(a['runtime']['valuation_status'],'OK')

    def test_label_deadline_keeps_financial_state_and_no_request(self):
        a={'cash':123,'positions':{},'execution_ab_signals':[{'epoch':1,'symbol':'BTC','gate':{},'outcomes':{}}]}
        with mock.patch.object(quality,'historical_close') as fetch:
            quality.settle_execution_ab(a,'BTC',deadline=0)
        fetch.assert_not_called();self.assertEqual(a['cash'],123)

    def test_history_guard_allows_append_not_rewrite(self):
        old={'created_at':'original','trades':[{'side':'BUY','fee':1}]}
        new=copy.deepcopy(old);new['trades'].append({'side':'SELL','fee':2})
        state_guard.verify(old,new)
        new['trades'][0]['fee']=0
        with self.assertRaises(ValueError):state_guard.verify(old,new)
        new=copy.deepcopy(old);new['created_at']='reset'
        with self.assertRaises(ValueError):state_guard.verify(old,new)

    def test_missing_and_nonfinite_account_cannot_bootstrap_in_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            path=pathlib.Path(directory)/'paper.json'
            with self.assertRaises(ValueError):state_guard.checked(path)
            path.write_text(json.dumps({'cash':float('nan'),'trades':[]}))
            with self.assertRaises(ValueError):state_guard.checked(path)

    def run_mocked_bot(self,bot,a,refreshed=None,enforce=None):
        gate=self.gate(100)
        values={'SYMBOLS':['BTC'],'get_macro':{},'macro_dimension':(0,[]),
            'get_fear_greed':(50,None),'sentiment_dimension':(0,[]),
            'get_market_data':[{'id':'bitcoin','current_price':1,'price_change_percentage_24h':0}],
            'get_stablecoin_liquidity':{},'stablecoin_dimension':(0,[]),
            'get_history':([100]*260,[100]*260),
            'technical_dimension':(60,[],{'regime':'BULL_TREND','rsi':50}),
            'get_derivatives':{'source':'test','status':'OK'},'derivatives_dimension':(0,[]),
            'free_etf_dimension':(0,[]),'get_exchange_flow':{},'flow_dimension':(0,[]),
            'combine_dimensions':(50,{}),'data_coverage':(100,['all']),
            'assess_okx_orderbook':gate}
        with contextlib.ExitStack() as stack:
            for name,value in values.items():
                stack.enter_context(mock.patch.object(bot,name,value) if name=='SYMBOLS' else mock.patch.object(bot,name,return_value=value))
            loader='load_account' if bot is spot else 'load_futures_account'
            writer='save_account' if bot is spot else 'save_futures_account'
            stack.enter_context(mock.patch.object(bot,loader,return_value=a))
            stack.enter_context(mock.patch.object(bot,writer))
            stack.enter_context(mock.patch.object(rt,'execution_quote',return_value=gate if refreshed is None else refreshed))
            if enforce is not None:stack.enter_context(mock.patch.object(bot,'ORDERFLOW_ENFORCEMENT',enforce))
            stack.enter_context(mock.patch.object(rt,'mark_quote',return_value=gate))
            stack.enter_context(mock.patch.object(rt,'finish_labels'))
            if bot is futures:stack.enter_context(mock.patch.object(bot,'get_latest_realized_funding',return_value={'rate':0,'ts':1}))
            stack.enter_context(mock.patch('sys.stdout',new_callable=io.StringIO))
            bot.run_paper()

    def test_full_main_paths_fresh_fills_and_repeat_is_idempotent(self):
        for bot in (spot,futures):
            with self.subTest(bot=bot.__name__):
                a=bot.new_account() if bot is spot else bot.new_futures_account()
                self.run_mocked_bot(bot,a)
                self.assertEqual(len(a['positions']),1)
                self.assertGreater(a['positions']['BTC']['entry_price'],99)
                history=copy.deepcopy(a['trades'])
                self.run_mocked_bot(bot,a)
                self.assertEqual(a['trades'],history)
                self.assertEqual(a['runtime']['valuation_status'],'OK')

    def test_refreshed_quote_must_pass_enforced_gate_again(self):
        rejected=self.gate();rejected['passes']=False
        for bot in (spot,futures):
            with self.subTest(bot=bot.__name__):
                a=bot.new_account() if bot is spot else bot.new_futures_account()
                with mock.patch.object(rt,'fresh',side_effect=[True,False]):
                    self.run_mocked_bot(bot,a,refreshed=rejected,enforce=True)
                self.assertEqual(a['positions'],{});self.assertEqual(a['trades'],[])

    def test_unknown_valuation_can_reload_without_reset(self):
        for bot,loader,writer,key in ((spot,'load_account','save_account','STATE_FILE'),
                (futures,'load_futures_account','save_futures_account','FUTURES_STATE_FILE')):
            with self.subTest(bot=bot.__name__),tempfile.TemporaryDirectory() as d:
                a=bot.new_account() if bot is spot else bot.new_futures_account()
                a['last_equity']=None;a['runtime']={'valuation_status':'UNAVAILABLE'}
                with mock.patch.object(bot,key,str(pathlib.Path(d)/'state.json')):
                    getattr(bot,writer)(a)
                    self.assertEqual(getattr(bot,loader)(),a)


if __name__=='__main__':unittest.main()
