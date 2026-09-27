import json
import unittest

from hippo_memory import interest


def uturn(text):
    return json.dumps({"type": "user", "message": {"content": text}})


def uturn_blocks(*blocks):
    content = [{"type": "text", "text": b} for b in blocks]
    return json.dumps({"type": "user", "message": {"content": content}})


def scan(*raw_lines):
    return interest.scan_lines(enumerate(raw_lines, start=1))


class InterestSignalTest(unittest.TestCase):
    def test_tangent_fires_on_unprompted_thread(self):
        c = scan(uturn("Another unrelated question: how is Northwind funded?"))
        self.assertEqual(len(c), 1)
        self.assertIn("tangent", c[0]["signals"])

    def test_affect_fires_on_reaction(self):
        c = scan(uturn("As an early adopter I am shocked they would offer this."))
        self.assertEqual(c[0]["signals"], ["affect"])

    def test_curiosity_fires_on_external_subject(self):
        c = scan(uturn("Explain how Northwind's calendar integration avoids lock-in."))
        self.assertIn("curiosity", c[0]["signals"])

    def test_curiosity_suppressed_by_frustration_at_assistant(self):
        # "you"/"way out there" mark a complaint about my output, not interest.
        c = scan(uturn("I don't understand, your logic is way out there. Explain."))
        self.assertEqual(c, [])

    def test_curiosity_suppressed_when_confusion_is_about_my_output(self):
        c = scan(uturn("What is this? That makes no sense, you lost me."))
        self.assertEqual(c, [])

    def test_affect_survives_topic_confusion(self):
        # "confusing" describes the situation, not my output -> still an interest.
        c = scan(uturn("That is confusing indeed. I am shocked by the whole thing."))
        self.assertIn("affect", c[0]["signals"])

    def test_bare_dont_understand_does_not_trigger(self):
        self.assertEqual(scan(uturn("I don't understand. Help me understand.")), [])

    def test_execution_command_is_silent(self):
        self.assertEqual(scan(uturn("Build appact and move the card to Done.")), [])

    def test_tool_result_and_wrappers_skipped(self):
        tool_turn = json.dumps({
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "ok"}]},
        })
        wrapper = uturn("<local-command-stdout>shocked fascinating</local-command-stdout>")
        self.assertEqual(scan(tool_turn, wrapper), [])

    def test_block_form_text_extracted_reminder_block_ignored(self):
        line = uturn_blocks(
            "<system-reminder>curious fascinating</system-reminder>",
            "Another side note: why does the grid trip on heat?",
        )
        c = scan(line)
        self.assertEqual(len(c), 1)
        self.assertIn("tangent", c[0]["signals"])
        self.assertIn("curiosity", c[0]["signals"])

    def test_skill_load_injection_skipped(self):
        # The Skill tool injects the skill body as a user turn; not the user.
        line = uturn("Base directory for this skill: /x/skills/brainstorming "
                     "... fascinating side note explain how ...")
        self.assertEqual(scan(line), [])

    def test_render_shape(self):
        c = scan(uturn("Another unrelated question: what is a nebula?"))
        out = interest.render(c)
        self.assertTrue(out.startswith("[tangent/curiosity]\tL1\t"))


if __name__ == "__main__":
    unittest.main()
