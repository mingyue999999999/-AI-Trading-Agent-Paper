"""Protect account writers from queued runs checking out an old event commit."""
import json
import re
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
WRITERS = (
    'spot-paper.yml', 'futures-paper.yml', 'meme-paper.yml', 'quant-paper.yml',
    'polymarket-paper.yml', 'polymarket-shadow.yml', 'shadow-research.yml',
    'strategy-research.yml',
)
FRESH_REF = "ref: ${{ github.event_name != 'pull_request' && github.ref == 'refs/heads/main' && 'main' || github.sha }}"


class StateCheckoutTests(unittest.TestCase):
    def test_all_writers_resolve_main_after_queue_but_pin_pr_sha(self):
        for name in WRITERS:
            with self.subTest(workflow=name):
                text = (ROOT/'.github/workflows'/name).read_text(encoding='utf-8')
                checkout_match = re.search(r'uses:\s*actions/checkout@v(\d+)(.*?)(?=\n      - |\Z)', text, re.S)
                self.assertIsNotNone(checkout_match)
                self.assertGreaterEqual(int(checkout_match.group(1)), 5)
                checkout = checkout_match.group(2)
                self.assertIn('fetch-depth: 0', checkout)
                self.assertIn(FRESH_REF, checkout)

    def test_official_actions_use_node24_compatible_majors(self):
        for workflow in (ROOT/'.github/workflows').glob('*.yml'):
            text = workflow.read_text(encoding='utf-8')
            for action, minimum in (('checkout', 5), ('setup-python', 6), ('upload-artifact', 6)):
                for major in re.findall(rf'uses:\s*actions/{action}@v(\d+)', text):
                    with self.subTest(workflow=workflow.name, action=action):
                        self.assertGreaterEqual(int(major), minimum)

    def test_state_writers_remain_serialized_and_main_only(self):
        for name in WRITERS:
            with self.subTest(workflow=name):
                text = (ROOT/'.github/workflows'/name).read_text(encoding='utf-8')
                self.assertIn('concurrency:', text)
                self.assertIn('cancel-in-progress: false', text)
                self.assertIn("if: github.ref == 'refs/heads/main'", text.replace('if: always() && ', 'if: '))
                self.assertNotIn('git push --force', text)
                self.assertNotIn('git push -f ', text)
                self.assertNotIn('rebase -X ours', text)

    def test_read_only_regression_keeps_default_pr_checkout(self):
        text = (ROOT/'.github/workflows/cross-bot-regression.yml').read_text(encoding='utf-8')
        self.assertNotIn(FRESH_REF, text)
        self.assertIn('contents: read', text)

    def test_queued_event_sha_is_stale_but_main_preserves_previous_writer(self):
        # Local Git only: no real accounts, remote calls, or trading commands.
        # A queued event pins A; the preceding serialized writer advances main
        # to B. The next writer must read B, then append C, never start from A.
        with tempfile.TemporaryDirectory(prefix='paper-queue-test-') as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.check_output(['git', '-C', tmp, *args], stderr=subprocess.STDOUT, text=True).strip()
            git('init', '-b', 'main')
            git('config', 'user.name', 'Offline test')
            git('config', 'user.email', 'offline@example.invalid')
            account = root/'fixture.json'
            def save(trades):
                account.write_text(json.dumps({'initial': 10000, 'trades': trades}), encoding='utf-8')
                git('add', 'fixture.json')
                git('commit', '-m', 'fixture state')
            save(['A'])
            event_sha = git('rev-parse', 'HEAD')
            save(['A', 'B'])
            git('checkout', '--detach', event_sha)
            self.assertEqual(json.loads(account.read_text())['trades'], ['A'])
            git('checkout', 'main')
            state = json.loads(account.read_text())
            self.assertEqual(state['trades'], ['A', 'B'])
            save(state['trades'] + ['C'])
            self.assertEqual(json.loads(account.read_text()), {'initial': 10000, 'trades': ['A', 'B', 'C']})


if __name__ == '__main__':
    unittest.main()
