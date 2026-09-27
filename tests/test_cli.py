import io
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from hippo_memory import cli, embed, store

FIXTURES = Path(__file__).parent / "fixtures"


class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Path(self.dir.name) / "hippo.db"
        os.environ["HIPPO_DB"] = str(self.db)

    def tearDown(self):
        os.environ.pop("HIPPO_DB", None)
        self.dir.cleanup()

    def test_init_then_add_then_list(self):
        cli.ensure_store()
        self.assertEqual(
            cli.main(["add", "--kind", "episodic", "--title", "Ship hippo",
                      "--dedup-key", "k1"]),
            0,
        )
        # second add with same key merges (recurrence increments)
        cli.main(["add", "--kind", "episodic", "--title", "Ship hippo",
                  "--dedup-key", "k1"])
        conn = store.connect(self.db)
        row = conn.execute("SELECT recurrence FROM memories WHERE dedup_key='k1'").fetchone()
        conn.close()
        self.assertEqual(row["recurrence"], 2)

    def test_list_flags_compose_and_episode_aliases_episodic(self):
        cli.ensure_store()
        cli.main(["add", "--kind", "episodic", "--title", "A thread",
                  "--dedup-key", "e1"])
        cli.main(["add", "--kind", "lesson", "--title", "A scar",
                  "--dedup-key", "l1"])
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["list", "--kind", "episode"]), 0)
        self.assertIn("A thread", out.getvalue())
        self.assertNotIn("A scar", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["list", "--kind", "lesson", "--status", "open",
                      "--min-salience", "0", "--sort", "id"])
        self.assertIn("A scar", out.getvalue())

    def test_list_default_limit_notes_hidden_rows(self):
        cli.ensure_store()
        for i in range(32):
            cli.main(["add", "--kind", "interest", "--title", f"row {i}",
                      "--dedup-key", f"k{i}"])
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["list"])
        text = out.getvalue()
        self.assertIn("2 more row(s)", text)
        self.assertEqual(
            len([l for l in text.splitlines() if l.startswith("[")]), 30)
        # --limit 0 lifts the cap and drops the note
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["list", "--limit", "0"])
        text = out.getvalue()
        self.assertNotIn("more row(s)", text)
        self.assertEqual(
            len([l for l in text.splitlines() if l.startswith("[")]), 32)

    def test_list_dates_flag_appends_created_and_touched_dates(self):
        cli.ensure_store()
        cli.main(["add", "--kind", "episodic", "--title", "Dated row",
                  "--dedup-key", "d1"])
        conn = store.connect(self.db)
        mid = conn.execute(
            "SELECT id FROM memories WHERE dedup_key='d1'").fetchone()["id"]
        conn.execute(
            "UPDATE memories SET created_at=?, last_touched_at=? WHERE id=?",
            ("2026-07-28T00:00:00+00:00", "2026-08-01T12:34:56+00:00", mid),
        )
        conn.commit()
        conn.close()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["list", "--dates"]), 0)
        line = next(l for l in out.getvalue().splitlines()
                    if l.startswith(f"[{mid}]"))
        # id, salience, status, created date, touched date, title - in that order.
        self.assertRegex(
            line,
            rf"^\[{mid}\] [\d.]+ open\s+2026-07-28 2026-08-01 Dated row$",
        )

    def test_list_without_dates_flag_is_byte_identical_to_undated_output(self):
        cli.ensure_store()
        cli.main(["add", "--kind", "episodic", "--title", "A thread",
                  "--dedup-key", "e1"])
        cli.main(["add", "--kind", "lesson", "--title", "A scar",
                  "--dedup-key", "l1"])
        conn = store.connect(self.db)
        rows = store.ranked(conn, order="salience")
        conn.close()
        # The documented legacy format, reconstructed independently of
        # cmd_list so a regression in the default branch cannot mask itself.
        expected = "".join(
            f"[{r['id']}] {r['salience']:.2f} {r['status']:6} {r['title']}\n"
            for r in rows
        )
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["list", "--limit", "0"])
        self.assertEqual(out.getvalue(), expected)

    def test_fold_subcommand_retires_the_row_and_its_vector(self):
        cli.ensure_store()
        cli.main(["add", "--kind", "episodic", "--title", "Dup rule",
                  "--dedup-key", "dup"])
        conn = store.connect(self.db)
        mid = conn.execute(
            "SELECT id FROM memories WHERE dedup_key='dup'").fetchone()["id"]
        store.upsert_embedding(conn, "hippo", str(mid), 1, b"\x00", "m1", None)
        conn.close()

        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(
                cli.main(["fold", str(mid), "--into", "feedback_example.md",
                          "--note", "superseded by the stock rule"]), 0)
        self.assertIn("folded", out.getvalue())

        conn = store.connect(self.db)
        row = store.get_memory(conn, mid)
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["folded_into"], "feedback_example.md")
        self.assertIsNone(store.get_embedding(conn, "hippo", str(mid)))
        conn.close()

    def test_watermark_subcommand(self):
        cli.ensure_store()
        self.assertEqual(cli.main(["watermark", "set", "sess-9"]), 0)
        conn = store.connect(self.db)
        self.assertEqual(store.get_state(conn, "watermark"), "sess-9")
        conn.close()

    def test_offset_roundtrip(self):
        cli.ensure_store()
        self.assertEqual(cli.main(["offset", "set", "abc-123", "42"]), 0)
        conn = store.connect(self.db)
        self.assertEqual(store.get_state(conn, "session:abc-123"), "42")
        conn.close()

    def test_backup_writes_consistent_snapshot(self):
        bak = Path(self.dir.name) / "backup" / "hippo.db"
        os.environ["HIPPO_BACKUP"] = str(bak)
        try:
            cli.ensure_store()
            cli.main(["add", "--kind", "episodic", "--title", "X", "--dedup-key", "x"])
            self.assertEqual(cli.main(["backup"]), 0)
            self.assertTrue(bak.exists())
            snap = store.connect(bak)
            self.assertEqual(snap.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            n = snap.execute("SELECT count(*) FROM memories").fetchone()[0]
            snap.close()
            self.assertEqual(n, 1)
        finally:
            os.environ.pop("HIPPO_BACKUP", None)

    def test_backup_snapshot_is_not_wal(self):
        # The snapshot inherits the live store's WAL mode through the backup
        # API, so every later reader of the snapshot recreates hippo.db-wal and
        # hippo.db-shm beside it. Reading it must leave one file.
        bak = Path(self.dir.name) / "backup" / "hippo.db"
        os.environ["HIPPO_BACKUP"] = str(bak)
        try:
            cli.ensure_store()
            cli.main(["add", "--kind", "episodic", "--title", "X", "--dedup-key", "x"])
            self.assertEqual(cli.main(["backup"]), 0)
            conn = sqlite3.connect(str(bak))
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            conn.execute("SELECT count(*) FROM memories").fetchone()
            conn.close()
            self.assertNotEqual(mode, "wal")
            self.assertFalse(bak.with_name(bak.name + "-wal").exists())
            self.assertFalse(bak.with_name(bak.name + "-shm").exists())
        finally:
            os.environ.pop("HIPPO_BACKUP", None)

    def test_snapshot_is_not_wal(self):
        # Same requirement for the pre-dream rollback point: restoring it must
        # not depend on a sidecar that a later reader happened to leave behind.
        snap = Path(self.dir.name) / "backup" / "hippo.pre-dream.db"
        cli.ensure_store()
        cli.main(["add", "--kind", "episodic", "--title", "X", "--dedup-key", "x"])
        conn = store.connect(self.db)
        cli._snapshot(conn, snap)
        conn.close()
        out = sqlite3.connect(str(snap))
        mode = out.execute("PRAGMA journal_mode").fetchone()[0]
        out.execute("SELECT count(*) FROM memories").fetchone()
        out.close()
        self.assertNotEqual(mode, "wal")
        self.assertFalse(snap.with_name(snap.name + "-wal").exists())
        self.assertFalse(snap.with_name(snap.name + "-shm").exists())

    def test_rollback_restores_pre_dream_snapshot(self):
        snap = Path(self.dir.name) / "backup" / "hippo.pre-dream.db"
        os.environ["HIPPO_ROLLBACK"] = str(snap)
        try:
            cli.ensure_store()
            cli.main(["add", "--kind", "episodic", "--title", "Before", "--dedup-key", "a"])
            # take the pre-apply snapshot (what cmd_dream does before mutating)
            conn = store.connect(self.db)
            cli._snapshot(conn, snap)
            conn.close()
            # mutate after the snapshot
            cli.main(["add", "--kind", "episodic", "--title", "After", "--dedup-key", "b"])
            conn = store.connect(self.db)
            self.assertEqual(conn.execute("SELECT count(*) FROM memories").fetchone()[0], 2)
            conn.close()
            # rollback discards the post-snapshot mutation
            self.assertEqual(cli.main(["rollback"]), 0)
            conn = store.connect(self.db)
            titles = [r["title"] for r in conn.execute("SELECT title FROM memories")]
            conn.close()
            self.assertEqual(titles, ["Before"])
        finally:
            os.environ.pop("HIPPO_ROLLBACK", None)

    def test_boost_subcommand_sets_weight_and_rescores(self):
        cli.ensure_store()
        cli.main(["add", "--kind", "project", "--title", "Scoreboard",
                  "--dedup-key", "sb"])
        conn = store.connect(self.db)
        mid = conn.execute(
            "SELECT id FROM memories WHERE dedup_key='sb'"
        ).fetchone()["id"]
        conn.close()
        self.assertEqual(cli.main(["boost", str(mid), "1.0"]), 0)
        conn = store.connect(self.db)
        row = conn.execute(
            "SELECT weight, salience FROM memories WHERE id=?", (mid,)
        ).fetchone()
        conn.close()
        self.assertEqual(row["weight"], 1.0)
        self.assertGreater(row["salience"], 0.0)

    def test_boost_subcommand_defaults_weight_to_one(self):
        cli.ensure_store()
        cli.main(["add", "--kind", "episodic", "--title", "T", "--dedup-key", "d"])
        conn = store.connect(self.db)
        mid = conn.execute("SELECT id FROM memories WHERE dedup_key='d'").fetchone()["id"]
        conn.close()
        self.assertEqual(cli.main(["boost", str(mid)]), 0)
        conn = store.connect(self.db)
        w = conn.execute("SELECT weight FROM memories WHERE id=?", (mid,)).fetchone()["weight"]
        conn.close()
        self.assertEqual(w, 1.0)

    def test_corrections_scan_subcommand_prints_pushback(self):
        sess = Path(self.dir.name) / "s.jsonl"
        sess.write_text(
            json.dumps({"type": "user", "message": {"content": "run daily note"}}) + "\n"
            + json.dumps({"type": "user",
                          "message": {"content": "you got this wrong, that is incorrect"}}) + "\n"
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["corrections-scan", str(sess)])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("CORRECTIONS", out)
        self.assertIn("L2", out)
        self.assertNotIn("L1", out)  # the routine command must not be flagged

    def test_show_prints_full_body_by_id(self):
        cli.ensure_store()
        cli.main(["add", "--kind", "interest", "--title", "Rising stress",
                  "--body", "work expectations plus tool friction",
                  "--dedup-key", "s"])
        conn = store.connect(self.db)
        mid = conn.execute("SELECT id FROM memories WHERE dedup_key='s'").fetchone()["id"]
        conn.close()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["show", str(mid)])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn(f"[{mid}]", out)
        self.assertIn("work expectations plus tool friction", out)

    def test_show_unknown_id_returns_nonzero(self):
        cli.ensure_store()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["show", "999"])
        self.assertEqual(rc, 1)
        self.assertIn("999", buf.getvalue())


class RecallCmdTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Path(self.dir.name) / "hippo.db"
        os.environ["HIPPO_DB"] = str(self.db)

    def tearDown(self):
        os.environ.pop("HIPPO_DB", None)
        self.dir.cleanup()

    def test_recall_prints_body_of_top_semantic_match(self):
        cli.ensure_store()
        cli.main(["add", "--kind", "interest", "--title", "Rising stress",
                  "--body", "work expectations plus tool friction",
                  "--dedup-key", "s"])

        class Stub:
            # index_hippo embeds "title\nbody"; cmd_recall embeds the query.
            table = {"Rising stress\nwork expectations plus tool friction": [1.0, 0.0],
                     "stress": [1.0, 0.0]}

            def embed(self, texts):
                return [self.table[t] for t in texts]

        orig = embed.get_embedder
        embed.get_embedder = lambda model=None: Stub()
        try:
            cli.main(["index", "--hippo"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["recall", "stress"])
        finally:
            embed.get_embedder = orig
        self.assertEqual(rc, 0)
        self.assertIn("work expectations plus tool friction", buf.getvalue())

    def test_recall_no_match_returns_nonzero(self):
        cli.ensure_store()

        class Stub:
            def embed(self, texts):
                return [[1.0, 0.0] for _ in texts]

        orig = embed.get_embedder
        embed.get_embedder = lambda model=None: Stub()
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["recall", "anything"])
        finally:
            embed.get_embedder = orig
        self.assertEqual(rc, 1)


class WorkingSetCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "hippo.db")
        os.environ["HIPPO_DB"] = self.db
        os.environ["HIPPO_JOURNAL"] = os.path.join(self.tmp.name, "journal.md")
        conn = store.connect(self.db)
        store.init_schema(conn)
        mid = store.upsert_memory(conn, "episodic", "ship the SessionStart hook")
        conn.execute("UPDATE memories SET salience=9.0 WHERE id=?", (mid,))
        conn.commit()
        conn.close()

    def tearDown(self):
        os.environ.pop("HIPPO_DB", None)
        os.environ.pop("HIPPO_JOURNAL", None)
        self.tmp.cleanup()

    def test_working_set_prints_block(self):
        rc = cli.main(["working-set"])
        self.assertEqual(rc, 0)


class CheckCmdTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["HIPPO_DB"] = str(Path(self.dir.name) / "hippo.db")

    def tearDown(self):
        os.environ.pop("HIPPO_DB", None)
        os.environ.pop("HIPPO_RECALL_PROBES", None)
        self.dir.cleanup()

    def test_check_runs_probes_and_returns_exit_code(self):
        probes = Path(self.dir.name) / "probes.json"
        probes.write_text(json.dumps(
            [{"query": "recall work", "expect": "ship semantic recall", "source": "hippo"}]
        ))
        os.environ["HIPPO_RECALL_PROBES"] = str(probes)
        cli.ensure_store()
        cli.main(["add", "--kind", "episodic", "--title", "ship semantic recall",
                  "--dedup-key", "a"])

        class Stub:
            # index_hippo embeds "title\n"; cmd_check embeds the probe query.
            table = {"ship semantic recall\n": [1.0, 0.0], "recall work": [1.0, 0.0]}

            def embed(self, texts):
                return [self.table[t] for t in texts]

        orig = embed.get_embedder
        embed.get_embedder = lambda model=None: Stub()
        try:
            cli.main(["index", "--hippo"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["check"])
        finally:
            embed.get_embedder = orig
        self.assertEqual(rc, 0)
        self.assertIn("PASS", buf.getvalue())
        self.assertIn("total 1/1", buf.getvalue())


class SearchFallbackCmdTest(unittest.TestCase):
    """cmd_search must degrade to search.lexical, not trace, when the
    embedding model is unavailable (Review Focus 4)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["HIPPO_DB"] = str(Path(self.dir.name) / "hippo.db")

    def tearDown(self):
        os.environ.pop("HIPPO_DB", None)
        self.dir.cleanup()

    def test_search_falls_back_to_lexical_without_model(self):
        cli.ensure_store()
        cli.main(["add", "--kind", "project", "--title", "Router firmware rollback plan",
                  "--dedup-key", "k1"])
        cli.main(["add", "--kind", "interest", "--title", "Sourdough starter",
                  "--dedup-key", "k2"])

        def boom(model):
            raise embed.EmbedderUnavailable("no model")

        orig = embed.get_embedder
        embed.get_embedder = boom
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["search", "router firmware"])
        finally:
            embed.get_embedder = orig
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("lexical", out.lower())
        self.assertIn("Router firmware rollback plan", out)
        self.assertNotIn("Sourdough starter", out)


class DigestCmdTest(unittest.TestCase):
    """cmd_digest (cli.py) reconstructs an argv list for digest.run from the
    parsed Namespace, since digest.run does its own argparse internally and
    cannot take a Namespace directly. Cover every flag cmd_digest forwards
    (target, --list, --days, --out) so a dropped one fails a test here
    rather than surfacing later as a silently-ignored CLI flag."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.sessions = Path(self.dir.name) / "sessions"
        self.sessions.mkdir()
        os.environ["HIPPO_DB"] = str(Path(self.dir.name) / "hippo.db")
        os.environ["HIPPO_SESSIONS_DIR"] = str(self.sessions)
        from hippo_memory import config
        config.SESSIONS_DIR = self.sessions
        self.session_path = self.sessions / "digest-fixture-0001.jsonl"
        shutil.copy(FIXTURES / "digest_sample.jsonl", self.session_path)

    def tearDown(self):
        os.environ.pop("HIPPO_DB", None)
        os.environ.pop("HIPPO_SESSIONS_DIR", None)
        self.dir.cleanup()

    def test_digest_list_flag_lists_the_session(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["digest", "--list"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("SESSION INDEX", out)
        self.assertIn("digest-f", out)  # session id, truncated to 8 chars

    def test_digest_target_prints_content(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["digest", "digest-fixture-0001"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("please summarize the deployment logs from last night", out)

    def test_digest_days_flag_narrows_the_window(self):
        old = time.time() - 10 * 86400
        os.utime(self.session_path, (old, old))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["digest", "--list", "--days", "1"])
        self.assertEqual(rc, 0)
        self.assertIn("(none)", buf.getvalue())
        # widening the window with the same flag picks the session back up,
        # so this is exercising --days, not a broken fixture.
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            rc2 = cli.main(["digest", "--list", "--days", "30"])
        self.assertEqual(rc2, 0)
        self.assertIn("digest-f", buf2.getvalue())

    def test_digest_out_flag_writes_a_file_instead_of_stdout(self):
        outfile = Path(self.dir.name) / "report.txt"
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["digest", "digest-fixture-0001", "--out", str(outfile)])
        self.assertEqual(rc, 0)
        self.assertIn("digest written to", buf.getvalue())
        self.assertTrue(outfile.exists())
        self.assertIn("please summarize the deployment logs from last night",
                      outfile.read_text())


class HandoffCliTest(unittest.TestCase):
    """hippo handoff add/list/close/latest."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Path(self.dir.name) / "hippo.db"
        self.sess = Path(self.dir.name) / "current-session"
        os.environ["HIPPO_DB"] = str(self.db)
        os.environ["HIPPO_CURRENT_SESSION"] = str(self.sess)
        cli.ensure_store()
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["add", "--kind", "episodic", "--title", "a loop", "--dedup-key", "l"])
        self.loop = int(out.getvalue().strip())

    def tearDown(self):
        os.environ.pop("HIPPO_DB", None)
        os.environ.pop("HIPPO_CURRENT_SESSION", None)
        self.dir.cleanup()

    def _run(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.main(argv)
        return rc, out.getvalue()

    def test_refuses_without_session(self):
        rc, out = self._run(["handoff", "add", "--title", "t", "--body", "State: x"])
        self.assertEqual(rc, 1)
        self.assertIn("session", out.lower())
        conn = store.connect(self.db)
        self.assertEqual(conn.execute("SELECT count(*) FROM memories WHERE kind='handoff'").fetchone()[0], 0)
        conn.close()

    def test_add_reads_session_file_links_and_prints_projection(self):
        self.sess.write_text("sess-9\n/tmp/x.jsonl\n")
        rc, out = self._run(["handoff", "add", "--title", "router: parser done",
                             "--body", "State: parser done\nNext: verify cron",
                             "--carries", str(self.loop)])
        self.assertEqual(rc, 0)
        self.assertIn("## Notes to self", out)
        self.assertIn("router: parser done", out)
        self.assertIn(f"(carries {self.loop})", out)
        conn = store.connect(self.db)
        row = conn.execute("SELECT * FROM memories WHERE kind='handoff'").fetchone()
        self.assertEqual(row["dedup_key"], "handoff:sess-9:1")
        self.assertEqual(row["salience"], 0.0)
        link = conn.execute("SELECT kind FROM links WHERE from_id=? AND to_id=?",
                            (row["id"], self.loop)).fetchone()
        self.assertEqual(link["kind"], "carries")
        conn.close()

    def test_explicit_session_flag(self):
        rc, _ = self._run(["handoff", "add", "--session", "s2", "--title", "t",
                           "--body", "State: x"])
        self.assertEqual(rc, 0)
        rc, out = self._run(["handoff", "latest", "--session", "s2"])
        self.assertEqual(rc, 0)
        self.assertIn("State: x", out)

    def test_rejects_unknown_label_and_cap(self):
        rc, out = self._run(["handoff", "add", "--session", "s", "--title", "t",
                             "--body", "Mood: great"])
        self.assertEqual(rc, 1)
        self.assertIn("Mood", out)
        from hippo_memory import config
        rc, out = self._run(["handoff", "add", "--session", "s", "--title", "t",
                             "--body", "State: " + "x" * (config.HANDOFF_BODY_MAX + 100)])
        self.assertEqual(rc, 1)
        self.assertIn(str(config.HANDOFF_BODY_MAX), out)

    def test_list_close_latest(self):
        self._run(["handoff", "add", "--session", "s", "--title", "one", "--body", "State: 1"])
        self._run(["handoff", "add", "--session", "s", "--title", "two", "--body", "State: 2"])
        rc, out = self._run(["handoff", "list", "--session", "s"])
        self.assertEqual(rc, 0)
        self.assertIn("one", out)
        self.assertIn("two", out)
        rc, out = self._run(["handoff", "latest", "--session", "s"])
        self.assertIn("two", out)
        conn = store.connect(self.db)
        two = conn.execute("SELECT id FROM memories WHERE title='two'").fetchone()["id"]
        conn.close()
        rc, _ = self._run(["handoff", "close", str(two)])
        self.assertEqual(rc, 0)
        rc, out = self._run(["handoff", "latest", "--session", "s"])
        self.assertIn("one", out)
        self.assertNotIn("two", out)
        rc, out = self._run(["handoff", "list", "--session", "s"])
        self.assertNotIn("two", out)
        rc, out = self._run(["handoff", "list", "--session", "s", "--all"])
        self.assertIn("two", out)

    def test_latest_none(self):
        rc, out = self._run(["handoff", "latest", "--session", "none"])
        self.assertEqual(rc, 1)


def test_removed_subcommands_are_gone():
    from hippo_memory import cli
    p = cli.build_parser()
    names = set(p._subparsers._group_actions[0].choices)
    for gone in ("guard-dismiss", "guard-candidates", "verdicts-scan"):
        assert gone not in names
    for kept in ("dream", "hook", "init", "doctor", "uninstall", "digest", "recall"):
        assert kept in names


def test_dream_with_no_activity_touches_nothing(tmp_path, capsys):
    from hippo_memory import cli, config
    cli.ensure_store()
    rc = cli.main(["dream"])
    assert rc == 0
    assert "no new session activity" in capsys.readouterr().out
    assert not config.HIPPO_ROLLBACK_PATH.exists()


def test_recall_without_model_exits_two_with_message(monkeypatch, capsys):
    from hippo_memory import cli, embed
    cli.ensure_store()
    def boom():
        raise embed.EmbedderUnavailable("no model")
    monkeypatch.setattr(embed, "get_embedder", boom)
    assert cli.main(["recall", "anything"]) == 2
    assert "hippo-memory[recall]" in capsys.readouterr().err


if __name__ == "__main__":
    unittest.main()
