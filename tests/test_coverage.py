"""Coverage pre-pass: the recurrence + external-artifact guard.

test_fable_in_tool_result_is_flagged encodes the 2026-06-12 miss directly. A
government order cutting Fable 5 access arrived in a WebFetch tool_result and
was silently dropped because it was judged from the fetch action, not the
content. This guard must surface it.
"""
import json
import unittest

from hippo_memory import coverage


def _line(role, content):
    return json.dumps({"type": role, "message": {"role": role, "content": content}})


MEMORIES = [
    {"id": 12, "title": "Fable 5 launch: immediate adoption and cost-awareness",
     "body": "Alex adopted Fable 5 on launch day; cost-aware about the Max plan."},
    {"id": 23, "title": "task-tracker: badge-on-card shipped; delete deferred",
     "body": "Task tracker improvements."},
    {"id": 17, "title": "Alex Cedar Goods: standing up his own business",
     "body": "Alex Cedar Goods storefront."},
]


def _index():
    return coverage.build_index(MEMORIES)


class TestRecurrence(unittest.TestCase):
    def test_fable_in_tool_result_is_flagged(self):
        # The miss: user points at a URL, assistant fetches, and the
        # government-order detail lives only in the tool_result body.
        lines = [
            (1, _line("user", "Append the summary of this "
                              "https://www.anthropic.com/news/fable-mythos-access "
                              "to today's news brief.")),
            (2, _line("assistant", [
                {"type": "tool_use", "name": "WebFetch",
                 "input": {"url": "https://www.anthropic.com/news/fable-mythos-access"}},
            ])),
            (3, _line("user", [
                {"type": "tool_result", "tool_use_id": "x", "content": [
                    {"type": "text", "text":
                        "The US government ordered Anthropic to cut foreign access "
                        "to Fable 5 and Mythos 5 after a safeguard bypass; US "
                        "accounts affected, only Opus 4.8 available."}]},
            ])),
        ]
        rec, arts = coverage.scan_lines(iter(lines), _index())
        self.assertIn(12, rec, "Fable thread must be flagged as recurrence")
        # Flagged in BOTH the user turn (url) and the tool_result body.
        self.assertTrue(any(a["kinds"] == ["WebFetch"] for a in arts))
        self.assertTrue(any("url" in a["kinds"] for a in arts))

    def test_alias_resolves_second_name(self):
        # A turn naming only "Mythos" (no "Fable") still resolves to the thread.
        lines = [(1, _line("assistant", [
            {"type": "text", "text": "Mythos 5 is also affected by the order."}]))]
        rec, _ = coverage.scan_lines(iter(lines), _index())
        self.assertIn(12, rec)

    def test_unrelated_chatter_matches_nothing(self):
        lines = [(1, _line("user", "Cooking Thai yellow curry with chicken tonight."))]
        rec, arts = coverage.scan_lines(iter(lines), _index())
        self.assertEqual(rec, {})
        self.assertEqual(arts, [])

    def test_generic_words_do_not_collide(self):
        # "launch" / "adoption" are in [12]'s title but are stoplisted, so a turn
        # using them generically must not flag the Fable thread.
        lines = [(1, _line("user",
                           "Plan the adoption rollout and launch timeline."))]
        rec, _ = coverage.scan_lines(iter(lines), _index())
        self.assertNotIn(12, rec)


    def test_tool_use_input_is_not_matched(self):
        # Keyword-looking words inside a tool_use input (a shell command, a
        # search query, schema plumbing) must not create recurrence.
        lines = [(1, _line("assistant", [
            {"type": "tool_use", "name": "Bash",
             "input": {"command": "echo fable mythos"}}]))]
        rec, arts = coverage.scan_lines(iter(lines), _index())
        self.assertEqual(rec, {})
        self.assertEqual(arts, [])


class TestRenderFiltering(unittest.TestCase):
    def test_strong_singleton_kept_weak_singleton_filtered(self):
        mems = [
            {"id": 1, "title": "Topical concern: denaturalization citizenship-stripping"},
            {"id": 2, "title": "Build focus shifted to mobile apps"},
        ]
        idx = coverage.build_index(mems)
        strong = coverage.strong_tokens(mems)
        titles = {m["id"]: m["title"] for m in mems}
        # One line touches each thread by exactly one token.
        lines = [(1, _line("user", "a quick note on denaturalization and mobile"))]
        rec, _ = coverage.scan_lines(iter(lines), idx)
        out = coverage.render(rec, [], titles, strong)
        self.assertIn("[1]", out)      # 'denaturalization' is unique + long -> kept
        self.assertNotIn("[2]", out)   # 'mobile' alone is short -> filtered as noise

    def test_two_distinct_tokens_make_the_list(self):
        idx = _index()
        strong = coverage.strong_tokens(MEMORIES)
        titles = {m["id"]: m["title"] for m in MEMORIES}
        lines = [(1, _line("user", "the Fable Mythos order"))]
        rec, _ = coverage.scan_lines(iter(lines), idx)
        out = coverage.render(rec, [], titles, strong)
        self.assertIn("[12]", out)     # fable + mythos = two distinct tokens


class TestArtifacts(unittest.TestCase):
    def test_bare_user_url_is_an_artifact(self):
        lines = [(1, _line("user", "look at https://example.com/thing please"))]
        _, arts = coverage.scan_lines(iter(lines), _index())
        self.assertEqual(len(arts), 1)
        self.assertIn("url", arts[0]["kinds"])
        self.assertEqual(arts[0]["url"], "https://example.com/thing")


def test_stopwords_carry_no_personal_or_vault_terms():
    import inspect
    from hippo_memory import coverage
    src = inspect.getsource(coverage).lower()
    for needle in ("designs", "infra", "vault"):
        assert needle not in src


if __name__ == "__main__":
    unittest.main()
