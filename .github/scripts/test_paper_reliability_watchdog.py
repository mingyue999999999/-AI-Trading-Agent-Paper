import os
import unittest
from unittest.mock import patch

os.environ.setdefault("GITHUB_TOKEN", "test-token")
os.environ.setdefault("GITHUB_REPOSITORY", "owner/repo")

import paper_reliability_watchdog as watchdog


def state(component, age, *, active=False, stale=None):
    return {
        "component": component,
        "workflow": f"{component}.yml",
        "age_minutes": age,
        "active": active,
        "stale": (age > 55) if stale is None else stale,
        "freshness_source": "last_successful_run",
    }


class WatchdogRecoveryDecisionTests(unittest.TestCase):
    def test_active_core_workflow_blocks_new_recovery(self):
        decision, selected = watchdog.recovery_decision([
            state("spot", 90),
            state("futures", 80, active=True, stale=False),
        ])
        self.assertEqual(decision, "active")
        self.assertIsNone(selected)

    def test_selects_only_oldest_stale_component(self):
        decision, selected = watchdog.recovery_decision([
            state("spot", 70),
            state("futures", 120),
            state("meme", 20, stale=False),
        ])
        self.assertEqual(decision, "recover")
        self.assertEqual(selected["component"], "futures")

    def test_fresh_components_do_not_dispatch(self):
        decision, selected = watchdog.recovery_decision([
            state("spot", 10, stale=False),
            state("futures", 55, stale=False),
        ])
        self.assertEqual(decision, "fresh")
        self.assertIsNone(selected)

    def test_main_dispatches_at_most_one_stale_component(self):
        initial = [state("spot", 90), state("futures", 80), state("meme", 70)]
        after = [state("spot", 1, stale=False), state("futures", 80), state("meme", 70)]
        with patch.object(watchdog, "all_states", side_effect=[initial, after]):
            with patch.object(watchdog, "dispatch_and_wait") as dispatch:
                watchdog.main()
        dispatch.assert_called_once_with("spot", "spot.yml")

    def test_main_defers_if_any_core_workflow_is_active(self):
        current = [state("spot", 90), state("futures", 80, active=True, stale=False)]
        with patch.object(watchdog, "all_states", return_value=current):
            with patch.object(watchdog, "dispatch_and_wait") as dispatch:
                watchdog.main()
        dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
