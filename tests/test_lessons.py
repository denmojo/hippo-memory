import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from hippo_memory import config, dream, lessons, store

NOW = datetime(2026, 7, 12, tzinfo=timezone.utc)


class StubEmbedder:
    def __init__(self, table, default=(0.0, 0.0, 1.0)):
        self.table = table
        self.default = list(default)

    def embed(self, texts):
        return [list(self.table.get(t, self.default)) for t in texts]


def _backdate(conn, mid, days, field="last_touched_at"):
    conn.execute(
        f"UPDATE memories SET {field} = ? WHERE id = ?",
        ((NOW - timedelta(days=days)).isoformat(), mid),
    )
    conn.commit()


class RearmTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)
        self.mid = store.upsert_memory(self.conn, "lesson", "a scar",
                                       dedup_key="s", weight=1.5)

    def tearDown(self):
        self.conn.close()

    def test_rearm_bumps_weight_recurrence_and_recency(self):
        before = store.get_memory(self.conn, self.mid)
        w = lessons.rearm(self.conn, self.mid)
        row = store.get_memory(self.conn, self.mid)
        self.assertEqual(w, 1.5 + config.LESSON_REARM_WEIGHT)
        self.assertEqual(row["weight"], w)
        self.assertEqual(row["recurrence"], before["recurrence"] + 1)
        self.assertGreaterEqual(row["last_touched_at"], before["last_touched_at"])

    def test_rearm_ceiling_holds(self):
        for _ in range(20):
            lessons.rearm(self.conn, self.mid)
        row = store.get_memory(self.conn, self.mid)
        self.assertEqual(row["weight"], config.LESSON_WEIGHT_CEILING)
        self.assertLess(config.LESSON_WEIGHT_CEILING * config.WEIGHTS["weight"],
                        config.WEIGHTS["pin"])

    def test_rearm_reopens_retired_lesson(self):
        store.close_memory(self.conn, self.mid)
        lessons.rearm(self.conn, self.mid)
        self.assertEqual(store.get_memory(self.conn, self.mid)["status"], "open")


class FoldTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)
        self.open_id = store.upsert_memory(
            self.conn, "lesson", "no unbidden writes", body="ask first",
            dedup_key="unbidden")
        self.closed_id = store.upsert_memory(
            self.conn, "lesson", "old closed scar", body="", dedup_key="old")
        store.close_memory(self.conn, self.closed_id)

    def tearDown(self):
        self.conn.close()

    def _emb(self, mapping):
        table = {"no unbidden writes\nask first": [1.0, 0.0, 0.0],
                 "old closed scar\n": [0.0, 1.0, 0.0]}
        table.update(mapping)
        return StubEmbedder(table)

    def test_near_duplicate_lesson_folds_into_open_lesson(self):
        dup = {"kind": "lesson", "title": "never write unprompted", "body": ""}
        emb = self._emb({"never write unprompted\n": [0.98, 0.1, 0.0]})
        kept, folds = lessons.fold_reoffenses(self.conn, [dup], emb)
        self.assertEqual(kept, [])
        self.assertEqual(len(folds), 1)
        self.assertEqual(folds[0]["id"], self.open_id)

    def test_closed_lessons_and_non_lessons_do_not_fold(self):
        relapse = {"kind": "lesson", "title": "matches the closed one", "body": ""}
        interest = {"kind": "interest", "title": "matches the open one", "body": ""}
        emb = self._emb({
            "matches the closed one\n": [0.0, 1.0, 0.0],   # = closed lesson vec
            "matches the open one\n": [1.0, 0.0, 0.0],     # = open lesson vec
        })
        kept, folds = lessons.fold_reoffenses(
            self.conn, [relapse, interest], emb)
        # The closed lesson is not in the fold corpus; the interest is not a
        # lesson add. Both stay in the plan.
        self.assertEqual(kept, [relapse, interest])
        self.assertEqual(folds, [])

    def test_distinct_lesson_kept(self):
        fresh = {"kind": "lesson", "title": "cite primary sources", "body": ""}
        emb = self._emb({"cite primary sources\n": [0.0, 0.0, 1.0]})
        kept, folds = lessons.fold_reoffenses(self.conn, [fresh], emb)
        self.assertEqual(kept, [fresh])
        self.assertEqual(folds, [])


class RetirementTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_clean_conduct_retires_and_recent_touch_spares(self):
        old = store.upsert_memory(self.conn, "lesson", "old clean scar",
                                  body="the rule", dedup_key="oc")
        fresh = store.upsert_memory(self.conn, "lesson", "fresh scar",
                                    dedup_key="fr")
        _backdate(self.conn, old, config.LESSON_RETIREMENT_DAYS + 5)
        retired = lessons.retire_clean(self.conn, now=NOW)
        self.assertEqual([r["id"] for r in retired], [old])
        row = store.get_memory(self.conn, old)
        self.assertEqual(row["status"], "closed")
        self.assertIn("RETIRED", row["body"])
        self.assertIn("the rule", row["body"])      # original body preserved
        self.assertEqual(store.get_memory(self.conn, fresh)["status"], "open")

    def test_graduation_close_note_appends_exit_story(self):
        mid = store.upsert_memory(self.conn, "lesson", "kanban first",
                                  body="record before working", dedup_key="k")
        store.close_memory(self.conn, mid,
                           note="GRADUATED: enforced by guard-kanban-cli.sh")
        row = store.get_memory(self.conn, mid)
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["body"],
                         "record before working\n"
                         "GRADUATED: enforced by guard-kanban-cli.sh")


class RunDreamLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def _run(self, plan, embedder=None):
        raw = json.dumps(plan)
        with tempfile.TemporaryDirectory() as d:
            jp = os.path.join(d, "journal.md")
            summary = dream.run_dream(
                self.conn, "material", invoke_fn=lambda p: raw,
                date="2026-07-12", journal_path=jp, embedder=embedder,
            )
            journal_text = open(jp).read() if os.path.exists(jp) else ""
        return summary, journal_text

    def test_dedup_key_reoffense_rearms_in_one_run(self):
        mid = store.upsert_memory(self.conn, "lesson", "no unbidden writes",
                                  dedup_key="unbidden")
        plan = {"adds": [{"kind": "lesson", "title": "did it again",
                          "dedup_key": "unbidden"}],
                "journal": ["promoted: none"]}
        # Explicit orthogonal vectors keep the semantic fold out of the way
        # (and the test never lazy-loads the ONNX model), so this
        # exercises the dedup_key path in apply_plan alone.
        emb = StubEmbedder({"did it again\n": [0.0, 1.0, 0.0],
                            "no unbidden writes\n": [1.0, 0.0, 0.0]})
        summary, journal_text = self._run(plan, embedder=emb)
        row = store.get_memory(self.conn, mid)
        self.assertEqual(summary["rearmed"], 1)
        self.assertEqual(summary["added"], 0)
        self.assertEqual(row["recurrence"], 2)
        self.assertEqual(row["weight"], config.LESSON_REARM_WEIGHT)
        # Only one lesson row exists: no sibling was filed.
        n = self.conn.execute(
            "SELECT count(*) FROM memories WHERE kind='lesson'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_semantic_fold_rearms_without_dedup_key(self):
        mid = store.upsert_memory(self.conn, "lesson", "no unbidden writes",
                                  body="ask first", dedup_key="unbidden")
        dup = {"kind": "lesson", "title": "never write unprompted", "body": ""}
        emb = StubEmbedder({
            "no unbidden writes\nask first": [1.0, 0.0, 0.0],
            "never write unprompted\n": [0.97, 0.1, 0.0],
        })
        plan = {"adds": [dup], "journal": []}
        summary, journal_text = self._run(plan, embedder=emb)
        row = store.get_memory(self.conn, mid)
        self.assertEqual(summary["folded"], 1)
        self.assertEqual(summary["rearmed"], 1)
        self.assertEqual(row["recurrence"], 2)
        self.assertIn("re-offense", journal_text)

    def test_retirement_journals_on_apply(self):
        old = store.upsert_memory(self.conn, "lesson", "long clean",
                                  dedup_key="lc")
        self.conn.execute(
            "UPDATE memories SET last_touched_at = ? WHERE id = ?",
            ((datetime.now(timezone.utc)
              - timedelta(days=config.LESSON_RETIREMENT_DAYS + 1)).isoformat(),
             old),
        )
        self.conn.commit()
        plan = {"adds": [], "journal": ["quiet night"]}
        summary, journal_text = self._run(plan)
        self.assertEqual(summary["retired"], 1)
        self.assertIn("retired: lesson", journal_text)
        self.assertEqual(store.get_memory(self.conn, old)["status"], "closed")


if __name__ == "__main__":
    unittest.main()
