import unittest
from datetime import datetime, timezone

from hippo_memory import config, store, workingset


def _add(conn, kind, title, salience, status="open"):
    mid = store.upsert_memory(conn, kind, title)
    conn.execute(
        "UPDATE memories SET salience=?, status=? WHERE id=?",
        (salience, status, mid),
    )
    conn.commit()
    return mid


class SelectTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_open_loops_capped(self):
        for i in range(config.WS_OPEN_LOOP_CAP + 5):
            _add(self.conn, "episodic", f"loop {i}", salience=10.0 - i * 0.1)
        sel = workingset.select(self.conn)
        self.assertEqual(len(sel["loops"]), config.WS_OPEN_LOOP_CAP)
        self.assertEqual(sel["loops"][0]["title"], "loop 0")

    def test_loops_are_open_episodic_or_project_only(self):
        _add(self.conn, "interest", "an interest", salience=9.0)
        _add(self.conn, "episodic", "a loop", salience=8.0)
        _add(self.conn, "project", "a project loop", salience=7.5)
        _add(self.conn, "episodic", "done chore", salience=7.0, status="closed")
        sel = workingset.select(self.conn)
        titles = {r["title"] for r in sel["loops"]}
        self.assertIn("a loop", titles)
        self.assertIn("a project loop", titles)
        self.assertNotIn("an interest", titles)
        self.assertNotIn("done chore", titles)

    def test_recent_excludes_tier1_and_includes_interests(self):
        _add(self.conn, "episodic", "a loop", salience=8.0)
        _add(self.conn, "interest", "an interest", salience=9.0)
        sel = workingset.select(self.conn)
        loop_ids = {r["id"] for r in sel["loops"]}
        recent_ids = {r["id"] for r in sel["recent"]}
        self.assertEqual(loop_ids & recent_ids, set())
        recent_titles = {r["title"] for r in sel["recent"]}
        self.assertIn("an interest", recent_titles)

    def test_older_counts_unsurfaced_open_only(self):
        for i in range(config.WS_OPEN_LOOP_CAP + config.WS_RECENT_CAP + 4):
            _add(self.conn, "episodic", f"loop {i}", salience=10.0 - i * 0.01)
        _add(self.conn, "episodic", "closed one", salience=1.0, status="closed")
        sel = workingset.select(self.conn)
        surfaced = len(sel["loops"]) + len(sel["recent"])
        self.assertEqual(sel["older"], 4)

    def test_recent_cut_does_not_split_an_exact_salience_tie(self):
        # Nine distinct high-salience interests, then a four-way exact tie that
        # straddles the cap. The cut must drop the whole tied group, not keep an
        # arbitrary one of them, so the boundary falls on a clear gap.
        for i in range(9):
            _add(self.conn, "interest", f"distinct {i}", salience=5.0 - i * 0.1)
        for i in range(4):
            _add(self.conn, "interest", f"tied {i}", salience=4.0)
        sel = workingset.select(self.conn)
        self.assertEqual(len(sel["recent"]), 9)
        self.assertTrue(all(r["salience"] > 4.0 for r in sel["recent"]))

    def test_recent_falls_back_to_cap_when_whole_tier_ties(self):
        # Degenerate: every candidate shares one salience, so there is no gap to
        # stop at. Rather than empty the tier, fall back to the hard cap.
        for i in range(config.WS_RECENT_CAP + 3):
            _add(self.conn, "interest", f"flat {i}", salience=4.0)
        sel = workingset.select(self.conn)
        self.assertEqual(len(sel["recent"]), config.WS_RECENT_CAP)

    def test_lessons_get_reserved_seats_and_never_enter_recent(self):
        # A burst of high-salience interests must not displace a lesson: the
        # tiers draw from disjoint pools by kind.
        _add(self.conn, "lesson", "no unbidden writes", salience=4.0)
        for i in range(config.WS_RECENT_CAP + 2):
            _add(self.conn, "interest", f"shiny {i}", salience=9.0 - i * 0.1)
        sel = workingset.select(self.conn)
        lesson_titles = {r["title"] for r in sel["lessons"]}
        recent_titles = {r["title"] for r in sel["recent"]}
        self.assertIn("no unbidden writes", lesson_titles)
        self.assertNotIn("no unbidden writes", recent_titles)
        self.assertEqual(len(sel["recent"]), config.WS_RECENT_CAP)

    def test_lessons_capped_and_excluded_from_loops(self):
        for i in range(config.WS_LESSON_CAP + 2):
            _add(self.conn, "lesson", f"lesson {i}", salience=8.0 - i * 0.1)
        sel = workingset.select(self.conn)
        self.assertEqual(len(sel["lessons"]), config.WS_LESSON_CAP)
        self.assertEqual(sel["lessons"][0]["title"], "lesson 0")
        self.assertEqual(sel["loops"], [])
        # Overflow lessons count toward the older-threads pointer.
        self.assertEqual(sel["older"], 2)

    def test_closed_lessons_not_selected(self):
        _add(self.conn, "lesson", "graduated", salience=8.0, status="closed")
        sel = workingset.select(self.conn)
        self.assertEqual(sel["lessons"], [])


