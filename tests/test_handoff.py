"""handoff.py: notes to self written by the in-session agent.
Label validation, session resolution, sequence numbering, carries links, and
the rule-based selection the fourth working-set section draws from."""
import os
import tempfile
import unittest
from pathlib import Path

from hippo_memory import config, handoff, store


class ParseBodyTest(unittest.TestCase):
    def test_accepts_known_labels_with_continuations(self):
        body = ("State: parser done\n"
                "  cron still unverified\n"
                "Dead ends: tried launchd StartInterval; ignored\n"
                "Next: run the probe by hand\n")
        got = handoff.parse_body(body)
        self.assertEqual(got["State"], "parser done\ncron still unverified")
        self.assertEqual(got["Next"], "run the probe by hand")

    def test_rejects_unknown_label(self):
        with self.assertRaises(handoff.HandoffError) as cm:
            handoff.parse_body("State: ok\nFeelings: fine\n")
        self.assertIn("Feelings", str(cm.exception))

    def test_rejects_empty_or_unlabelled_body(self):
        with self.assertRaises(handoff.HandoffError):
            handoff.parse_body("")
        with self.assertRaises(handoff.HandoffError):
            handoff.parse_body("just some prose with no label\n")

    def test_rejects_over_cap(self):
        with self.assertRaises(handoff.HandoffError):
            handoff.parse_body("State: " + "x" * config.HANDOFF_BODY_MAX)


class SessionResolveTest(unittest.TestCase):
    def test_explicit_wins(self):
        self.assertEqual(handoff.resolve_session("abc", path="/nonexistent"), "abc")

    def test_reads_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "current-session"
            p.write_text("sid-1\n/tmp/t.jsonl\n")
            self.assertEqual(handoff.resolve_session(None, path=p), "sid-1")

    def test_refuses_without_source(self):
        with self.assertRaises(handoff.HandoffError):
            handoff.resolve_session(None, path="/nonexistent/current-session")


class StoreOpsTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)
        self.loop = store.upsert_memory(self.conn, "episodic", "router probe", dedup_key="l1")
        self.loop2 = store.upsert_memory(self.conn, "project", "income engine", dedup_key="l2")

    def tearDown(self):
        self.conn.close()

    def test_add_numbers_per_session_and_links_carries(self):
        a = handoff.add(self.conn, "first", "State: a", "s1", carries=[self.loop])
        b = handoff.add(self.conn, "second", "State: b", "s1", carries=[self.loop, self.loop2])
        c = handoff.add(self.conn, "other", "State: c", "s2")
        rows = {r["id"]: r for r in store.ranked(self.conn, kind="handoff")}
        self.assertEqual(rows[a]["dedup_key"], "handoff:s1:1")
        self.assertEqual(rows[b]["dedup_key"], "handoff:s1:2")
        self.assertEqual(rows[c]["dedup_key"], "handoff:s2:1")
        self.assertEqual(handoff.carries(self.conn, b), [self.loop, self.loop2])
        self.assertEqual(handoff.carries(self.conn, c), [])
        self.assertEqual(rows[a]["salience"], 0.0)

    def test_add_refuses_missing_loop(self):
        with self.assertRaises(handoff.HandoffError):
            handoff.add(self.conn, "x", "State: a", "s1", carries=[9999])

    def test_latest_and_list_and_close(self):
        a = handoff.add(self.conn, "first", "State: a", "s1")
        b = handoff.add(self.conn, "second", "State: b", "s1")
        self.assertEqual(handoff.latest(self.conn, "s1")["id"], b)
        self.assertEqual([r["id"] for r in handoff.list_rows(self.conn, session="s1")], [b, a])
        handoff.close(self.conn, b)
        self.assertEqual(handoff.latest(self.conn, "s1")["id"], a)
        self.assertEqual([r["id"] for r in handoff.list_rows(self.conn, session="s1")], [a])
        self.assertEqual(
            [r["id"] for r in handoff.list_rows(self.conn, session="s1", include_closed=True)],
            [b, a],
        )
        self.assertIsNone(handoff.latest(self.conn, "nope"))


