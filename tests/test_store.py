import unittest
from hippo_memory import config, store


def test_connect_creates_data_dir_owner_only(tmp_path):
    db_path = tmp_path / "data" / "hippo.db"
    conn = store.connect(str(db_path))
    conn.close()
    mode = db_path.parent.stat().st_mode & 0o777
    assert mode == 0o700


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_tables_exist(self):
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r["name"] for r in rows}
        self.assertIn("memories", names)
        self.assertIn("links", names)
        self.assertIn("dream_state", names)

    def test_wal_and_foreign_keys_pragmas(self):
        fk = self.conn.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(fk, 1)


class CrudTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_upsert_inserts_then_merges_on_dedup_key(self):
        a = store.upsert_memory(self.conn, "episodic", "Ship hippo", dedup_key="k1")
        b = store.upsert_memory(self.conn, "episodic", "Ship hippo", dedup_key="k1")
        self.assertEqual(a, b)
        row = store.get_memory(self.conn, a)
        self.assertEqual(row["recurrence"], 2)

    def test_close_sets_status(self):
        i = store.upsert_memory(self.conn, "episodic", "Loop", dedup_key="k2")
        store.close_memory(self.conn, i)
        self.assertEqual(store.get_memory(self.conn, i)["status"], "closed")

    def test_watermark_roundtrip(self):
        self.assertIsNone(store.get_state(self.conn, "watermark"))
        store.set_state(self.conn, "watermark", "sess-42")
        self.assertEqual(store.get_state(self.conn, "watermark"), "sess-42")


class RescoreTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_rescore_then_rank_open_above_closed(self):
        a = store.upsert_memory(self.conn, "episodic", "Open thread", dedup_key="a")
        b = store.upsert_memory(self.conn, "episodic", "Done thread", dedup_key="b")
        store.close_memory(self.conn, b)
        store.rescore_all(self.conn)
        ranked = store.ranked(self.conn, limit=10)
        self.assertEqual(ranked[0]["id"], a)
        self.assertGreater(ranked[0]["salience"], 0.0)

    def test_ranked_status_filter(self):
        store.upsert_memory(self.conn, "episodic", "Open", dedup_key="o")
        c = store.upsert_memory(self.conn, "episodic", "Closed", dedup_key="c")
        store.close_memory(self.conn, c)
        store.rescore_all(self.conn)
        open_only = store.ranked(self.conn, status="open")
        self.assertTrue(all(r["status"] == "open" for r in open_only))

    def test_interest_kind_accepted_and_outranks_execution(self):
        # The schema must accept kind='interest', and after rescore a single-mention
        # interest must rank above a fresh open execution item with recurrence.
        intr = store.upsert_memory(self.conn, "interest", "Cares about X", dedup_key="i")
        exe = store.upsert_memory(self.conn, "episodic", "Built Y", dedup_key="e")
        store.upsert_memory(self.conn, "episodic", "Built Y", dedup_key="e")  # recurrence=2
        store.rescore_all(self.conn)
        ranked = store.ranked(self.conn, limit=10)
        self.assertEqual(ranked[0]["id"], intr)
        intr_row = store.get_memory(self.conn, intr)
        exe_row = store.get_memory(self.conn, exe)
        self.assertGreater(intr_row["salience"], exe_row["salience"])


