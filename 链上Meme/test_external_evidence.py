import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import 外部信号复核 as e
import new_launch_shadow as launch

class Tests(unittest.TestCase):
    def row(self, **kw):
        x = {
            "chain": "solana", "address": "abc", "smart_buyers": 5,
            "smart_holders": 3, "wallet_history_days": 60,
            "wallet_sample_tokens": 30, "wallet_cross_cycle": True,
            "can_sell": True, "organic_volume_score": .9,
        }
        x.update(kw)
        return x

    def test_cross_source_wallet_evidence_is_bounded(self):
        rows = [e.normalize(self.row(), "gmgn"), e.normalize(self.row(), "fomo")]
        result = e.evaluate(e.merge(rows)["solana:abc"])
        self.assertEqual(result["sources"], ["fomo", "gmgn"])
        self.assertEqual(result["qualified_wallet_sources"], ["fomo", "gmgn"])
        self.assertLessEqual(result["bonus"], 12)
        self.assertFalse(result["blockers"])

    def test_weak_wallet_history_gets_no_wallet_bonus(self):
        row = e.normalize(self.row(wallet_history_days=3, wallet_sample_tokens=2), "debot")
        result = e.evaluate(e.merge([row])["solana:abc"])
        self.assertEqual(result["qualified_wallet_sources"], [])
        self.assertEqual(result["bonus"], 2)

    def test_paid_promotion_and_sell_failure_block(self):
        rows = [
            e.normalize(self.row(paid_promotion=True), "debot"),
            e.normalize(self.row(can_sell=False), "gmgn"),
        ]
        result = e.evaluate(e.merge(rows)["solana:abc"])
        self.assertIn("PAID_PROMOTION", result["blockers"])
        self.assertIn("SELLABILITY_FAILED", result["blockers"])
        self.assertEqual(result["bonus"], 0)

    def test_bad_feed_url_is_not_fetched(self):
        called = []
        records, status = e.load_optional_feeds(
            lambda url: called.append(url),
            {"MEME_GMGN_FEED_URL": "http://gmgn.ai/feed"},
        )
        self.assertFalse(called)
        self.assertFalse(records)
        self.assertEqual(status["gmgn"], "REJECTED_URL")

    def test_vendor_signal_cannot_bypass_independent_gate(self):
        unsafe = {"price": 1, "age_h": 1, "liquidity": 1000, "volume24": 10,
                  "fdv": 1000000, "liq_fdv": .001, "buys": 500, "sells": 100,
                  "h1": 5, "h24": 10}
        self.assertIsNone(launch.score_launch(unsafe))

if __name__ == "__main__":
    unittest.main()
