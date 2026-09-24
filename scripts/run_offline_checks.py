"""Cross-bot offline regression entry point. Never runs a trading command.

Tests execute in separate processes to isolate module globals and environment.
All repository JSON files must remain byte-identical, including account state.
"""
import hashlib
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def snapshot():
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in ROOT.rglob('*.json') if '.git' not in p.parts}


CHECKS = [
    ('queued state writers and PR isolation', ROOT,
     ['-m', 'unittest', 'test_workflow_state_checkout', '-q']),
    ('runtime, execution and account loading', ROOT,
     ['-m', 'unittest', 'test_paper_runtime', 'test_execution_quality', 'test_account_fail_closed', '-q']),
    ('spot static safety', ROOT, ['现货/test_static.py']),
    ('futures static safety', ROOT, ['合约/test_v5_static.py']),
    ('basis static safety', ROOT, ['合约/test_basis_static.py']),
    ('schedule configuration', ROOT, ['.github/scripts/check_schedule_config.py']),
    ('quant', ROOT/'量化', ['-m', 'unittest', 'discover', '-s', 'tests', '-q']),
    ('meme and independent challengers', ROOT/'链上Meme', ['-m', 'unittest', 'discover', '-p', 'test_*.py', '-q']),
    ('polymarket', ROOT/'Polymarket套利机器人', ['-m', 'unittest', 'discover', '-s', 'tests', '-q']),
    ('strategy lab and polymarket 15m', ROOT/'研究', ['-m', 'unittest', 'discover', '-p', 'test_*.py', '-q']),
    ('shadow research', ROOT, ['-m', 'unittest', 'discover', '-s', 'shadow_research/tests', '-q']),
]


def main():
    before = snapshot()
    failures = []
    for label, directory, args in CHECKS:
        print('\nCHECK: '+label, flush=True)
        try:
            result = subprocess.run([sys.executable, *args], cwd=directory, timeout=120, check=False)
            if result.returncode:
                failures.append(label)
        except subprocess.TimeoutExpired:
            failures.append(label+' (timeout)')
    after = snapshot()
    changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    if changed:
        failures.append('JSON/state mutation: '+', '.join(changed))
    if failures:
        raise SystemExit('FAILED: '+'; '.join(failures))
    print('\nAll offline suites passed; repository JSON/state unchanged.', flush=True)


if __name__ == '__main__':
    main()