class StaleTest(unittest.TestCase):
    def test_last_dream_date_reads_first_heading(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "journal.md")
            with open(p, "w") as fh:
                fh.write("# Memory Dream Journal\n\n## 2026-06-14\n\n- did a thing\n\n## 2026-06-01\n\n- older\n")
            self.assertEqual(workingset.last_dream_date(p), "2026-06-14")

    def test_stale_days_counts_from_latest(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "journal.md")
            with open(p, "w") as fh:
                fh.write("# Memory Dream Journal\n\n## 2026-06-10\n\n- a\n")
            now = datetime(2026, 6, 15, tzinfo=timezone.utc)
            self.assertEqual(workingset.stale_days(now=now, journal_path=p), 5)

    def test_stale_days_same_local_day_is_zero(self):
        # Regression: an evening 'now' on the same local day as the dream must read 0,
        # not 1. The heading is a local date; the old code compared a UTC now against
        # UTC-midnight and rolled past 24h once UTC crossed into the next day.
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "journal.md")
            with open(p, "w") as fh:
                fh.write("# Memory Dream Journal\n\n## 2026-06-19\n\n- a\n")
            now = datetime(2026, 6, 19, 18, 32)  # naive local, same calendar day
            self.assertEqual(workingset.stale_days(now=now, journal_path=p), 0)

    def test_stale_days_none_when_no_journal(self):
        self.assertIsNone(workingset.stale_days(journal_path="/nonexistent/journal.md"))


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_empty_store_renders_empty_string(self):
        sel = workingset.select(self.conn)
        self.assertEqual(workingset.render(sel), "")

    def test_three_tiers_present(self):
        _add(self.conn, "episodic", "ship the hook", salience=8.0)
        _add(self.conn, "interest", "civil liberties", salience=5.0)
        for i in range(config.WS_OPEN_LOOP_CAP + config.WS_RECENT_CAP + 3):
            _add(self.conn, "episodic", f"thread {i}", salience=4.0 - i * 0.01)
        sel = workingset.select(self.conn)
        out = workingset.render(sel)
        self.assertIn("Open loops", out)
        self.assertIn("Recent context", out)
        self.assertIn("ship the hook", out)
        self.assertRegex(out, r"older")
        self.assertGreater(sel["older"], 0)

    def test_standing_lessons_section_renders_between_loops_and_recent(self):
        _add(self.conn, "episodic", "ship the hook", salience=8.0)
        _add(self.conn, "lesson", "thinking aloud is not a directive", salience=6.0)
        _add(self.conn, "interest", "civil liberties", salience=5.0)
        sel = workingset.select(self.conn)
        out = workingset.render(sel)
        self.assertIn("## Standing lessons", out)
        self.assertIn("thinking aloud is not a directive", out)
        self.assertLess(out.index("Open loops"), out.index("Standing lessons"))
        self.assertLess(out.index("Standing lessons"), out.index("Recent context"))

    def test_lessons_alone_still_render(self):
        _add(self.conn, "lesson", "only a scar", salience=6.0)
        sel = workingset.select(self.conn)
        out = workingset.render(sel)
        self.assertIn("only a scar", out)

    def test_every_tier_carries_its_memory_id(self):
        """A carried line must be addressable. Without the id, expanding a
        one-line summary means a search-and-grep hunt through the store; with it,
        `hippo show <id>` is a single call."""
        loop = _add(self.conn, "episodic", "ship the hook", salience=8.0)
        lesson = _add(self.conn, "lesson", "use the purpose-built tool", salience=6.0)
        interest = _add(self.conn, "interest", "civil liberties", salience=5.0)
        out = workingset.render(workingset.select(self.conn))
        for mid in (loop, lesson, interest):
            self.assertRegex(
                out, rf"- \[{mid}\] ",
                f"memory {mid} rendered without its id, so it cannot be expanded",
            )

    def test_tail_names_the_one_shot_expansion_command(self):
        _add(self.conn, "episodic", "a loop", salience=8.0)
        out = workingset.render(workingset.select(self.conn))
        self.assertIn("hippo show", out)

    def test_stale_note_when_over_threshold(self):
        _add(self.conn, "episodic", "a loop", salience=8.0)
        sel = workingset.select(self.conn)
        out = workingset.render(sel, stale=4)
        self.assertIn("4 days ago", out)

    def test_no_stale_note_when_fresh(self):
        _add(self.conn, "episodic", "a loop", salience=8.0)
        sel = workingset.select(self.conn)
        out = workingset.render(sel, stale=0)
        self.assertNotIn("days ago", out)

    NOW = datetime(2026, 6, 20, 9, 0, 0)

    def test_last_at_shows_absolute_date_and_time(self):
        # The carryover header must carry an absolute timestamp, not a day-granular
        # "today"/"N days ago" that only bounds the last dream to a 24h window.
        _add(self.conn, "episodic", "a loop", salience=8.0)
        sel = workingset.select(self.conn)
        out = workingset.render(sel, last_at="2026-06-20T06:26:54", now=self.NOW)
        self.assertIn("Last dream: 2026-06-20 06:26", out)
        self.assertNotIn("today", out)

    def test_last_at_adds_stale_parenthetical_when_old(self):
        _add(self.conn, "episodic", "a loop", salience=8.0)
        sel = workingset.select(self.conn)
        out = workingset.render(sel, last_at="2026-06-16T02:30:00", now=self.NOW)
        self.assertIn("Last dream: 2026-06-16 02:30", out)
        self.assertIn("4 days ago", out)
        self.assertIn("stale", out)

    def test_last_at_takes_precedence_over_stale_days(self):
        _add(self.conn, "episodic", "a loop", salience=8.0)
        sel = workingset.select(self.conn)
        out = workingset.render(sel, stale=0, last_at="2026-06-20T06:26:54", now=self.NOW)
        self.assertIn("06:26", out)
        self.assertNotIn("Last dream: today", out)

    def test_malformed_last_at_falls_back_to_stale_days(self):
        _add(self.conn, "episodic", "a loop", salience=8.0)
        sel = workingset.select(self.conn)
        out = workingset.render(sel, stale=0, last_at="not-a-timestamp", now=self.NOW)
        self.assertIn("Last dream: today", out)

    def test_dream_age_shown_today_when_fresh(self):
        # Fallback path: no recorded timestamp, only the day count.
        _add(self.conn, "episodic", "a loop", salience=8.0)
        sel = workingset.select(self.conn)
        out = workingset.render(sel, stale=0)
        self.assertIn("Last dream: today", out)

    def test_dream_age_singular_day(self):
        _add(self.conn, "episodic", "a loop", salience=8.0)
        sel = workingset.select(self.conn)
        out = workingset.render(sel, stale=1)
        self.assertIn("1 day ago", out)
        self.assertNotIn("1 days ago", out)

    def test_no_dream_age_line_when_unknown(self):
        _add(self.conn, "episodic", "a loop", salience=8.0)
        sel = workingset.select(self.conn)
        out = workingset.render(sel, stale=None)
        self.assertNotIn("Last dream", out)

    def test_failure_note_overrides_stale_note(self):
        _add(self.conn, "episodic", "a loop", salience=8.0)
        sel = workingset.select(self.conn)
        failure = {"date": "2026-06-20", "error": "claude -p exited 1: 401 auth"}
        out = workingset.render(sel, stale=1, failure=failure)
        self.assertIn("Last dream FAILED (2026-06-20)", out)
        self.assertIn("401 auth", out)
        self.assertIn("re-run", out.lower())
        self.assertNotIn("1 day ago", out)

    def test_failure_note_overrides_last_at(self):
        _add(self.conn, "episodic", "a loop", salience=8.0)
        sel = workingset.select(self.conn)
        failure = {"date": "2026-06-20", "error": "401 auth"}
        out = workingset.render(
            sel, last_at="2026-06-20T06:26:54", failure=failure, now=self.NOW)
        self.assertIn("Last dream FAILED (2026-06-20)", out)
        self.assertNotIn("06:26", out)


