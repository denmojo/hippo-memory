import json
import unittest

from hippo_memory import corrections


def uturn(text):
    return json.dumps({"type": "user", "message": {"content": text}})


def uturn_blocks(*blocks):
    content = [{"type": "text", "text": b} for b in blocks]
    return json.dumps({"type": "user", "message": {"content": content}})


def scan(*raw_lines):
    return corrections.scan_lines(enumerate(raw_lines, start=1))


class CorrectionSignalTest(unittest.TestCase):
    def test_dissatisfaction_fires_when_user_says_assistant_is_wrong(self):
        c = scan(uturn("you get this wrong? the url does NOT point to the brochure."))
        self.assertEqual(len(c), 1)
        self.assertIn("dissatisfaction", c[0]["signals"])

    def test_dissatisfaction_fires_on_you_forgot(self):
        c = scan(uturn("Did you forget the release checklist once again?"))
        self.assertIn("dissatisfaction", c[0]["signals"])

    def test_interrupt_marker_fires(self):
        c = scan(uturn("[Request interrupted by user]"))
        self.assertEqual(len(c), 1)
        self.assertIn("interrupt", c[0]["signals"])

    def test_interrupt_for_tool_use_marker_fires(self):
        c = scan(uturn("[Request interrupted by user for tool use]"))
        self.assertIn("interrupt", c[0]["signals"])

    def test_directive_fires_on_redirect(self):
        c = scan(uturn("Actually, don't do that. From now on, output to console instead."))
        self.assertIn("directive", c[0]["signals"])

    def test_charged_fires_on_profanity(self):
        c = scan(uturn("god dammit don't answer about memory by bloating memory more"))
        self.assertIn("charged", c[0]["signals"])

    def test_charged_fires_on_shouting_run(self):
        # A run of consecutive all-caps words marks a charged correction.
        c = scan(uturn("didn't I just say output to terminal? WHY ARE YOU EDITING"))
        self.assertIn("charged", c[0]["signals"])

    def test_single_acronym_is_not_shouting(self):
        # One acronym (ADR, URL, NOW) is not a shout; must not flag as charged.
        c = scan(uturn("Update the ADR with the URL now please."))
        self.assertEqual(c, [])

    def test_routine_command_is_silent(self):
        self.assertEqual(scan(uturn("run daily note and clear the exports")), [])

    def test_bare_go_is_silent(self):
        self.assertEqual(scan(uturn("go")), [])
        self.assertEqual(scan(uturn("implement")), [])
        self.assertEqual(scan(uturn("yes, do it")), [])

    def test_tool_result_and_wrappers_skipped(self):
        tool_turn = json.dumps({
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "you are wrong"}]},
        })
        wrapper = uturn("<local-command-stdout>WHY ARE YOU dammit</local-command-stdout>")
        self.assertEqual(scan(tool_turn, wrapper), [])

    def test_assistant_turns_are_ignored(self):
        a = json.dumps({"type": "assistant",
                        "message": {"content": [{"type": "text", "text": "you are wrong, dammit"}]}})
        self.assertEqual(scan(a), [])

    def test_inverts_interest_frustration_suppressor(self):
        # The exact turn interest.py SUPPRESSES (frustration at my output) is the
        # turn corrections.py must SURFACE. This is the design's whole point.
        from hippo_memory import interest
        line = uturn("I don't understand, your logic is way out there.")
        self.assertEqual(interest.scan_lines(enumerate([line], start=1)), [])
        self.assertTrue(scan(line))

    def test_regression_short_link_pointer_miss(self):
        # The 2026-06-13 miss: a correction delivered inside a daily-note run that
        # was nearly dropped. corrections-scan must flag it.
        line = uturn(
            "I set you to effort 'xhigh' and you get this wrong? the public "
            "download URL does NOT point to the /docs/ archive, you sure you read right?")
        self.assertTrue(scan(line))

    def test_regression_skimmed_synthesis_miss(self):
        line = uturn("despite the procedural directive to read the entire export, "
                     "you still skimmed and screwed up facts.")
        self.assertTrue(scan(line))

    def test_render_lists_line_and_signals(self):
        c = scan(uturn("Did you forget? WHY ARE YOU doing that"))
        out = corrections.render(c)
        self.assertIn("L1", out)
        self.assertIn("dissatisfaction", out)

    def test_scan_file_parity(self):
        import tempfile, os
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(uturn("you got it backwards, that is wrong") + "\n")
                f.write(uturn("run daily note") + "\n")
            res = corrections.scan_file(path)
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["line"], 1)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
