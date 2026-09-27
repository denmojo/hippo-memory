import tempfile
import unittest
from pathlib import Path

from hippo_memory import journal


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "Journal.md"
        self.archive = Path(self.dir.name) / "Archive.md"

    def tearDown(self):
        self.dir.cleanup()

    def test_render_entry_shape(self):
        entry = journal.render_entry("2026-06-08", "08:15", ["did X", "promoted Y"])
        self.assertTrue(entry.startswith("## 2026-06-08"))
        self.assertIn("_run 08:15_", entry)
        self.assertIn("- did X", entry)

    def test_add_puts_newest_day_on_top(self):
        journal.add_entry(self.path, "2026-06-07", "09:00", ["older"])
        journal.add_entry(self.path, "2026-06-08", "09:00", ["newer"])
        text = self.path.read_text()
        self.assertTrue(text.startswith("# Memory Dream Journal"))
        self.assertLess(text.index("2026-06-08"), text.index("2026-06-07"))

    def test_same_day_runs_share_one_heading(self):
        journal.add_entry(self.path, "2026-06-08", "08:15", ["first run"])
        journal.add_entry(self.path, "2026-06-08", "21:49", ["second run"])
        text = self.path.read_text()
        # exactly one date heading for the day
        self.assertEqual(text.count("## 2026-06-08"), 1)
        # both runs present, labelled, oldest-first within the day
        self.assertIn("_run 08:15_", text)
        self.assertIn("_run 21:49_", text)
        self.assertLess(text.index("_run 08:15_"), text.index("_run 21:49_"))
        self.assertLess(text.index("first run"), text.index("second run"))

    def test_archive_keeps_newest_n(self):
        for d in ["01", "02", "03"]:
            journal.add_entry(self.path, f"2026-06-{d}", "09:00", [f"entry {d}"])
        journal.archive_old(self.path, keep_n=2, archive_path=self.archive)
        active = self.path.read_text()
        self.assertNotIn("2026-06-01", active)
        self.assertIn("2026-06-03", active)
        self.assertIn("2026-06-01", self.archive.read_text())


if __name__ == "__main__":
    unittest.main()