class RankedFilterTest(unittest.TestCase):
    #: the list flags ride on ranked()'s optional filters.
    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)
        self.lesson = store.upsert_memory(self.conn, "lesson", "Old scar", dedup_key="l")
        self.interest = store.upsert_memory(self.conn, "interest", "New shiny", dedup_key="i")
        # Backdate the lesson so the recency filters have a boundary to test.
        self.conn.execute(
            "UPDATE memories SET created_at='2026-01-01T00:00:00+00:00', "
            "last_touched_at='2026-01-02T00:00:00+00:00' WHERE id=?",
            (self.lesson,))
        self.conn.commit()
        store.rescore_all(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_kind_filter(self):
        rows = store.ranked(self.conn, kind="lesson")
        self.assertEqual([r["id"] for r in rows], [self.lesson])

    def test_since_filters_on_created_at(self):
        rows = store.ranked(self.conn, since="2026-02-01")
        self.assertEqual([r["id"] for r in rows], [self.interest])

    def test_touched_since_filters_on_last_touched(self):
        rows = store.ranked(self.conn, touched_since="2026-01-02")
        self.assertEqual({r["id"] for r in rows}, {self.lesson, self.interest})
        rows = store.ranked(self.conn, touched_since="2026-01-03")
        self.assertEqual([r["id"] for r in rows], [self.interest])

    def test_min_salience_floor(self):
        sal = {r["id"]: r["salience"] for r in store.ranked(self.conn)}
        floor = (sal[self.lesson] + sal[self.interest]) / 2
        rows = store.ranked(self.conn, min_salience=floor)
        self.assertEqual([r["id"] for r in rows],
                         [max(sal, key=sal.get)])

    def test_filters_compose(self):
        rows = store.ranked(self.conn, kind="interest", since="2026-02-01",
                            status="open")
        self.assertEqual([r["id"] for r in rows], [self.interest])
        rows = store.ranked(self.conn, kind="lesson", since="2026-02-01")
        self.assertEqual(rows, [])

    def test_sort_id_and_recency(self):
        by_id = store.ranked(self.conn, order="id")
        self.assertEqual([r["id"] for r in by_id],
                         sorted(r["id"] for r in by_id))
        by_recency = store.ranked(self.conn, order="recency")
        self.assertEqual(by_recency[0]["id"], self.interest)


class SetWeightTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_set_weight_writes_weight_and_rearms_recency(self):
        mid = store.upsert_memory(self.conn, "project", "Sparring scoreboard",
                                  dedup_key="s")
        self.conn.execute(
            "UPDATE memories SET last_touched_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", mid),
        )
        self.conn.commit()
        store.set_weight(self.conn, mid, 1.0)
        row = store.get_memory(self.conn, mid)
        self.assertEqual(row["weight"], 1.0)
        self.assertNotEqual(row["last_touched_at"], "2020-01-01T00:00:00+00:00")

    def test_set_weight_leaves_status_unchanged(self):
        mid = store.upsert_memory(self.conn, "episodic", "Closed thread",
                                  dedup_key="c")
        store.close_memory(self.conn, mid)
        store.set_weight(self.conn, mid, 1.0)
        self.assertEqual(store.get_memory(self.conn, mid)["status"], "closed")

    def test_set_weight_raises_salience_by_the_weight_term(self):
        mid = store.upsert_memory(self.conn, "episodic", "Thread", dedup_key="t")
        store.rescore_all(self.conn)
        before = store.get_memory(self.conn, mid)["salience"]
        store.set_weight(self.conn, mid, 1.0)
        store.rescore_all(self.conn)
        after = store.get_memory(self.conn, mid)["salience"]
        self.assertGreater(after, before)
        self.assertAlmostEqual(after - before, 1.0, places=3)


class EmbeddingsTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_upsert_then_get_roundtrips_and_replaces(self):
        store.upsert_embedding(self.conn, "hippo", "1", 3, b"\x00\x01\x02",
                               model="m1", source_mtime=None)
        store.upsert_embedding(self.conn, "hippo", "1", 3, b"\x09\x09\x09",
                               model="m2", source_mtime=None)
        rows = store.get_embeddings(self.conn, source="hippo")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "m2")
        self.assertEqual(rows[0]["vec"], b"\x09\x09\x09")

    def test_get_filters_by_source(self):
        store.upsert_embedding(self.conn, "hippo", "1", 1, b"\x00", "m", None)
        store.upsert_embedding(self.conn, "stock", "a.md", 1, b"\x00", "m", "t")
        self.assertEqual(len(store.get_embeddings(self.conn, source="stock")), 1)
        self.assertEqual(len(store.get_embeddings(self.conn)), 2)

    def test_unembedded_counts_rows_with_no_vector_for_the_model(self):
        a = store.upsert_memory(self.conn, "episodic", "indexed", dedup_key="a")
        store.upsert_memory(self.conn, "episodic", "never indexed", dedup_key="b")
        store.upsert_embedding(self.conn, "hippo", str(a), 1, b"\x00", "m1", None)
        self.assertEqual(store.unembedded_hippo(self.conn, "m1"), 1)

    def test_a_model_change_makes_every_row_unembedded(self):
        a = store.upsert_memory(self.conn, "episodic", "indexed", dedup_key="a")
        store.upsert_embedding(self.conn, "hippo", str(a), 1, b"\x00", "m1", None)
        self.assertEqual(store.unembedded_hippo(self.conn, "m2"), 1)


class FoldTest(unittest.TestCase):
    """Folding retires a duplicate that a durable rule already covers: the body
    survives as evidence, but the row leaves the retrieval pool so it stops
    outranking the rule it duplicates. Closing alone does not do this, because
    search ranks on relevance and does not filter by status."""

    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)
        self.mid = store.upsert_memory(self.conn, "episodic", "dup rule", dedup_key="d")
        store.upsert_embedding(self.conn, "hippo", str(self.mid), 1, b"\x00", "m1", None)

    def tearDown(self):
        self.conn.close()

    def test_fold_closes_records_the_target_and_drops_the_vector(self):
        store.fold_memory(self.conn, self.mid, into="feedback_example.md")
        row = store.get_memory(self.conn, self.mid)
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["folded_into"], "feedback_example.md")
        self.assertIsNone(
            store.get_embedding(self.conn, "hippo", str(self.mid)),
            "a folded row kept its vector, so it still competes in recall",
        )

    def test_fold_keeps_the_body_as_evidence_and_appends_the_note(self):
        store.fold_memory(self.conn, self.mid, into="stock_rule.md", note="superseded")
        body = store.get_memory(self.conn, self.mid)["body"]
        self.assertIn("superseded", body)

    def test_folded_rows_are_not_counted_as_unembedded(self):
        """A folded row has no vector by design, so coverage must not chase it
        forever and report a hole that is deliberate."""
        store.fold_memory(self.conn, self.mid, into="stock_rule.md")
        self.assertEqual(store.unembedded_hippo(self.conn, "m1"), 0)


_PRE_LESSON_SCHEMA = store.SCHEMA_SQL.replace(
    "'episodic','project','interest','lesson'", "'episodic','project','interest'"
)


class LessonMigrationTest(unittest.TestCase):
    """A store created before the lesson kind must be rebuilt by init_schema:
    CREATE TABLE IF NOT EXISTS leaves the old CHECK in place, so without the
    migration every lesson insert fails forever on live databases."""

    def setUp(self):
        self.conn = store.connect(":memory:")
        self.conn.executescript(_PRE_LESSON_SCHEMA)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_old_store_rejects_lesson_without_migration(self):
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO memories (kind, title, created_at, last_touched_at) "
                "VALUES ('lesson', 'scar', '2026-07-12', '2026-07-12')"
            )

    def test_init_schema_migrates_and_preserves_rows_and_links(self):
        a = store.upsert_memory(self.conn, "episodic", "old row", dedup_key="a",
                                weight=1.5, pinned=True)
        b = store.upsert_memory(self.conn, "interest", "other row", dedup_key="b")
        store.add_link(self.conn, a, b)

        store.init_schema(self.conn)

        row = store.get_memory(self.conn, a)
        self.assertEqual(row["title"], "old row")
        self.assertEqual(row["weight"], 1.5)
        self.assertEqual(row["pinned"], 1)
        self.assertEqual(store.count_links(self.conn, a), 1)
        # The widened CHECK now admits lessons and ids keep advancing.
        c = store.upsert_memory(self.conn, "lesson", "a scar", dedup_key="c")
        self.assertGreater(c, b)
        # dedup (the UNIQUE index) survived the rebuild.
        again = store.upsert_memory(self.conn, "lesson", "a scar", dedup_key="c")
        self.assertEqual(again, c)

    def test_migration_is_idempotent(self):
        store.init_schema(self.conn)
        before = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='memories'"
        ).fetchone()["sql"]
        store.init_schema(self.conn)
        after = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='memories'"
        ).fetchone()["sql"]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()


