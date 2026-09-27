import unittest
from datetime import datetime, timezone, timedelta

from hippo_memory import salience

WEIGHTS = {"open_loop": 2.0, "recency": 1.5, "recurrence": 1.0, "weight": 1.0, "pin": 5.0}
HALFLIFE = 7.0
NOW = datetime(2026, 6, 8, tzinfo=timezone.utc)


INTEREST_BASELINE = 3.5
INTEREST_HALFLIFE = 30.0


def mem(**over):
    base = {
        "kind": "episodic",
        "status": "open",
        "recurrence": 1,
        "weight": 0.0,
        "pinned": 0,
        "last_touched_at": NOW.isoformat(),
    }
    base.update(over)
    return base


class SalienceTest(unittest.TestCase):
    def test_recency_halves_each_halflife(self):
        old = (NOW - timedelta(days=7)).isoformat()
        self.assertAlmostEqual(
            salience.recency_factor(old, NOW, HALFLIFE), 0.5, places=6
        )

    def test_fresh_open_loop_outranks_stale_closed(self):
        fresh_open = salience.score(mem(), NOW, WEIGHTS, HALFLIFE)
        stale_closed = salience.score(
            mem(status="closed", last_touched_at=(NOW - timedelta(days=30)).isoformat()),
            NOW, WEIGHTS, HALFLIFE,
        )
        self.assertGreater(fresh_open, stale_closed)

    def test_pin_dominates(self):
        pinned_old = salience.score(
            mem(pinned=1, last_touched_at=(NOW - timedelta(days=60)).isoformat()),
            NOW, WEIGHTS, HALFLIFE,
        )
        self.assertGreater(pinned_old, WEIGHTS["pin"])

    def test_interest_outranks_fresh_execution_without_loop_or_recurrence(self):
        # A single-mention interest (open=irrelevant, recurrence=1, weight=0) must
        # outrank a fresh open execution item that has an open loop working for it.
        interest = salience.score(
            mem(kind="interest", status="closed", weight=0.0),
            NOW, WEIGHTS, HALFLIFE, INTEREST_BASELINE, INTEREST_HALFLIFE,
        )
        fresh_execution = salience.score(
            mem(kind="episodic", status="open", recurrence=2),
            NOW, WEIGHTS, HALFLIFE, INTEREST_BASELINE, INTEREST_HALFLIFE,
        )
        self.assertGreater(interest, fresh_execution)

    def test_interest_ignores_open_loop_and_recurrence(self):
        a = salience.score(
            mem(kind="interest", status="open", recurrence=5),
            NOW, WEIGHTS, HALFLIFE, INTEREST_BASELINE, INTEREST_HALFLIFE,
        )
        b = salience.score(
            mem(kind="interest", status="closed", recurrence=1),
            NOW, WEIGHTS, HALFLIFE, INTEREST_BASELINE, INTEREST_HALFLIFE,
        )
        self.assertEqual(a, b)

    def test_interest_decays_slower_than_execution(self):
        old = (NOW - timedelta(days=14)).isoformat()
        interest_keep = salience.recency_factor(old, NOW, INTEREST_HALFLIFE)
        execution_keep = salience.recency_factor(old, NOW, HALFLIFE)
        self.assertGreater(interest_keep, execution_keep)


LESSON_BASELINE = 4.5
LESSON_HALFLIFE = 14.0


def _score(m):
    return salience.score(
        m, NOW, WEIGHTS, HALFLIFE,
        INTEREST_BASELINE, INTEREST_HALFLIFE,
        LESSON_BASELINE, LESSON_HALFLIFE,
    )


class LessonSalienceTest(unittest.TestCase):
    def test_fresh_lesson_outranks_fresh_interest(self):
        # The 2026-07-11 failure mode: the night's sharpest correction must not
        # lose to a same-day interest (the motorcycle daydream).
        lesson = _score(mem(kind="lesson"))
        interest = _score(mem(kind="interest"))
        self.assertGreater(lesson, interest)

    def test_lesson_holds_through_one_half_life(self):
        # Daily-use persistence (half-life retuned 45 -> 14): at two weeks a
        # lesson still scores above the fresh-interest mark instead of fading
        # on the 7-day operational curve.
        two_weeks = (NOW - timedelta(days=14)).isoformat()
        aged = _score(mem(kind="lesson", last_touched_at=two_weeks))
        fresh_interest = _score(mem(kind="interest"))
        self.assertGreater(aged, fresh_interest)

    def test_lesson_ignores_open_loop_and_recurrence(self):
        a = _score(mem(kind="lesson", status="open", recurrence=5))
        b = _score(mem(kind="lesson", status="closed", recurrence=1))
        self.assertEqual(a, b)

    def test_charge_weight_raises_lesson_score(self):
        stamped = _score(mem(kind="lesson", weight=1.5))
        flat = _score(mem(kind="lesson", weight=0.0))
        self.assertAlmostEqual(stamped - flat, WEIGHTS["weight"] * 1.5, places=6)


if __name__ == "__main__":
    unittest.main()


class HandoffSalienceTest(unittest.TestCase):
    """Handoff notes never enter the ranking: constant zero
    regardless of pin, weight, recency, or status."""

    def test_handoff_scores_zero(self):
        m = mem(kind="handoff", weight=3.0, pinned=1, recurrence=9)
        self.assertEqual(
            salience.score(m, NOW, WEIGHTS, HALFLIFE, INTEREST_BASELINE,
                           INTEREST_HALFLIFE, 4.5, 14.0),
            0.0,
        )

    def test_handoff_zero_when_stale_and_closed(self):
        m = mem(kind="handoff", status="closed",
                last_touched_at=(NOW - timedelta(days=90)).isoformat())
        self.assertEqual(salience.score(m, NOW, WEIGHTS, HALFLIFE), 0.0)
