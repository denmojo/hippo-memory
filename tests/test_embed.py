import unittest

from hippo_memory import embed


class FakeEmbedderTest(unittest.TestCase):
    def test_deterministic_and_fixed_dim(self):
        e = embed.FakeEmbedder(dim=8)
        a1 = e.embed(["hello"])[0]
        a2 = e.embed(["hello"])[0]
        self.assertEqual(a1, a2)
        self.assertEqual(len(a1), 8)

    def test_distinct_texts_differ(self):
        e = embed.FakeEmbedder(dim=8)
        self.assertNotEqual(e.embed(["x"])[0], e.embed(["y"])[0])

    def test_batch_returns_one_vector_per_text(self):
        e = embed.FakeEmbedder(dim=4)
        out = e.embed(["a", "b", "c"])
        self.assertEqual(len(out), 3)
        self.assertTrue(all(len(v) == 4 for v in out))


if __name__ == "__main__":
    unittest.main()
