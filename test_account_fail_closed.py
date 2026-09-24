"""Offline loader regression tests; never execute module-level trading code."""
import ast
import copy
import json
import math
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
CASES = [('现货/现货机器人.py', 'new_account', 'load_account', 'STATE_FILE'),
         ('合约/合约机器人.py', 'new_futures_account', 'load_futures_account', 'FUTURES_STATE_FILE'),
         ('合约/资金费率基差实验.py', 'new_account', 'load_account', 'STATE_FILE')]


class AccountLoaderTests(unittest.TestCase):
    def test_polymarket_state_writer_is_main_only(self):
        workflow = (ROOT/'.github/workflows/polymarket-paper.yml').read_text()
        self.assertIn('  push:\n    branches: [main]\n', workflow)
        self.assertIn("  paper:\n    if: github.ref == 'refs/heads/main'\n", workflow)
        self.assertIn("      - name: Persist paper state and reports\n        if: github.ref == 'refs/heads/main'\n", workflow)

    def loader(self, case, directory):
        file, new_name, load_name, state_name = case
        tree = ast.parse((ROOT/file).read_text(encoding='utf-8'))
        selected = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name in (new_name, load_name)]
        env = dict(json=json, os=os, math=math, INITIAL_BALANCE=10000,
                   VERSION='test', now_utc=lambda: datetime.now(timezone.utc).isoformat())
        path = Path(directory)/'account.json'
        env[state_name] = str(path)
        exec(compile(ast.Module(body=selected, type_ignores=[]), file, 'exec'), env)
        return path, env[new_name], env[load_name]

    def test_missing_file_still_bootstraps(self):
        for case in CASES:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as d:
                path, new, load = self.loader(case, d)
                self.assertEqual(load()['cash'], 10000)
                self.assertFalse(path.exists())

    def test_corrupt_empty_partial_and_invalid_containers_fail_closed(self):
        for case in CASES:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as d:
                path, new, load = self.loader(case, d)
                valid = new()
                payloads = ['{broken', '{}', 'null', json.dumps({'cash': 10000})]
                for key in ('cash', 'trades'):
                    invalid = copy.deepcopy(valid); invalid.pop(key)
                    payloads.append(json.dumps(invalid))
                invalid = copy.deepcopy(valid); invalid['trades'] = {}
                payloads.append(json.dumps(invalid))
                if 'positions' in valid:
                    invalid = copy.deepcopy(valid); invalid['positions'] = []
                    payloads.append(json.dumps(invalid))
                for payload in payloads:
                    path.write_text(payload)
                    with self.assertRaises(RuntimeError): load()
                    self.assertEqual(path.read_text(), payload)

    def test_nonfinite_cash_is_rejected(self):
        for case in CASES:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as d:
                path, new, load = self.loader(case, d)
                for amount in (float('nan'), float('inf')):
                    a = new(); a['cash'] = amount
                    path.write_text(json.dumps(a))
                    with self.assertRaises(RuntimeError): load()

    def test_valid_financial_history_is_preserved(self):
        for case in CASES:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as d:
                path, new, load = self.loader(case, d)
                a = new(); a['cash'] = 9876.54
                a['trades'] = [{'time': '2026-09-20T00:00:00+00:00', 'side': 'BUY', 'fee': 1.23}]
                a['fees_paid'] = 1.23
                path.write_text(json.dumps(a))
                self.assertEqual(load(), a)


if __name__ == '__main__': unittest.main()