class SelectTest(unittest.TestCase):
    """The rule: every open handoff carrying a still-open loop, newest first,
    plus the single newest open handoff carrying no loops, under a ceiling."""

    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)
        self.open_loop = store.upsert_memory(self.conn, "episodic", "open", dedup_key="o")
        self.closed_loop = store.upsert_memory(self.conn, "episodic", "closed", dedup_key="c")
        store.close_memory(self.conn, self.closed_loop)

    def tearDown(self):
        self.conn.close()

    def test_rule(self):
        carried = handoff.add(self.conn, "carried", "State: x", "s1", carries=[self.open_loop])
        dead = handoff.add(self.conn, "dead", "State: x", "s1", carries=[self.closed_loop])
        free_old = handoff.add(self.conn, "free old", "State: x", "s2")
        free_new = handoff.add(self.conn, "free new", "State: x", "s3")
        got = [r["id"] for r in handoff.select(self.conn, cap=5)]
        self.assertIn(carried, got)
        self.assertNotIn(dead, got)
        self.assertIn(free_new, got)
        self.assertNotIn(free_old, got)
        self.assertEqual(len(got), 2)

    def test_cap_and_newest_first(self):
        ids = [handoff.add(self.conn, f"n{i}", "State: x", f"s{i}", carries=[self.open_loop])
               for i in range(4)]
        got = [r["id"] for r in handoff.select(self.conn, cap=2)]
        self.assertEqual(got, [ids[3], ids[2]])

    def test_render_lines(self):
        h = handoff.add(self.conn, "router: parser done", "State: x", "s1",
                        carries=[self.open_loop])
        lines = handoff.render_lines(self.conn, handoff.select(self.conn, cap=5))
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith(f"- [{h}] "))
        self.assertIn("router: parser done", lines[0])
        self.assertIn(f"(carries {self.open_loop})", lines[0])
        # An uncarried note has no parenthetical.
        f = handoff.add(self.conn, "free", "State: x", "s2")
        lines = handoff.render_lines(self.conn, handoff.select(self.conn, cap=5))
        free_line = [l for l in lines if f"[{f}]" in l][0]
        self.assertNotIn("carries", free_line)


class MaterialAndLifecycleTest(unittest.TestCase):
    """The dream reads handoff rows first (material_block), and its
    deterministic housekeeping closes notes whose loops all closed, folds
    superseded notes into the newest per session, and closes uncarried notes
    older than one cycle."""

    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)
        self.loop = store.upsert_memory(self.conn, "episodic", "loop", dedup_key="l")

    def tearDown(self):
        self.conn.close()

    def test_material_block_lists_notes_with_bodies(self):
        h = handoff.add(self.conn, "router: parser done", "State: parser done\nNext: cron",
                        "s1", carries=[self.loop])
        block = handoff.material_block(self.conn, "s1")
        self.assertIn("HANDOFF NOTES", block)
        self.assertIn(f"[{h}]", block)
        self.assertIn("carries", block)
        self.assertIn("Next: cron", block)
        self.assertEqual(handoff.material_block(self.conn, "none"), "")

    def test_housekeep_closes_when_every_carried_loop_closed(self):
        h = handoff.add(self.conn, "n", "State: x", "s1", carries=[self.loop])
        self.assertEqual(handoff.housekeep(self.conn)["closed_done"], [])
        store.close_memory(self.conn, self.loop)
        res = handoff.housekeep(self.conn)
        self.assertEqual(res["closed_done"], [h])
        self.assertEqual(store.get_memory(self.conn, h)["status"], "closed")

    def test_housekeep_folds_superseded_into_newest_per_session(self):
        a = handoff.add(self.conn, "a", "State: x", "s1", carries=[self.loop])
        b = handoff.add(self.conn, "b", "State: y", "s1", carries=[self.loop])
        other = handoff.add(self.conn, "o", "State: z", "s2", carries=[self.loop])
        res = handoff.housekeep(self.conn)
        self.assertEqual(res["superseded"], [(a, b)])
        ra = store.get_memory(self.conn, a)
        self.assertEqual(ra["status"], "closed")
        self.assertEqual(ra["folded_into"], f"handoff:{b}")
        self.assertEqual(store.get_memory(self.conn, b)["status"], "open")
        self.assertEqual(store.get_memory(self.conn, other)["status"], "open")

    def test_housekeep_closes_uncarried_after_one_cycle(self):
        from datetime import datetime, timedelta, timezone
        fresh = handoff.add(self.conn, "fresh", "State: x", "s1")
        old = handoff.add(self.conn, "old", "State: x", "s2")
        past = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        self.conn.execute("UPDATE memories SET created_at=? WHERE id=?", (past, old))
        self.conn.commit()
        res = handoff.housekeep(self.conn)
        self.assertEqual(res["closed_uncarried"], [old])
        self.assertEqual(store.get_memory(self.conn, fresh)["status"], "open")

    def test_session_end_reason(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "session-ends.jsonl"
            p.write_text(json.dumps({"session_id": "s1", "reason": "clear", "had_handoff": True}) + "\n"
                         + json.dumps({"session_id": "s1", "reason": "logout", "had_handoff": True}) + "\n")
            self.assertEqual(handoff.session_end_reason("s1", path=p), "logout")
            self.assertIsNone(handoff.session_end_reason("s2", path=p))
            self.assertIsNone(handoff.session_end_reason("s1", path=Path(d) / "missing"))


if __name__ == "__main__":
    unittest.main()