class RenderMotdTest(unittest.TestCase):
    NOW = datetime(2026, 6, 20, 9, 0, 0)

    def test_today_shows_time(self):
        line = workingset.render_motd(last_at="2026-06-20T02:33:50", now=self.NOW)
        self.assertEqual(line, "Last dream happened today at 02:33")

    def test_yesterday(self):
        line = workingset.render_motd(last_at="2026-06-19T02:31:00", now=self.NOW)
        self.assertEqual(line, "Last dream happened yesterday at 02:31")

    def test_older_shows_date_and_time(self):
        line = workingset.render_motd(last_at="2026-06-17T02:30:00", now=self.NOW)
        self.assertEqual(line, "Last dream happened on Jun 17 at 02:30")

    def test_failure_takes_precedence(self):
        line = workingset.render_motd(
            failure={"date": "2026-06-20", "error": "401"},
            last_at="2026-06-20T02:33:50", now=self.NOW,
        )
        self.assertIn("Last dream FAILED (2026-06-20)", line)
        self.assertIn("hippo dream", line)

    def test_falls_back_to_journal_date_when_no_time(self):
        line = workingset.render_motd(journal_date="2026-06-20", now=self.NOW)
        self.assertEqual(line, "Last dream: 2026-06-20")

    def test_malformed_timestamp_falls_back(self):
        line = workingset.render_motd(
            last_at="not-a-timestamp", journal_date="2026-06-20", now=self.NOW)
        self.assertEqual(line, "Last dream: 2026-06-20")

    def test_empty_when_nothing_known(self):
        self.assertEqual(workingset.render_motd(now=self.NOW), "")


