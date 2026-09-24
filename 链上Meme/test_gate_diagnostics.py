import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
import gate_diagnostics as g
import new_launch_shadow as launch
import run_new_launch_shadow as runner
import 链上Meme机器人 as base

class Tests(unittest.TestCase):
    def metrics(self, **changes):
        m=dict(price=1, age_h=1, liquidity=100000, volume24=250000, fdv=1000000,
               liq_fdv=.1, buys=250, sells=150, h1=10, h24=20)
        m.update(changes)
        return m

    def pair(self, address='abc', **changes):
        m=self.metrics(**changes)
        return dict(chainId='solana', baseToken=dict(address=address,symbol=address), priceUsd=m['price'],
                    liquidity=dict(usd=m['liquidity']),fdv=m['fdv'], volume=dict(h24=m['volume24']),
                    txns=dict(h24=dict(buys=m['buys'],sells=m['sells'])),
                    priceChange=dict(h1=m['h1'],h24=m['h24']),
                    pairCreatedAt=int((base.time.time()-m['age_h']*3600)*1000))

    def test_all_reasons_and_sole_failure_counts(self):
        a=g.evaluate(self.metrics(liquidity=1000,volume24=10,h1=80))
        self.assertEqual(set(a['failures']),{'liquidity','volume24','h1'})
        b=g.evaluate(self.metrics(liq_fdv=.06))
        summary=g.summarize([dict(gate=a),dict(gate=b)])
        self.assertEqual(summary['sole_failure_counts'],{'liq_fdv':1})
        self.assertEqual(summary['failure_counts']['volume24'],1)

    def test_only_ratio_gate_changes_and_score_formula_is_identical(self):
        m=self.metrics(liquidity=90000,fdv=1500000,liq_fdv=.06)
        self.assertIsNone(launch.score_launch(m))
        self.assertIsNotNone(launch.score_launch(m,.05))
        self.assertFalse(g.evaluate(m)['passed'])
        self.assertTrue(g.evaluate(m,min_liq_fdv=.05)['passed'])
        for ratio in (.08,.1,.5):
            m['liq_fdv']=ratio
            self.assertEqual(launch.score_launch(m),launch.score_launch(m,.05))

    def test_missing_and_nonfinite_not_neutral(self):
        p=self.pair(); del p['priceChange']['h1']
        m=g.snapshot(p,base.time.time())
        self.assertIn('h1',g.evaluate(m)['failures'])
        self.assertIn('price',g.evaluate(self.metrics(price=float('nan')))['failures'])
        self.assertIn('transactions',g.evaluate(self.metrics(buys=-1))['failures'])

    def test_persistent_external_and_new_seeds_all_revisited(self):
        a=launch.new_account(); a['watchlist']['solana:old']=dict(last_seen=1000)
        rows=[dict(chainId='solana',tokenAddress='new')]
        vendor=[dict(chain='solana',address='external',source='gmgn')]
        selected,reg=runner.select_seeds(rows,vendor,{},[a],1000)
        self.assertEqual(set(selected),{'solana:old','solana:new','solana:external'})
        selected,_=runner.select_seeds([],[],reg,[a],1100)
        self.assertIn('solana:new',selected)

    def test_external_blocker_cannot_be_bypassed_by_5pct(self):
        vendor=[dict(chain='solana',address='abc',source='gmgn',honeypot=True)]
        with patch.object(runner.external,'load_optional_feeds',return_value=(vendor,{})), \
             patch.object(base,'get_json',side_effect=[[],[self.pair(liquidity=90000,fdv=1500000)]]), \
             patch.object(runner.time,'sleep'):
            c8,c5,report,*_=runner.discover()
        self.assertFalse(c8); self.assertFalse(c5)
        self.assertFalse(report['ratio_only_added'])

    def test_complete_cycle_persists_isolated_accounts_and_history(self):
        original=os.getcwd()
        with tempfile.TemporaryDirectory() as temp:
            os.chdir(temp)
            try:
                responses=[[dict(chainId='solana',tokenAddress='abc')],
                           [self.pair(liquidity=90000,fdv=1500000)]]
                with patch.object(runner.external,'load_optional_feeds',return_value=([],{})), \
                     patch.object(base,'get_json',side_effect=responses),patch.object(runner.time,'sleep'), \
                     patch('builtins.print'):
                    runner.main()
                report=json.loads(runner.REPORT_FILE.read_text())
                self.assertEqual(report['accepted_for_watch'],0)
                self.assertEqual(report['accepted_5pct'],1)
                accounts={k:json.loads(p.read_text()) for k,p in runner.EXPERIMENT_FILES.items()}
                self.assertEqual(accounts['control_8pct']['watchlist'],{})
                self.assertEqual(len(accounts['challenger_5pct']['watchlist']),1)
                self.assertEqual(accounts['challenger_5pct']['cash'],10000)
                self.assertFalse(accounts['challenger_5pct']['trades'])
                self.assertEqual(len(list(Path('链上Meme/诊断历史').glob('*.jsonl'))),1)
            finally:
                os.chdir(original)

    def test_missing_price_never_reported_as_current_equity(self):
        a=launch.new_account(); a['positions']['solana:x']=dict(quantity=1,entry_price=100,invested_usdt=100)
        summary=runner.account_summary(a,{},0,base.time.time())
        self.assertIsNone(summary['equity']); self.assertIsNone(summary['net_pnl'])

    def test_baseline_thresholds_unchanged(self):
        for liquidity in (249999,250000,500000):
            for hour in (-6,-5,0,25,26):
                p=self.pair(liquidity=liquidity,volume24=1000000,buys=700,sells=600,age_h=24,h1=hour)
                self.assertEqual(base.score_pair(p) is not None,g.evaluate(g.snapshot(p,base.time.time()),'base')['passed'])

if __name__=='__main__': unittest.main()
