import json
import os
import tempfile
import unittest

from hippo_memory import config, crossdedup, dream, retrieve, store

MODEL = "test-model"


class StubEmbedder:
    """Hand-set vectors keyed by exact text; unknown texts get the default,
    orthogonal to everything seeded, so only deliberate matches divert."""

    def __init__(self, table, default=(0.0, 0.0, 1.0)):
        self.table = table
        self.default = list(default)

    def embed(self, texts):
        return [list(self.table.get(t, self.default)) for t in texts]


def _seed_stock(conn, ref, vec, model=MODEL):
    store.upsert_embedding(conn, "stock", ref, len(vec),
                           retrieve.pack(vec), model, None)


def _text(add):
    return f"{add['title']}\n{add.get('body') or ''}"


class DivertTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_satellite_diverted_original_kept(self):
        _seed_stock(self.conn, "/stock/feedback_example.md", [1.0, 0.0, 0.0])
        satellite = {"kind": "episodic", "title": "no appeasing replies",
                     "body": "acknowledge and fix"}
        fresh = {"kind": "interest", "title": "dual-sport motorcycles", "body": ""}
        emb = StubEmbedder({
            _text(satellite): [0.99, 0.1, 0.0],   # cosine ~0.99 vs stock
            _text(fresh): [0.0, 1.0, 0.0],        # orthogonal
        })
        kept, div = crossdedup.divert(
            self.conn, [satellite, fresh], emb, MODEL, 0.80)
        self.assertEqual(kept, [fresh])
        self.assertEqual(len(div), 1)
        self.assertEqual(div[0]["ref"], "/stock/feedback_example.md")
        self.assertGreaterEqual(div[0]["cosine"], 0.80)

    def test_below_threshold_kept(self):
        _seed_stock(self.conn, "/stock/rule.md", [1.0, 0.0, 0.0])
        add = {"kind": "episodic", "title": "related but distinct", "body": "b"}
        emb = StubEmbedder({_text(add): [0.5, 0.5, 0.5]})  # cosine ~0.58
        kept, div = crossdedup.divert(self.conn, [add], emb, MODEL, 0.80)
        self.assertEqual(kept, [add])
        self.assertEqual(div, [])

    def test_no_stock_vectors_fails_open(self):
        add = {"kind": "episodic", "title": "anything", "body": ""}
        emb = StubEmbedder({})
        kept, div = crossdedup.divert(self.conn, [add], emb, MODEL, 0.80)
        self.assertEqual(kept, [add])
        self.assertEqual(div, [])

    def test_other_model_vectors_ignored(self):
        _seed_stock(self.conn, "/stock/rule.md", [1.0, 0.0, 0.0], model="old-model")
        add = {"kind": "episodic", "title": "would match", "body": ""}
        emb = StubEmbedder({_text(add): [1.0, 0.0, 0.0]})
        kept, div = crossdedup.divert(self.conn, [add], emb, MODEL, 0.80)
        self.assertEqual(kept, [add])
        self.assertEqual(div, [])

    def test_hippo_source_vectors_never_compared(self):
        # Within-store matching against other hippo rows is a separate pass;
        # this one reads stock vectors only.
        store.upsert_embedding(self.conn, "hippo", "7", 3,
                               retrieve.pack([1.0, 0.0, 0.0]), MODEL, None)
        add = {"kind": "episodic", "title": "matches a hippo row", "body": ""}
        emb = StubEmbedder({_text(add): [1.0, 0.0, 0.0]})
        kept, div = crossdedup.divert(self.conn, [add], emb, MODEL, 0.80)
        self.assertEqual(kept, [add])
        self.assertEqual(div, [])


class RunDreamDedupTest(unittest.TestCase):
    """The pass wired into run_dream: a synthetic near-duplicate produces a
    Journal merge-candidate instead of a new row."""

    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)
        self._orig_model = config.EMBED_MODEL
        config.EMBED_MODEL = MODEL

    def tearDown(self):
        config.EMBED_MODEL = self._orig_model
        self.conn.close()

    def _run(self, plan, embedder):
        raw = json.dumps(plan)
        with tempfile.TemporaryDirectory() as d:
            jp = os.path.join(d, "journal.md")
            summary = dream.run_dream(
                self.conn, "material", invoke_fn=lambda p: raw,
                date="2026-07-12", journal_path=jp, embedder=embedder,
            )
            journal_text = open(jp).read() if os.path.exists(jp) else ""
        return summary, journal_text

    def test_near_duplicate_becomes_journal_merge_candidate(self):
        _seed_stock(self.conn, "/stock/feedback_example.md", [1.0, 0.0, 0.0])
        satellite = {"kind": "episodic", "title": "never flatter",
                     "body": "no performed contrition", "dedup_key": "sat"}
        fresh = {"kind": "interest", "title": "grid backup power",
                 "body": "", "dedup_key": "grid"}
        emb = StubEmbedder({
            _text(satellite): [0.98, 0.05, 0.0],
            _text(fresh): [0.0, 1.0, 0.0],
        })
        plan = {"adds": [satellite, fresh], "journal": ["promoted: stuff"]}

        summary, journal_text = self._run(plan, emb)

        self.assertEqual(summary["diverted"], 1)
        self.assertEqual(summary["added"], 1)
        rows = self.conn.execute("SELECT dedup_key FROM memories").fetchall()
        self.assertEqual([r["dedup_key"] for r in rows], ["grid"])
        self.assertIn("merge-candidate", journal_text)
        self.assertIn("feedback_example.md", journal_text)
        self.assertIn("never flatter", journal_text)

    def test_no_stock_vectors_skips_without_embedder(self):
        # No stock rows in the embeddings table: the pass must not even need
        # an embedder (None here would otherwise attempt the ONNX load).
        plan = {"adds": [{"kind": "interest", "title": "kept as-is"}],
                "journal": ["promoted: one"]}
        summary, _ = self._run(plan, None)
        self.assertEqual(summary["diverted"], 0)
        self.assertEqual(summary["added"], 1)

    def test_diversion_only_plan_still_writes_journal(self):
        # Every diverted add must leave a reviewable trace even when the model
        # proposed no journal lines of its own.
        _seed_stock(self.conn, "/stock/rule.md", [1.0, 0.0, 0.0])
        satellite = {"kind": "episodic", "title": "paraphrase", "body": ""}
        emb = StubEmbedder({_text(satellite): [1.0, 0.0, 0.0]})
        plan = {"adds": [satellite]}
        summary, journal_text = self._run(plan, emb)
        self.assertEqual(summary["diverted"], 1)
        self.assertEqual(summary["added"], 0)
        self.assertIn("merge-candidate", journal_text)


if __name__ == "__main__":
    unittest.main()
