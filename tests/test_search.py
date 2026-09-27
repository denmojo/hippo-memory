import unittest

from hippo_memory import indexer, retrieve, search, store


class StubEmbedder:
    """Maps exact strings to chosen vectors, so relevance ordering is deterministic."""

    def __init__(self, table):
        self.table = table

    def embed(self, texts):
        return [self.table[t] for t in texts]


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)
        # index_hippo embeds f"{title}\n{body}", so keys include the trailing newline.
        self.emb = StubEmbedder({
            "ship semantic recall\n": [1.0, 0.0, 0.0],
            "buy groceries\n": [0.0, 1.0, 0.0],
            "find the recall work": [0.9, 0.1, 0.0],
            "anything": [0.0, 0.0, 1.0],
        })
        store.upsert_memory(self.conn, "episodic", "ship semantic recall", dedup_key="a")
        store.upsert_memory(self.conn, "episodic", "buy groceries", dedup_key="b")
        store.rescore_all(self.conn)
        indexer.index_hippo(self.conn, self.emb, model="stub")

    def tearDown(self):
        self.conn.close()

    def test_search_returns_blended_ranked_hits(self):
        hits = search.run(self.conn, "find the recall work", self.emb,
                          model="stub", k=2, source="hippo")
        self.assertEqual(hits[0]["title"], "ship semantic recall")
        for key in ("blended", "relevance", "salience", "source", "ref"):
            self.assertIn(key, hits[0])
        # relevance is raw cosine (~0.994 here), not min-max normalized to 1.0
        self.assertGreater(hits[0]["relevance"], 0.9)
        self.assertLess(hits[0]["relevance"], 0.999)

    def test_source_filter_restricts_to_empty_stock(self):
        hits = search.run(self.conn, "anything", self.emb, model="stub", source="stock")
        self.assertEqual(hits, [])

    def test_model_mismatch_excludes_vectors(self):
        hits = search.run(self.conn, "find the recall work", self.emb,
                          model="other-model", source="hippo")
        self.assertEqual(hits, [])


def _unit(rel):
    """Unit vector whose cosine against the unit query [1,0,0] is exactly rel."""
    return [rel, (1 - rel * rel) ** 0.5, 0.0]


class TiebreakCapTest(unittest.TestCase):
    """Salience is a tiebreak: it may flip a near-tie but never a wide relevance
    gap. Reconstructs the 2026-07-11 check-recall regression, where a hot hippo
    paraphrase (rel 0.650, sal 2.83) outranked the more-relevant stock rule it
    extends (rel 0.685)."""

    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)
        self.emb = StubEmbedder({
            "hot paraphrase of the rule\n": _unit(0.650),
            "near tie with the other rule\n": _unit(0.980),
            "the rule query": [1.0, 0.0, 0.0],
        })
        store.upsert_embedding(self.conn, "stock", "/mem/feedback_rule.md", 3,
                               retrieve.pack(_unit(0.685)), "stub", "1")
        store.upsert_embedding(self.conn, "stock", "/mem/feedback_other.md", 3,
                               retrieve.pack(_unit(0.985)), "stub", "1")
        store.upsert_memory(self.conn, "episodic", "hot paraphrase of the rule",
                            dedup_key="hp")
        store.upsert_memory(self.conn, "episodic", "near tie with the other rule",
                            dedup_key="nt")
        self.conn.execute("UPDATE memories SET salience=3.0")
        self.conn.commit()
        indexer.index_hippo(self.conn, self.emb, model="stub")

    def tearDown(self):
        self.conn.close()

    def test_salience_cannot_overturn_real_relevance_gap(self):
        hits = search.run(self.conn, "the rule query", self.emb, model="stub", k=4)
        rank = {h["title"]: i for i, h in enumerate(hits)}
        self.assertLess(rank["feedback_rule.md"], rank["hot paraphrase of the rule"],
                        "a 0.035 relevance gap must not be overturned by salience")

    def test_salience_still_breaks_a_near_tie(self):
        hits = search.run(self.conn, "the rule query", self.emb, model="stub", k=4)
        rank = {h["title"]: i for i, h in enumerate(hits)}
        self.assertLess(rank["near tie with the other rule"], rank["feedback_other.md"],
                        "a live hippo thread should still edge an equal-relevance cold rule")


if __name__ == "__main__":
    unittest.main()
