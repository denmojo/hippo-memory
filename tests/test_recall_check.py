import json
import tempfile
import unittest
from pathlib import Path

from hippo_memory import recall_check


def _hit(source, title, ref=None, rel=0.7):
    return {"source": source, "title": title, "ref": ref or title,
            "relevance": rel, "salience": 0.0, "blended": rel}


class LoadProbesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def _write(self, obj):
        p = Path(self.dir.name) / "probes.json"
        p.write_text(json.dumps(obj))
        return str(p)

    def test_loads_valid_probes(self):
        path = self._write([
            {"query": "generic feedback", "expect": "feedback_example.md", "source": "stock"},
            {"query": "credit card", "expect": "Credit-card optimization", "source": "hippo"},
        ])
        probes = recall_check.load_probes(path)
        self.assertEqual(len(probes), 2)
        self.assertEqual(probes[0]["source"], "stock")

    def test_rejects_missing_field(self):
        path = self._write([{"query": "x", "source": "stock"}])
        with self.assertRaises(ValueError):
            recall_check.load_probes(path)

    def test_rejects_bad_source(self):
        path = self._write([{"query": "x", "expect": "y", "source": "vault"}])
        with self.assertRaises(ValueError):
            recall_check.load_probes(path)

    def test_rejects_non_list(self):
        path = self._write({"query": "x", "expect": "y", "source": "stock"})
        with self.assertRaises(ValueError):
            recall_check.load_probes(path)


class EvaluateTest(unittest.TestCase):
    def test_pass_when_rank1_matches_stock(self):
        probe = {"query": "q", "expect": "feedback_example.md", "source": "stock"}
        hits = [_hit("stock", "feedback_example.md")]
        r = recall_check.evaluate(probe, hits)
        self.assertTrue(r["passed"])
        self.assertEqual(r["expected_rank"], 1)

    def test_hippo_match_is_case_insensitive_substring(self):
        probe = {"query": "q", "expect": "Credit-card optimization", "source": "hippo"}
        hits = [_hit("hippo", "Credit-card optimization sprint, hard deadline 6/19")]
        self.assertTrue(recall_check.evaluate(probe, hits)["passed"])

    def test_fail_when_wrong_source_at_rank1(self):
        probe = {"query": "q", "expect": "feedback_example.md", "source": "stock"}
        hits = [_hit("hippo", "Workplace reputation"),
                _hit("stock", "feedback_example.md")]
        r = recall_check.evaluate(probe, hits)
        self.assertFalse(r["passed"])
        self.assertEqual(r["expected_rank"], 2)

    def test_fail_when_expected_absent(self):
        probe = {"query": "q", "expect": "feedback_example.md", "source": "stock"}
        hits = [_hit("hippo", "Workplace reputation")]
        r = recall_check.evaluate(probe, hits)
        self.assertFalse(r["passed"])
        self.assertIsNone(r["expected_rank"])

    def test_fail_on_empty_hits(self):
        probe = {"query": "q", "expect": "x", "source": "hippo"}
        r = recall_check.evaluate(probe, [])
        self.assertFalse(r["passed"])
        self.assertIsNone(r["top"])


class RenderTest(unittest.TestCase):
    def _results(self):
        return [
            recall_check.evaluate(
                {"query": "generic feedback please", "expect": "feedback_example.md",
                 "source": "stock"},
                [_hit("stock", "feedback_example.md", rel=0.704)],
            ),
            recall_check.evaluate(
                {"query": "credit card optimization deadline",
                 "expect": "Credit-card optimization", "source": "hippo"},
                [_hit("stock", "feedback_complete.md", rel=0.62),
                 _hit("hippo", "Weekend errand list"),
                 _hit("hippo", "Credit-card optimization sprint")],
            ),
        ]

    def test_render_marks_pass_and_fail(self):
        out = recall_check.render(self._results())
        self.assertIn("PASS", out)
        self.assertIn("FAIL", out)
        self.assertIn("rank 3", out)        # expected item's rank on failure
        self.assertIn("stock 1/1", out)
        self.assertIn("hippo 0/1", out)
        self.assertIn("total 1/2", out)

    def test_exit_code_zero_only_when_all_pass(self):
        self.assertEqual(recall_check.exit_code(self._results()), 1)
        all_pass = [recall_check.evaluate(
            {"query": "q", "expect": "feedback_x.md", "source": "stock"},
            [_hit("stock", "feedback_x.md")])]
        self.assertEqual(recall_check.exit_code(all_pass), 0)


class CoverageTest(unittest.TestCase):
    """The check passed 7/7 while 157 of 233 rows had no vector, because every
    probe happened to point at an older embedded row. Probes sample; they cannot
    detect a hole they never touch. Coverage is asserted directly."""

    def _all_pass(self):
        return [recall_check.evaluate(
            {"query": "q", "expect": "feedback_x.md", "source": "stock"},
            [_hit("stock", "feedback_x.md")])]

    def test_unembedded_rows_fail_the_check_even_when_every_probe_passes(self):
        self.assertEqual(recall_check.exit_code(self._all_pass(), unembedded=3), 1)

    def test_full_coverage_keeps_a_passing_check_passing(self):
        self.assertEqual(recall_check.exit_code(self._all_pass(), unembedded=0), 0)

    def test_coverage_line_reports_the_hole(self):
        line = recall_check.coverage_line(unembedded=157, total=233)
        self.assertIn("FAIL", line)
        self.assertIn("157", line)
        self.assertIn("hippo index", line)

    def test_coverage_line_passes_when_fully_indexed(self):
        line = recall_check.coverage_line(unembedded=0, total=233)
        self.assertIn("PASS", line)
        self.assertIn("233", line)

    def test_drifted_vectors_fail_the_check_even_when_nothing_is_missing(self):
        """A vector that no longer matches an edited body is as unfindable as a
        missing one, so it must fail the same way."""
        self.assertEqual(
            recall_check.exit_code(self._all_pass(), unembedded=0, stale=2), 1)
        line = recall_check.coverage_line(unembedded=0, total=233, stale=2)
        self.assertIn("FAIL", line)
        self.assertIn("drifted", line)


if __name__ == "__main__":
    unittest.main()
