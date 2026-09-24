import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest import mock

P = Path(__file__).parents[1] / 'bot.py'
S = importlib.util.spec_from_file_location('quant_execution_timing_bot', P)
bot = importlib.util.module_from_spec(S)
S.loader.exec_module(bot)


class ExecutionTimingTests(unittest.TestCase):
    def setUp(self):
        self.c = bot.load_json(P.parent / 'config.json')
        self.c['symbols'] = ['BTCUSDT', 'ETHUSDT']
        self.a = bot.new_account(self.c)
        self.a['cash'] = 9800
        self.b = [[i * 1000, 100, 101, 99, 100, 100, i * 1000 + 999]
                  for i in range(260)]
        for symbol in self.c['symbols']:
            self.a['positions'][symbol] = dict(qty=1, entry=100, cost=100,
                stop=90, target=200, highest=100)
            self.a['last_bar'][symbol] = self.b[-1][0]
        self.clock = 1800000000.0

    def gate(self, price=80, age=0):
        return dict(available=True, reference_mid=price,
                    price_epoch_ms=int((self.clock-age)*1000))

    def run_paper(self, assess, settle):
        with mock.patch.object(bot, 'load_json', return_value=self.a), \
             mock.patch.object(bot, 'fetch_history', return_value=self.b), \
             mock.patch.object(bot, 'assess_okx_orderbook', side_effect=assess), \
             mock.patch.object(bot, 'settle_execution_ab', side_effect=settle), \
             mock.patch.object(bot.time, 'time', side_effect=lambda: self.clock), \
             mock.patch.object(bot, 'save_json') as save, \
             contextlib.redirect_stdout(io.StringIO()):
            bot.paper(self.c)
        return save.call_args_list[-1].args[1]

    def test_all_position_checks_precede_slow_shadow_labels(self):
        positions_at_label = []
        def settle(*args):
            positions_at_label.append(list(self.a['positions']))
            self.clock += 60
        self.run_paper(lambda *args: self.gate(), settle)
        self.assertEqual(positions_at_label, [[], []])
        self.assertEqual(len(self.a['trades']), 2)

    def test_quote_expired_during_batch_fetch_is_refreshed(self):
        calls = []
        def assess(symbol, *args):
            calls.append(symbol)
            if len(calls) == 2:
                self.clock += 40
            return self.gate(price=80 if len(calls) == 1 else 100)
        self.run_paper(assess, lambda *args: None)
        self.assertEqual(calls, ['BTC', 'ETH', 'BTC'])
        self.assertEqual(self.a['trades'], [])
        self.assertEqual(len(self.a['positions']), 2)

    def test_stale_refresh_cannot_book_a_historical_stop(self):
        report = self.run_paper(lambda *args: self.gate(age=40), lambda *args: None)
        self.assertEqual(self.a['trades'], [])
        self.assertTrue(all(x['action'] == 'SKIP_RISK_DATA' for x in report['decisions']))

    def test_research_error_does_not_lose_completed_risk_checks(self):
        def broken(*args):
            raise RuntimeError('shadow source unavailable')
        report = self.run_paper(lambda *args: self.gate(), broken)
        self.assertEqual(len(self.a['trades']), 2)
        self.assertEqual(len(report['research_errors']), 2)

    def test_snapshot_time_precedes_slow_research_completion(self):
        initial_clock = self.clock
        def settle(*args):
            self.clock += 60
        report = self.run_paper(lambda *args: self.gate(), settle)
        self.assertEqual(self.a['equity_curve'][-1][0], int(initial_clock*1000))
        self.assertEqual(bot.datetime.fromisoformat(report['time']).timestamp(), initial_clock)

    def test_invalid_timestamps_and_prices_are_not_executable(self):
        with mock.patch.object(bot.time, 'time', return_value=self.clock):
            for age in (31, -3):
                self.assertFalse(bot.execution_quote_usable(self.gate(age=age)))
            for value in (0, float('nan'), float('inf')):
                self.assertFalse(bot.execution_quote_usable(self.gate(price=value)))
            self.assertFalse(bot.execution_quote_usable({'available': True, 'reference_mid': 80}))
            self.assertTrue(bot.execution_quote_usable(self.gate()))


if __name__ == '__main__':
    unittest.main()
