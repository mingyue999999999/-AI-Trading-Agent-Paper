import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("polybot", ROOT / "bot.py")
bot = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bot
SPEC.loader.exec_module(bot)


class BotTests(unittest.TestCase):
    def test_unreadable_or_invalid_existing_account_never_resets(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'a.json'
            for payload in ('{broken', '{}', 'null', '{"cash_usdt":10000}'):
                p.write_text(payload)
                with self.assertRaises(RuntimeError): bot.load_account(p,10000)
                self.assertEqual(p.read_text(),payload)

    def test_safety_lock(self):
        source = (ROOT / "bot.py").read_text(encoding="utf-8")
        forbidden = ["private_key", "post_order", "create_order", "signature_type"]
        self.assertTrue(all(x not in source.lower() for x in forbidden))

    def test_dynamic_size_is_capped(self):
        cfg = json.loads((ROOT / "config.json").read_text())
        small = bot.size_order(10000, .45, .52, .5, 100000, cfg)
        large = bot.size_order(10000, .45, .70, .9, 100000, cfg)
        self.assertGreater(large, small)
        self.assertLessEqual(large, 10000 * cfg["max_trade_fraction"])

    def test_wallet_anomaly_flags(self):
        now = 2_000_000_000
        candidate = {"address":"0x"+"1"*40,"name":"x","pnl":900,"volume":1000,"appearances":1}
        trades = [{"timestamp":now-i*100,"conditionId":"only","asset":"a","outcome":"Yes","side":"BUY","size":10,"price":.98,"title":"x"} for i in range(20)]
        cfg = json.loads((ROOT / "config.json").read_text())
        score = bot.wallet_quality(candidate, trades, now, cfg)
        self.assertIn("one_event_dominance", score.flags)
        self.assertIn("late_odds_specialist", score.flags)
        self.assertIn("extreme_return_needs_review", score.flags)

    def test_atomic_account_and_exit(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"a.json"; a = bot.load_account(p,10000); bot.atomic_json(p,a)
            self.assertEqual(json.loads(p.read_text())["mode"], "PAPER_ONLY")


    def test_uncalibrated_wallet_consensus_is_shadow_only(self):
        cfg = json.loads((ROOT / "config.json").read_text())
        self.assertFalse(cfg["wallet_consensus_execution_enabled"])
        with tempfile.TemporaryDirectory() as d:
            account = bot.load_account(Path(d) / "missing.json", 10000)
        signal = {
            "type":"wallet_consensus", "condition_id":"c", "asset":"a", "title":"x",
            "outcome":"Yes", "category":"OTHER", "price":0.40,
            "fair_probability":0.60, "wallet_count":8, "liquidity":100000,
            "news":{"score":0.8}
        }
        self.assertEqual(bot.execute_paper(account, [signal], cfg, 2_000_000_000), [])
        self.assertEqual(account["positions"], [])

    def test_existing_consensus_positions_are_retired_without_history_reset(self):
        cfg = json.loads((ROOT / "config.json").read_text())
        with tempfile.TemporaryDirectory() as d:
            account = bot.load_account(Path(d) / "missing.json", 10000)
        account["cash_usdt"] = 9900
        account["positions"] = [{
            "id":"p", "signal_type":"wallet_consensus", "strategy_version":"consensus_v2",
            "asset":"a", "condition_id":"c", "title":"x", "outcome":"Yes",
            "category":"OTHER", "entry_price":0.50, "mark_price":0.50,
            "fair_probability":0.60, "shares":200, "cost_usdt":100,
            "opened_ts":1_999_999_000, "confidence":0.8, "wallet_count":5,
            "net_edge_at_entry":0.05
        }]
        bot.mark_and_exit(account, {"a":0.49}, cfg, 2_000_000_000)
        self.assertEqual(account["positions"], [])
        self.assertEqual(len(account["closed_trades"]), 1)
        self.assertEqual(account["closed_trades"][0]["reason"], "wallet_consensus_shadow_only")


if __name__ == "__main__": unittest.main()
