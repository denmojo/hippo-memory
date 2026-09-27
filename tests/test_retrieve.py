import unittest

from hippo_memory import retrieve


class RetrieveTest(unittest.TestCase):
    def test_pack_unpack_roundtrip(self):
        v = [0.5, -1.0, 2.25]
        self.assertEqual(retrieve.unpack(retrieve.pack(v)), v)

    def test_cosine_identicals_is_one_orthogonal_is_zero(self):
        self.assertAlmostEqual(retrieve.cosine([1, 0], [1, 0]), 1.0, places=6)
        self.assertAlmostEqual(retrieve.cosine([1, 0], [0, 1]), 0.0, places=6)

    def test_cosine_zero_vector_is_zero(self):
        self.assertEqual(retrieve.cosine([0, 0], [1, 1]), 0.0)

    def test_top_k_orders_by_similarity(self):
        q = [1.0, 0.0]
        cands = [("a", [0.0, 1.0]), ("b", [1.0, 0.1]), ("c", [0.5, 0.5])]
        ranked = retrieve.top_k(q, cands, k=2)
        self.assertEqual([r[0] for r in ranked], ["b", "c"])
        self.assertGreater(ranked[0][1], ranked[1][1])

    def test_minmax_norm_handles_flat_and_spread(self):
        self.assertEqual(retrieve.minmax_norm([5.0, 5.0]), [0.0, 0.0])
        self.assertEqual(retrieve.minmax_norm([0.0, 1.0, 2.0]), [0.0, 0.5, 1.0])
        self.assertEqual(retrieve.minmax_norm([]), [])


if __name__ == "__main__":
    unittest.main()
