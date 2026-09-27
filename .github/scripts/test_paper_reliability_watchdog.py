import os
import unittest

os.environ.setdefault("GITHUB_TOKEN", "test-token")
os.environ.setdefault("GITHUB_REPOSITORY", "owner/repo")

from paper_reliability_watchdog import recovery_decision


def state(component, age, *, active=False, stale=None):
    return {
        "component": component,
        "age_minutes": age,
        "active": active,
        "stale": (age > 55) if stale is None else stale,
    }


class WatchdogRecoveryDecisionTests(unittest.TestCase):
    def test_active_core_workflow_blocks_new_recovery(self):
        decision, selected = recovery_decision([
            state("spot", 90),
            state("futures", 80, active=True, stale=False),
        ])
        self.assertEqual(decision, "active")
        self.assertIsNone(selected)

    def test_selects_only_oldest_stale_component(self):
        decision, selected = recovery_decision([
            state("spot", 70),
            state("futures", 120),
            state("meme", 20, stale=False),
        ])
        self.assertEqual(decision, "recover")
        self.assertEqual(selected["component"], "futures")

    def test_fresh_components_do_not_dispatch(self):
        decision, selected = recovery_decision([
            state("spot", 10, stale=False),
            state("futures", 55, stale=False),
        ])
        self.assertEqual(decision, "fresh")
        self.assertIsNone(selected)


if __name__ == "__main__":
    unittest.main()
