import copy
import unittest
from unittest.mock import patch
import run_new_launch_shadow as runner


class ProfileCoverageTests(unittest.TestCase):
    def test_every_raw_profile_has_exclusive_reason_without_mutating_input(self):
        seeds=[{'chainId':'solana','tokenAddress':'a'},
               {'chainId':'solana','tokenAddress':'a'},
               {'chainId':'unsupported','tokenAddress':'b'},
               {'chainId':'solana','tokenAddress':''},None,
               {'chainId':'solana','tokenAddress':'c'}]
        before=copy.deepcopy(seeds)
        result=runner.profile_coverage(seeds,['solana:a','solana:held'])
        self.assertEqual(sum(result['decision_counts'].values()),len(seeds))
        self.assertEqual(result['supported_unique_tokens'],2)
        self.assertEqual([r['decision'] for r in result['records']],
                         ['SELECTED','DUPLICATE_PROFILE','UNSUPPORTED_CHAIN',
                          'INVALID_ADDRESS','INVALID_PROFILE','DEFER_SELECTION_BUDGET'])
        self.assertEqual(seeds,before)

    def test_held_and_old_watchlist_do_not_inflate_profile_count(self):
        seeds=[{'chainId':'solana','tokenAddress':'a'}]
        registry={'solana:old':{'chain':'solana','address':'old','sources':[],
                               'first_seen':900,'last_checked':0}}
        selected,_=runner.select_seeds(seeds,[],registry,[],1000)
        self.assertEqual(set(selected),{'solana:a','solana:old'})
        coverage=runner.profile_coverage(seeds,selected)
        self.assertEqual(coverage['raw_records'],1)
        self.assertEqual(coverage['decision_counts'],{'SELECTED':1})

    def test_diagnostic_does_not_change_scan_budget(self):
        seeds=[{'chainId':'solana','tokenAddress':x} for x in ('a','b','c')]
        with patch.object(runner,'MAX_SCAN',1):
            selected,_=runner.select_seeds(seeds,[],{},[],1000)
        self.assertEqual(len(selected),1)
        result=runner.profile_coverage(seeds,selected)
        self.assertEqual(result['decision_counts'],{'SELECTED':1,'DEFER_SELECTION_BUDGET':2})


if __name__=='__main__':unittest.main()