_PRE_HANDOFF_SCHEMA = store.SCHEMA_SQL.replace(
    "'episodic','project','interest','lesson','handoff'",
    "'episodic','project','interest','lesson'",
)


class HandoffKindTest(unittest.TestCase):
    """The handoff kind: admitted by the CHECK, migrated onto
    lesson-era stores by the same rebuild that admitted lessons, body-capped,
    and linkable to the loops it carries."""

    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_handoff_kind_is_admitted(self):
        mid = store.upsert_memory(self.conn, "handoff", "note", body="State: x",
                                  dedup_key="handoff:s:1")
        self.assertEqual(store.get_memory(self.conn, mid)["kind"], "handoff")

    def test_unknown_kind_still_rejected(self):
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            store.upsert_memory(self.conn, "diary", "nope")

    def test_body_cap_enforced_for_handoff_only(self):
        long_body = "x" * (config.HANDOFF_BODY_MAX + 1)
        with self.assertRaises(ValueError):
            store.upsert_memory(self.conn, "handoff", "too long", body=long_body)
        # Other kinds are not capped.
        mid = store.upsert_memory(self.conn, "episodic", "fine", body=long_body)
        self.assertIsNotNone(mid)

    def test_carries_link(self):
        loop = store.upsert_memory(self.conn, "episodic", "loop", dedup_key="l")
        note = store.upsert_memory(self.conn, "handoff", "note", dedup_key="h")
        store.add_link(self.conn, note, loop, kind="carries")
        rows = self.conn.execute(
            "SELECT kind FROM links WHERE from_id=? AND to_id=?", (note, loop)
        ).fetchall()
        self.assertEqual([r["kind"] for r in rows], ["carries"])


class HandoffMigrationTest(unittest.TestCase):
    """A lesson-era store (post-lesson, pre-handoff, with folded_into present)
    must be rebuilt to admit handoff rows, keeping every column and link."""

    def setUp(self):
        self.conn = store.connect(":memory:")
        self.conn.executescript(_PRE_HANDOFF_SCHEMA)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_old_store_rejects_handoff_without_migration(self):
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO memories (kind, title, created_at, last_touched_at) "
                "VALUES ('handoff', 'n', '2026-08-25', '2026-08-25')"
            )

    def test_init_schema_migrates_and_preserves_folded_into(self):
        a = store.upsert_memory(self.conn, "lesson", "scar", dedup_key="a", weight=1.5)
        b = store.upsert_memory(self.conn, "interest", "thing", dedup_key="b")
        store.add_link(self.conn, a, b)
        self.conn.execute("UPDATE memories SET folded_into='stock.md' WHERE id=?", (b,))
        self.conn.commit()

        store.init_schema(self.conn)

        self.assertEqual(store.get_memory(self.conn, a)["weight"], 1.5)
        self.assertEqual(store.get_memory(self.conn, b)["folded_into"], "stock.md")
        self.assertEqual(store.count_links(self.conn, a), 1)
        c = store.upsert_memory(self.conn, "handoff", "note", dedup_key="c")
        self.assertGreater(c, b)

    def test_migration_is_idempotent(self):
        store.init_schema(self.conn)
        before = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='memories'"
        ).fetchone()["sql"]
        store.init_schema(self.conn)
        self.assertEqual(before, self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='memories'"
        ).fetchone()["sql"])
