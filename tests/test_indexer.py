import tempfile
import unittest
from pathlib import Path

from hippo_memory import embed, indexer, store


class IndexerTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)
        self.emb = embed.FakeEmbedder(dim=8)
        self.dir = tempfile.TemporaryDirectory()
        self.mem = Path(self.dir.name)
        (self.mem / "feedback_x.md").write_text("avoid em dashes in prose")
        (self.mem / "MEMORY.md").write_text("# index\n")

    def tearDown(self):
        self.conn.close()
        self.dir.cleanup()

    def test_index_hippo_never_re_embeds_a_folded_row(self):
        """Without this the next dream's indexing pass resurrects the vector of
        every row we just folded, and the duplicate walks back into recall."""
        keep = store.upsert_memory(self.conn, "episodic", "keep me", dedup_key="k")
        gone = store.upsert_memory(self.conn, "episodic", "folded dup", dedup_key="g")
        store.fold_memory(self.conn, gone, into="stock_rule.md")
        n = indexer.index_hippo(self.conn, self.emb, model="fake")
        self.assertEqual(n, 1)
        self.assertIsNotNone(store.get_embedding(self.conn, "hippo", str(keep)))
        self.assertIsNone(store.get_embedding(self.conn, "hippo", str(gone)))

    def test_index_stock_skips_memory_md_and_embeds_topic_files(self):
        n = indexer.index_stock(self.conn, self.mem, self.emb, model="fake")
        self.assertEqual(n, 1)
        rows = store.get_embeddings(self.conn, source="stock")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ref"], str(self.mem / "feedback_x.md"))
        self.assertEqual(rows[0]["dim"], 8)

    def test_reindex_is_idempotent_when_unchanged(self):
        indexer.index_stock(self.conn, self.mem, self.emb, model="fake")
        added = indexer.index_stock(self.conn, self.mem, self.emb, model="fake")
        self.assertEqual(added, 0)

    def test_model_change_forces_reembed(self):
        indexer.index_stock(self.conn, self.mem, self.emb, model="fake")
        added = indexer.index_stock(self.conn, self.mem, self.emb, model="fake-v2")
        self.assertEqual(added, 1)

    def test_index_hippo_embeds_rows(self):
        store.upsert_memory(self.conn, "episodic", "Ship recall", dedup_key="k")
        n = indexer.index_hippo(self.conn, self.emb, model="fake")
        self.assertEqual(n, 1)
        self.assertEqual(len(store.get_embeddings(self.conn, source="hippo")), 1)

    def test_index_hippo_is_idempotent_when_the_body_is_unchanged(self):
        store.upsert_memory(self.conn, "episodic", "Ship recall", dedup_key="k")
        indexer.index_hippo(self.conn, self.emb, model="fake")
        self.assertEqual(indexer.index_hippo(self.conn, self.emb, model="fake"), 0)

    def test_editing_a_body_re_embeds_the_row(self):
        """Freshness keyed on the model name alone let an edited row keep its old
        vector, so the memory stayed retrievable by what it used to say rather
        than by what it now says."""
        mid = store.upsert_memory(self.conn, "episodic", "Ship recall",
                                  body="original", dedup_key="k")
        indexer.index_hippo(self.conn, self.emb, model="fake")
        before = store.get_embedding(self.conn, "hippo", str(mid))["vec"]

        self.conn.execute("UPDATE memories SET body=? WHERE id=?",
                          ("rewritten entirely", mid))
        self.conn.commit()

        self.assertEqual(indexer.index_hippo(self.conn, self.emb, model="fake"), 1)
        after = store.get_embedding(self.conn, "hippo", str(mid))["vec"]
        self.assertNotEqual(before, after, "the vector still reflects the old body")

    def test_editing_a_title_re_embeds_the_row(self):
        mid = store.upsert_memory(self.conn, "episodic", "old title", dedup_key="k")
        indexer.index_hippo(self.conn, self.emb, model="fake")
        self.conn.execute("UPDATE memories SET title=? WHERE id=?", ("new title", mid))
        self.conn.commit()
        self.assertEqual(indexer.index_hippo(self.conn, self.emb, model="fake"), 1)

    def test_stale_hippo_counts_rows_whose_vector_drifted_from_their_text(self):
        mid = store.upsert_memory(self.conn, "episodic", "t", body="original",
                                  dedup_key="k")
        indexer.index_hippo(self.conn, self.emb, model="fake")
        self.assertEqual(indexer.stale_hippo(self.conn, model="fake"), 0)
        self.conn.execute("UPDATE memories SET body=? WHERE id=?", ("changed", mid))
        self.conn.commit()
        self.assertEqual(indexer.stale_hippo(self.conn, model="fake"), 1)

    def test_stale_hippo_ignores_folded_rows(self):
        mid = store.upsert_memory(self.conn, "episodic", "dup", dedup_key="d")
        indexer.index_hippo(self.conn, self.emb, model="fake")
        store.fold_memory(self.conn, mid, into="stock_rule.md", note="superseded")
        self.assertEqual(indexer.stale_hippo(self.conn, model="fake"), 0)


if __name__ == "__main__":
    unittest.main()
