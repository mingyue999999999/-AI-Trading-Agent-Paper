import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

P = Path(__file__).parents[1] / 'bot.py'
S = importlib.util.spec_from_file_location('quant_integrity_bot', P)
bot = importlib.util.module_from_spec(S)
S.loader.exec_module(bot)


class AccountIntegrityTests(unittest.TestCase):
    def test_corrupt_json_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'account.json'
            p.write_text('{broken', encoding='utf-8')
            with self.assertRaises(json.JSONDecodeError):
                bot.load_json(p)
            self.assertEqual(p.read_text(), '{broken')

    def test_empty_existing_account_is_not_reinitialized(self):
        c = bot.load_json(P.parent / 'config.json')
        for invalid in ({}, None, [], {'cash': 10000}):
            with self.subTest(invalid=invalid), \
                 mock.patch.object(bot, 'load_json', return_value=invalid), \
                 mock.patch.object(bot, 'new_account') as new, \
                 mock.patch.object(bot, 'fetch_history') as fetch, \
                 mock.patch.object(bot, 'save_json') as save:
                with self.assertRaises(ValueError):
                    bot.paper(c)
                new.assert_not_called()
                fetch.assert_not_called()
                save.assert_not_called()

    def test_valid_history_read_unchanged(self):
        c = bot.load_json(P.parent / 'config.json')
        a = bot.new_account(c)
        a['trades'].append({'side': 'BUY', 'fee': 1.0})
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'account.json'
            p.write_text(json.dumps(a), encoding='utf-8')
            self.assertEqual(bot.load_json(p), a)

    def test_missing_file_can_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(bot.load_json(Path(tmp) / 'missing.json'))


if __name__ == '__main__':
    unittest.main()