class DreamFailureTest(unittest.TestCase):
    def test_none_when_no_sentinel(self):
        self.assertIsNone(workingset.dream_failure(failure_path="/nonexistent/x.json"))

    def test_reads_sentinel(self):
        import json
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps({"date": "2026-06-20", "error": "boom"}))
            path = f.name
        try:
            got = workingset.dream_failure(failure_path=path)
            self.assertEqual(got["date"], "2026-06-20")
            self.assertEqual(got["error"], "boom")
        finally:
            import os
            os.unlink(path)

    def test_none_on_malformed_sentinel(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("not json {")
            path = f.name
        try:
            self.assertIsNone(workingset.dream_failure(failure_path=path))
        finally:
            import os
            os.unlink(path)


class HandoffExclusionTest(unittest.TestCase):
    """Handoff rows never leak into the loops or recent tiers; the fourth
    section that surfaces them is tested here."""

    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_handoff_not_in_loops_or_recent(self):
        _add(self.conn, "episodic", "loop", salience=5.0)
        _add(self.conn, "interest", "interest", salience=4.0)
        _add(self.conn, "handoff", "note to self", salience=9.0)
        sel = workingset.select(self.conn)
        titles = {r["title"] for r in sel["loops"]} | {r["title"] for r in sel["recent"]}
        self.assertNotIn("note to self", titles)
        self.assertIn("loop", {r["title"] for r in sel["loops"]})
        self.assertIn("interest", {r["title"] for r in sel["recent"]})

    def test_rescore_keeps_handoff_at_zero(self):
        mid = _add(self.conn, "handoff", "note", salience=9.0)
        store.rescore_all(self.conn)
        self.assertEqual(store.get_memory(self.conn, mid)["salience"], 0.0)


class HandoffSectionTest(unittest.TestCase):
    """The fourth section: rule-selected handoff notes rendered
    between Standing lessons and Recent context, never counted as older
    threads, never in the ranked tiers."""

    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_section_renders_between_lessons_and_recent(self):
        from hippo_memory import handoff
        loop = _add(self.conn, "episodic", "router probe", salience=8.0)
        _add(self.conn, "lesson", "confirm the reading", salience=6.0)
        _add(self.conn, "interest", "mesh radio", salience=5.0)
        h = handoff.add(self.conn, "router: parser done, cron unverified",
                        "State: parser done", "s1", carries=[loop])
        sel = workingset.select(self.conn)
        self.assertEqual([r["id"] for r in sel["handoffs"]], [h])
        out = workingset.render(sel)
        self.assertIn(handoff.SECTION_HEADER, out)
        self.assertLess(out.index("Standing lessons"), out.index("Notes to self"))
        self.assertLess(out.index("Notes to self"), out.index("Recent context"))
        line = [l for l in out.splitlines() if f"[{h}]" in l][0]
        self.assertIn("router: parser done, cron unverified", line)
        self.assertIn(f"(carries {loop})", line)
        self.assertEqual(sel["older"], 0)

    def test_handoff_alone_renders_and_is_not_older(self):
        from hippo_memory import handoff
        handoff.add(self.conn, "free note", "State: x", "s1")
        sel = workingset.select(self.conn)
        self.assertEqual(sel["older"], 0)
        out = workingset.render(sel)
        self.assertIn("free note", out)
        self.assertNotIn("Open loops", out)

    def test_dead_handoff_not_rendered(self):
        from hippo_memory import handoff
        loop = _add(self.conn, "episodic", "done thing", salience=8.0, status="closed")
        handoff.add(self.conn, "stale", "State: x", "s1", carries=[loop])
        handoff.add(self.conn, "newer free", "State: x", "s2")
        handoff.add(self.conn, "older free", "State: x", "s3")
        # created_at ties within a test; force the order by id.
        self.conn.execute("UPDATE memories SET created_at='2026-08-01T00:00:00+00:00' WHERE title='older free'")
        self.conn.commit()
        out = workingset.render(workingset.select(self.conn))
        self.assertNotIn("stale", out)
        self.assertIn("newer free", out)
        self.assertNotIn("older free", out)



def test_select_has_no_laurel_and_render_ignores_session(tmp_path):
    from hippo_memory import store, workingset
    conn = store.connect(tmp_path / "w.db")
    store.init_schema(conn)
    store.upsert_memory(conn, "episodic", "Open loop one", "", "k1")
    sel = workingset.select(conn, session_id="abc")
    assert "laurel" not in sel
    out = workingset.render(sel)
    assert "Open loop one" in out and "Laurel" not in out
