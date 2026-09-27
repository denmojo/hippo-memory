import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from hippo_memory import config, dream, embed, indexer, store


class ParsePlanTest(unittest.TestCase):
    def test_parses_bare_plan_json(self):
        raw = json.dumps({
            "adds": [{"kind": "interest", "title": "T", "body": "B"}],
            "closes": [3],
            "journal": ["promoted: T"],
        })
        plan = dream.parse_plan(raw)
        self.assertEqual(len(plan["adds"]), 1)
        self.assertEqual(plan["closes"], [3])
        self.assertEqual(plan["journal"], ["promoted: T"])
        # missing keys default to empty lists
        self.assertEqual(plan["links"], [])
        self.assertEqual(plan["offsets"], [])

    def test_unwraps_claude_print_envelope(self):
        # claude -p --output-format json wraps the reply in a result envelope
        inner = json.dumps({"adds": [{"kind": "project", "title": "X"}]})
        raw = json.dumps({"type": "result", "result": inner})
        plan = dream.parse_plan(raw)
        self.assertEqual(plan["adds"][0]["title"], "X")

    def test_extracts_json_from_fenced_text(self):
        body = '```json\n{"closes": [1, 2]}\n```'
        raw = json.dumps({"type": "result", "result": "Here is the plan:\n" + body})
        plan = dream.parse_plan(raw)
        self.assertEqual(plan["closes"], [1, 2])

    def test_malformed_raises(self):
        with self.assertRaises(dream.DreamPlanError):
            dream.parse_plan("not json at all, no object here")


class ValidatePlanTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)
        self.open_id = store.upsert_memory(self.conn, "episodic", "Open loop", dedup_key="o")
        self.pinned_id = store.upsert_memory(self.conn, "project", "Floor", dedup_key="p", pinned=True)

    def tearDown(self):
        self.conn.close()

    def test_rejects_bad_kind_and_empty_title(self):
        plan = {"adds": [
            {"kind": "bogus", "title": "T"},
            {"kind": "interest", "title": ""},
            {"kind": "interest", "title": "Good"},
        ], "closes": [], "links": [], "offsets": [], "journal": []}
        clean, rej = dream.validate_plan(self.conn, plan)
        self.assertEqual(len(clean["adds"]), 1)
        self.assertEqual(clean["adds"][0]["title"], "Good")
        self.assertEqual(len(rej), 2)

    def test_rejects_close_of_pinned_and_missing(self):
        plan = {"adds": [], "closes": [self.open_id, self.pinned_id, 9999],
                "links": [], "offsets": [], "journal": []}
        clean, rej = dream.validate_plan(self.conn, plan)
        self.assertEqual(clean["closes"], [self.open_id])
        self.assertEqual(len(rej), 2)  # pinned + missing

    def test_rejects_link_to_missing_id(self):
        plan = {"adds": [], "closes": [], "offsets": [], "journal": [],
                "links": [
                    {"from": self.open_id, "to": self.pinned_id},
                    {"from": self.open_id, "to": 9999},
                ]}
        clean, rej = dream.validate_plan(self.conn, plan)
        self.assertEqual(len(clean["links"]), 1)
        self.assertEqual(len(rej), 1)

    def test_rejects_negative_offset(self):
        plan = {"adds": [], "closes": [], "links": [], "journal": [],
                "offsets": [{"session": "s1", "value": 10},
                            {"session": "s2", "value": -1}]}
        clean, rej = dream.validate_plan(self.conn, plan)
        self.assertEqual(len(clean["offsets"]), 1)
        self.assertEqual(len(rej), 1)


class ApplyPlanTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_apply_adds_closes_links_offsets(self):
        a = store.upsert_memory(self.conn, "episodic", "to close", dedup_key="x")
        plan = {
            "adds": [{"kind": "interest", "title": "New", "body": "b", "dedup_key": "n"}],
            "closes": [a],
            "links": [],
            "offsets": [{"session": "sess-1", "value": 55}],
            "journal": [],
        }
        clean, _ = dream.validate_plan(self.conn, plan)
        summary = dream.apply_plan(self.conn, clean)
        self.assertEqual(summary["added"], 1)
        self.assertEqual(summary["closed"], 1)
        self.assertEqual(summary["offsets"], 1)
        self.assertEqual(store.get_memory(self.conn, a)["status"], "closed")
        self.assertEqual(store.get_state(self.conn, "session:sess-1"), "55")
        new = self.conn.execute("SELECT * FROM memories WHERE dedup_key='n'").fetchone()
        self.assertEqual(new["kind"], "interest")


class DecayMergeValidateTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)
        self.a = store.upsert_memory(self.conn, "interest", "A", dedup_key="a")
        self.b = store.upsert_memory(self.conn, "interest", "B", dedup_key="b")
        self.pin = store.upsert_memory(self.conn, "project", "Floor", dedup_key="p", pinned=True)

    def tearDown(self):
        self.conn.close()

    def _empty(self, **kw):
        plan = {k: [] for k in dream.PLAN_KEYS}
        plan.update(kw)
        return plan

    def test_decay_rejects_pinned_missing_and_bad_weight(self):
        plan = self._empty(decays=[
            {"id": self.a, "weight": -1.0},      # ok (demote)
            {"id": self.pin, "weight": -1.0},    # pinned -> reject
            {"id": 9999, "weight": -1.0},        # missing -> reject
            {"id": self.b, "weight": "low"},     # bad weight -> reject
        ])
        clean, rej = dream.validate_plan(self.conn, plan)
        self.assertEqual(len(clean["decays"]), 1)
        self.assertEqual(clean["decays"][0]["id"], self.a)
        self.assertEqual(len(rej), 3)

    def test_merge_rejects_pinned_self_and_missing(self):
        plan = self._empty(merges=[
            {"from": self.a, "into": self.b},      # ok
            {"from": self.a, "into": self.pin},    # into pinned -> reject
            {"from": self.pin, "into": self.b},    # from pinned -> reject
            {"from": self.a, "into": self.a},      # self -> reject
            {"from": self.a, "into": 9999},        # missing -> reject
        ])
        clean, rej = dream.validate_plan(self.conn, plan)
        self.assertEqual(len(clean["merges"]), 1)
        self.assertEqual(len(rej), 4)


class ChargeFloorTest(unittest.TestCase):
    def test_no_signals_no_floor(self):
        self.assertEqual(dream.charge_floor(set()), 0.0)
        self.assertEqual(dream.charge_floor(None), 0.0)

    def test_charged_or_interrupt_is_flashbulb_tier(self):
        self.assertEqual(dream.charge_floor({"charged"}), config.LESSON_FLOOR_CHARGED)
        self.assertEqual(dream.charge_floor({"interrupt"}), config.LESSON_FLOOR_CHARGED)
        self.assertEqual(
            dream.charge_floor({"dissatisfaction", "charged"}),
            config.LESSON_FLOOR_CHARGED,
        )

    def test_mild_pushback_floors_low(self):
        self.assertEqual(
            dream.charge_floor({"dissatisfaction"}), config.LESSON_FLOOR_MILD
        )
        self.assertEqual(dream.charge_floor({"directive"}), config.LESSON_FLOOR_MILD)


class LessonGuardTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)
        self.fresh = store.upsert_memory(self.conn, "lesson", "fresh scar", dedup_key="f")
        self.old = store.upsert_memory(self.conn, "lesson", "healed scar", dedup_key="h")
        self.conn.execute(
            "UPDATE memories SET created_at = ? WHERE id = ?",
            ((datetime.now(timezone.utc) - timedelta(days=60)).isoformat(), self.old),
        )
        self.conn.commit()
        self.loop = store.upsert_memory(self.conn, "episodic", "a loop", dedup_key="l")

    def tearDown(self):
        self.conn.close()

    def _empty(self, **kw):
        plan = {k: [] for k in dream.PLAN_KEYS}
        plan.update(kw)
        return plan

    def test_lesson_add_weight_floored_at_charge(self):
        plan = self._empty(adds=[
            {"kind": "lesson", "title": "under-priced", "weight": 0.2},
            {"kind": "lesson", "title": "unweighted"},
            {"kind": "lesson", "title": "boosted", "weight": 2.5},
            {"kind": "interest", "title": "not a lesson", "weight": 0.0},
        ])
        clean, rej = dream.validate_plan(self.conn, plan, charge=1.5)
        self.assertEqual(rej, [])
        by_title = {a["title"]: a for a in clean["adds"]}
        self.assertEqual(by_title["under-priced"]["weight"], 1.5)
        self.assertEqual(by_title["unweighted"]["weight"], 1.5)
        self.assertEqual(by_title["boosted"]["weight"], 2.5)   # never lowered
        self.assertEqual(by_title["not a lesson"]["weight"], 0.0)

    def test_no_charge_leaves_weights_alone(self):
        plan = self._empty(adds=[{"kind": "lesson", "title": "calm lesson"}])
        clean, _ = dream.validate_plan(self.conn, plan, charge=0.0)
        self.assertNotIn("weight", clean["adds"][0])

    def test_refractory_blocks_close_decay_merge_of_young_lesson(self):
        plan = self._empty(
            closes=[self.fresh],
            decays=[{"id": self.fresh, "weight": -1.0}],
            merges=[{"from": self.fresh, "into": self.loop}],
        )
        clean, rej = dream.validate_plan(self.conn, plan)
        self.assertEqual(clean["closes"], [])
        self.assertEqual(clean["decays"], [])
        self.assertEqual(clean["merges"], [])
        self.assertEqual(len(rej), 3)
        self.assertTrue(all("refractory" in r for r in rej))

    def test_refractory_expires_and_spares_non_lessons(self):
        plan = self._empty(
            closes=[self.old, self.loop],
            decays=[{"id": self.old, "weight": -1.0}],
        )
        clean, rej = dream.validate_plan(self.conn, plan)
        self.assertEqual(rej, [])
        self.assertEqual(set(clean["closes"]), {self.old, self.loop})
        self.assertEqual(len(clean["decays"]), 1)

    def test_run_dream_stamps_charge_into_stored_lesson(self):
        raw = json.dumps({
            "adds": [{"kind": "lesson", "title": "never write unbidden",
                      "dedup_key": "unbidden"}],
            "journal": ["promoted: lesson"],
        })

        # Explicit orthogonal vectors: the setUp lessons must not fold this
        # add away (and the test must not lazy-load the ONNX model).
        class _Stub:
            _table = {"never write unbidden\n": [1.0, 0.0, 0.0],
                      "fresh scar\n": [0.0, 1.0, 0.0],
                      "healed scar\n": [0.0, 0.0, 1.0]}

            def embed(self, texts):
                return [list(self._table[t]) for t in texts]

        with tempfile.TemporaryDirectory() as d:
            jp = os.path.join(d, "journal.md")
            dream.run_dream(
                self.conn, "material", invoke_fn=lambda p: raw,
                date="2026-07-12", journal_path=jp, charge=1.5,
                embedder=_Stub(),
            )
        row = self.conn.execute(
            "SELECT * FROM memories WHERE dedup_key = 'unbidden'"
        ).fetchone()
        self.assertEqual(row["kind"], "lesson")
        self.assertEqual(row["weight"], 1.5)
        # Rescore ran: a fresh charged lesson scores at baseline + recency + weight.
        self.assertGreater(row["salience"], config.LESSON_BASELINE)


class ApplyDecayMergeTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_decay_sets_weight(self):
        m = store.upsert_memory(self.conn, "interest", "M", dedup_key="m", weight=2.0)
        plan = {k: [] for k in dream.PLAN_KEYS}
        plan["decays"] = [{"id": m, "weight": -1.5}]
        clean, _ = dream.validate_plan(self.conn, plan)
        summary = dream.apply_plan(self.conn, clean)
        self.assertEqual(summary["decayed"], 1)
        self.assertEqual(store.get_memory(self.conn, m)["weight"], -1.5)

    def test_merge_closes_absorbed_relinks_and_marks(self):
        survivor = store.upsert_memory(self.conn, "interest", "Survivor", dedup_key="s")
        absorbed = store.upsert_memory(self.conn, "interest", "Absorbed", dedup_key="ab")
        other = store.upsert_memory(self.conn, "interest", "Other", dedup_key="ot")
        store.add_link(self.conn, absorbed, other)  # should re-point to survivor
        plan = {k: [] for k in dream.PLAN_KEYS}
        plan["merges"] = [{"from": absorbed, "into": survivor}]
        clean, _ = dream.validate_plan(self.conn, plan)
        summary = dream.apply_plan(self.conn, clean)
        self.assertEqual(summary["merged"], 1)
        # absorbed is closed (non-destructive, reversible), survivor stays open
        self.assertEqual(store.get_memory(self.conn, absorbed)["status"], "closed")
        self.assertEqual(store.get_memory(self.conn, survivor)["status"], "open")
        # the link now originates from survivor, and a 'merged' edge records the fold
        links = self.conn.execute("SELECT from_id, to_id, kind FROM links").fetchall()
        pairs = {(l["from_id"], l["to_id"], l["kind"]) for l in links}
        self.assertIn((survivor, other, "relates"), pairs)
        self.assertIn((survivor, absorbed, "merged"), pairs)


class CountDestructiveTest(unittest.TestCase):
    def test_counts_closes_decays_merges_not_adds(self):
        plan = {
            "adds": [{"kind": "interest", "title": "x"}, {"kind": "interest", "title": "y"}],
            "closes": [1, 2],
            "decays": [{"id": 3, "weight": -1}],
            "merges": [{"from": 4, "into": 5}],
            "links": [{"from": 1, "to": 2}],
            "offsets": [], "journal": [],
        }
        self.assertEqual(dream.count_destructive(plan), 4)


class RunDreamTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Path(self.tmp.name) / "journal.md"
        self.old_id = store.upsert_memory(self.conn, "episodic", "old loop", dedup_key="old")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _invoke(self, plan_dict):
        return lambda prompt: json.dumps({"type": "result", "result": json.dumps(plan_dict)})

    def test_run_applies_and_writes_journal(self):
        plan = {
            "adds": [{"kind": "interest", "title": "Fresh interest", "dedup_key": "fi"}],
            "closes": [self.old_id],
            "offsets": [{"session": "abc", "value": 12}],
            "journal": ["promoted: Fresh interest", "closed: old loop"],
        }
        summary = dream.run_dream(
            self.conn, material="DIGEST MATERIAL", invoke_fn=self._invoke(plan),
            date="2026-06-17", journal_path=str(self.journal),
        )
        self.assertEqual(summary["added"], 1)
        self.assertEqual(summary["closed"], 1)
        self.assertEqual(store.get_memory(self.conn, self.old_id)["status"], "closed")
        self.assertTrue(self.journal.exists())
        self.assertIn("Fresh interest", self.journal.read_text())
        # rescore ran: the new memory has a non-default salience
        new = self.conn.execute("SELECT salience FROM memories WHERE dedup_key='fi'").fetchone()
        self.assertGreater(new["salience"], 0.0)

    def test_dry_run_does_not_mutate(self):
        plan = {"adds": [{"kind": "interest", "title": "Nope"}], "closes": [self.old_id]}
        summary = dream.run_dream(
            self.conn, material="X", invoke_fn=self._invoke(plan),
            date="2026-06-17", journal_path=str(self.journal), apply=False,
        )
        self.assertTrue(summary["dry_run"])
        self.assertEqual(store.get_memory(self.conn, self.old_id)["status"], "open")
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 1)

    def test_blast_radius_abort_saves_plan_and_does_not_mutate(self):
        plan = {"closes": [self.old_id, self.old_id, self.old_id], "journal": ["x"]}
        summary = dream.run_dream(
            self.conn, material="X", invoke_fn=self._invoke(plan),
            date="2026-06-17", journal_path=str(self.journal),
            destructive_cap=2, plan_dir=self.tmp.name,
        )
        self.assertTrue(summary["aborted"])
        self.assertEqual(summary["destructive"], 3)
        # nothing applied, no journal written
        self.assertEqual(store.get_memory(self.conn, self.old_id)["status"], "open")
        self.assertFalse(self.journal.exists())
        # the rejected plan is saved for review
        self.assertTrue(Path(summary["plan_path"]).exists())

    def test_under_cap_applies_decay_and_merge(self):
        s = store.upsert_memory(self.conn, "interest", "surv", dedup_key="sv", weight=1.0)
        plan = {
            "decays": [{"id": self.old_id, "weight": -2.0}],
            "merges": [{"from": s, "into": self.old_id}],
            "journal": ["decayed + merged"],
        }
        summary = dream.run_dream(
            self.conn, material="X", invoke_fn=self._invoke(plan),
            date="2026-06-17", journal_path=str(self.journal), destructive_cap=10,
        )
        self.assertFalse(summary.get("aborted"))
        self.assertEqual(summary["decayed"], 1)
        self.assertEqual(summary["merged"], 1)

    def test_prompt_carries_material_and_asks_for_json(self):
        captured = {}

        def invoke(prompt):
            captured["p"] = prompt
            return json.dumps({"adds": [], "journal": []})

        dream.run_dream(self.conn, material="UNIQUE-MARKER-9281", invoke_fn=invoke,
                        date="2026-06-17", journal_path=str(self.journal))
        self.assertIn("UNIQUE-MARKER-9281", captured["p"])
        self.assertIn("JSON", captured["p"])

    def test_run_dream_embeds_the_memories_it_promotes(self):
        """A memory the dream just created must be findable by semantic recall
        the moment the dream ends. Without this the index silently stops being
        fed and `search`/`recall` go blind to everything recent."""
        plan = {"adds": [{"kind": "interest", "title": "Fresh interest", "dedup_key": "fi"}],
                "journal": ["promoted: Fresh interest"]}
        summary = dream.run_dream(
            self.conn, material="X", invoke_fn=self._invoke(plan),
            date="2026-06-17", journal_path=str(self.journal),
            embedder=embed.FakeEmbedder(dim=8),
        )
        new_id = self.conn.execute(
            "SELECT id FROM memories WHERE dedup_key='fi'").fetchone()["id"]
        self.assertIsNotNone(
            store.get_embedding(self.conn, "hippo", str(new_id)),
            "the dream promoted a memory but left it unembedded, so recall cannot see it",
        )
        self.assertEqual(summary["indexed"], 2)  # the new add + the pre-existing old loop

    def test_run_dream_survives_an_unavailable_embedder(self):
        """A dream that consolidated correctly must not fail because the
        embedder could not load. Indexing is best-effort, and reported."""
        plan = {"adds": [{"kind": "interest", "title": "No embedder", "dedup_key": "ne"}],
                "journal": ["promoted: No embedder"]}
        with mock.patch.object(dream, "_load_embedder", return_value=None):
            summary = dream.run_dream(
                self.conn, material="X", invoke_fn=self._invoke(plan),
                date="2026-06-17", journal_path=str(self.journal),
            )
        self.assertEqual(summary["added"], 1)
        self.assertEqual(summary["indexed"], 0)

    def test_unindexed_dream_warns_in_the_journal(self):
        """A skipped index is invisible until recall silently misses, so it must
        be surfaced, not swallowed."""
        plan = {"adds": [{"kind": "interest", "title": "No embedder", "dedup_key": "ne"}],
                "journal": ["promoted: No embedder"]}
        with mock.patch.object(dream, "_load_embedder", return_value=None):
            dream.run_dream(
                self.conn, material="X", invoke_fn=self._invoke(plan),
                date="2026-06-17", journal_path=str(self.journal),
            )
        self.assertIn("WARNING", self.journal.read_text())
        self.assertIn("hippo index", self.journal.read_text())

    def test_indexing_failure_does_not_kill_a_good_dream(self):
        """The embedder blowing up must not cost us a correctly consolidated night."""
        plan = {"adds": [{"kind": "interest", "title": "Boom", "dedup_key": "bm"}],
                "journal": ["promoted: Boom"]}
        boom = mock.Mock(side_effect=RuntimeError("onnxruntime exploded"))
        with mock.patch.object(indexer, "index_hippo", boom):
            summary = dream.run_dream(
                self.conn, material="X", invoke_fn=self._invoke(plan),
                date="2026-06-17", journal_path=str(self.journal),
                embedder=embed.FakeEmbedder(dim=8),
            )
        self.assertEqual(summary["added"], 1)
        self.assertEqual(summary["indexed"], 0)
        self.assertIn("WARNING", self.journal.read_text())


class InvokeClaudeTest(unittest.TestCase):
    class _Proc:
        def __init__(self, returncode, stdout="", stderr=""):
            self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def setUp(self):
        # These tests exercise retry/error handling, not auth, so give
        # setup-token mode (the default) a token file to find; otherwise every
        # call would fail on the missing-token check before reaching a runner.
        tok_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tok_dir.cleanup)
        tok = Path(tok_dir.name) / "tok"
        tok.write_text("sk-ant-oat-test\n")
        patcher = mock.patch.object(config, "TOKEN_PATH", tok)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patch_run(self, results):
        calls = {"n": 0}

        def fake_run(*a, **k):
            i = calls["n"]
            calls["n"] += 1
            return results[min(i, len(results) - 1)]

        return fake_run, calls

    def test_prompt_goes_on_stdin_not_argv(self):
        # A prompt in argv put the whole dream material on the command line and
        # overflowed ARG_MAX on 2026-07-27, killing the run with OSError E2BIG.
        # The bigger the day, the likelier the skip, so hold the prompt off argv.
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"], seen["kwargs"] = cmd, kwargs
            return self._Proc(0, stdout="OK")

        big = "x" * 2_000_000
        self.assertEqual(
            dream.invoke_claude(big, sleep=lambda _: None, runner=fake_run), "OK"
        )
        self.assertEqual(seen["cmd"], ["claude", "-p", "--output-format", "json"])
        self.assertEqual(seen["kwargs"]["input"], big)

    def test_returns_stdout_on_success(self):
        fake, calls = self._patch_run([self._Proc(0, stdout="OK")])
        self.assertEqual(
            dream.invoke_claude("p", sleep=lambda _: None, runner=fake), "OK"
        )
        self.assertEqual(calls["n"], 1)

    def test_error_message_includes_stdout_when_stderr_empty(self):
        # claude -p writes its error to stdout, not stderr; the message must show it.
        fake, _ = self._patch_run(
            [self._Proc(1, stdout="401 Invalid authentication credentials")]
        )
        with self.assertRaises(dream.DreamPlanError) as cm:
            dream.invoke_claude("p", retries=0, sleep=lambda _: None, runner=fake)
        self.assertIn("401 Invalid authentication", str(cm.exception))

    def test_retries_transient_then_succeeds(self):
        fake, calls = self._patch_run([
            self._Proc(1, stdout="error: 529 overloaded"),
            self._Proc(0, stdout="OK"),
        ])
        self.assertEqual(
            dream.invoke_claude("p", retries=2, sleep=lambda _: None, runner=fake), "OK"
        )
        self.assertEqual(calls["n"], 2)

    def test_does_not_retry_non_transient(self):
        fake, calls = self._patch_run([self._Proc(1, stdout="malformed plan json")])
        with self.assertRaises(dream.DreamPlanError):
            dream.invoke_claude("p", retries=2, sleep=lambda _: None, runner=fake)
        self.assertEqual(calls["n"], 1)

    def test_auth_failure_is_not_retried(self):
        # An expired credential is deterministic at 02:30, not a blip: surface it
        # immediately rather than retrying it into silence.
        fake, calls = self._patch_run(
            [self._Proc(1, stdout="401 Invalid authentication credentials")]
        )
        with self.assertRaises(dream.DreamPlanError) as cm:
            dream.invoke_claude("p", retries=2, sleep=lambda _: None, runner=fake)
        self.assertEqual(calls["n"], 1)
        self.assertIn("401", str(cm.exception))

    def test_is_transient_matches_rate_not_auth(self):
        self.assertTrue(dream._is_transient("429 rate limited"))
        self.assertTrue(dream._is_transient("529 overloaded"))
        self.assertFalse(dream._is_transient("401 Invalid authentication credentials"))
        self.assertFalse(dream._is_transient("authentication_failed"))
        self.assertFalse(dream._is_transient("malformed json"))


class HandoffNeverDreamWrittenTest(unittest.TestCase):
    """The dream reads handoff rows; it never writes one."""

    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_apply_plan_rejects_handoff_add(self):
        plan = {"adds": [{"kind": "handoff", "title": "note"}],
                "closes": [], "links": [], "offsets": [], "journal": []}
        clean, rej = dream.validate_plan(self.conn, plan)
        self.assertEqual(clean["adds"], [])
        self.assertEqual(len(rej), 1)
        self.assertIn("handoff", rej[0])


class AnnotateTest(unittest.TestCase):
    """The dream folds a handoff's Decided/Dead ends content into the episodic
    or project row it corroborates through the annotate op, with a source
    link back to the note."""

    def setUp(self):
        from hippo_memory import handoff
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)
        self.loop = store.upsert_memory(self.conn, "episodic", "router probe", dedup_key="l")
        self.pinned = store.upsert_memory(self.conn, "project", "floor", dedup_key="p", pinned=True)
        self.note = handoff.add(self.conn, "n", "Decided: cron over launchd", "s1", carries=[self.loop])

    def tearDown(self):
        self.conn.close()

    def _plan(self, **over):
        base = {k: [] for k in dream.PLAN_KEYS}
        base.update(over)
        return base

    def test_validate_accepts_good_and_rejects_bad(self):
        plan = self._plan(annotate=[
            {"id": self.loop, "source": self.note, "text": "cron over launchd"},
            {"id": 9999, "source": self.note, "text": "x"},
            {"id": self.loop, "source": self.loop, "text": "x"},
            {"id": self.note, "source": self.note, "text": "x"},
            {"id": self.pinned, "source": self.note, "text": "x"},
            {"id": self.loop, "source": self.note, "text": "  "},
        ])
        clean, rej = dream.validate_plan(self.conn, plan)
        self.assertEqual(len(clean["annotate"]), 1)
        self.assertEqual(len(rej), 5)

    def test_apply_appends_and_links(self):
        plan = self._plan(annotate=[{"id": self.loop, "source": self.note, "text": "cron over launchd"}])
        clean, _ = dream.validate_plan(self.conn, plan)
        summary = dream.apply_plan(self.conn, clean)
        self.assertEqual(summary["annotated"], 1)
        body = store.get_memory(self.conn, self.loop)["body"]
        self.assertIn("cron over launchd", body)
        self.assertIn(f"[from handoff {self.note}]", body)
        kinds = {r["kind"] for r in self.conn.execute(
            "SELECT kind FROM links WHERE from_id=? AND to_id=?", (self.note, self.loop))}
        self.assertEqual(kinds, {"carries", "source"})

    def test_prompt_names_handoff_rules(self):
        self.assertIn("HANDOFF NOTES", dream.PROMPT_HEADER)
        self.assertIn("annotate", dream.PROMPT_HEADER)


class DreamHandoffLifecycleTest(unittest.TestCase):
    """run_dream runs handoff housekeeping after apply and journals one
    handoff-backed / no-handoff line per consolidated session."""

    class _Stub:
        def embed(self, texts):
            return [[1.0, 0.0, 0.0] for _ in texts]

    def setUp(self):
        from hippo_memory import handoff
        self.conn = store.connect(":memory:")
        store.init_schema(self.conn)
        self.loop = store.upsert_memory(self.conn, "episodic", "loop", dedup_key="l")
        self.note = handoff.add(self.conn, "n", "State: x", "s1", carries=[self.loop])
        self.tmp = tempfile.TemporaryDirectory()
        ends = Path(self.tmp.name) / "session-ends.jsonl"
        ends.write_text(json.dumps({"session_id": "s1", "reason": "clear", "had_handoff": True}) + "\n")
        os.environ["HIPPO_SESSION_ENDS"] = str(ends)

    def tearDown(self):
        os.environ.pop("HIPPO_SESSION_ENDS", None)
        self.tmp.cleanup()
        self.conn.close()

    def test_housekeeping_and_per_session_journal(self):
        raw = json.dumps({"closes": [self.loop], "journal": ["closed: loop"]})
        jp = os.path.join(self.tmp.name, "journal-test.txt")
        summary = dream.run_dream(
            self.conn, "material", invoke_fn=lambda p: raw, date="2026-08-25",
            journal_path=jp, embedder=self._Stub(), sessions=["s1", "s2"],
        )
        self.assertEqual(summary["handoffs_closed"], 1)
        self.assertEqual(store.get_memory(self.conn, self.note)["status"], "closed")
        text = Path(jp).read_text()
        self.assertIn("session s1: handoff-backed (1 note); ended: clear", text)
        self.assertIn("session s2: no handoff", text)


def test_prompt_names_configured_owner():
    p = dream.build_prompt("MATERIAL", owner="Alex")
    assert "Alex's memory store" in p
    assert config.OWNER not in p


def test_invoke_uses_setup_token_and_model(monkeypatch, tmp_path):
    calls = {}

    def runner(cmd, **kw):
        calls["cmd"] = cmd
        calls["env"] = kw.get("env")
        calls["cwd"] = kw.get("cwd")
        return subprocess.CompletedProcess(cmd, 0, stdout='{"result":"{}"}', stderr="")

    tok = tmp_path / "tok"
    tok.write_text("sk-ant-oat-test\n")
    monkeypatch.setattr(config, "AUTH_MODE", "setup-token")
    monkeypatch.setattr(config, "MODEL", "claude-sonnet-5")
    monkeypatch.setattr(config, "TOKEN_PATH", tok)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    dream.invoke_claude("hi", runner=runner)
    assert calls["cmd"][:2] == ["claude", "-p"]
    assert "--model" in calls["cmd"] and "claude-sonnet-5" in calls["cmd"]
    assert calls["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-test"
    assert calls["cwd"] == str(tmp_path)


def test_invoke_setup_token_mode_missing_file_raises(monkeypatch, tmp_path):
    tok = tmp_path / "missing-tok"
    monkeypatch.setattr(config, "AUTH_MODE", "setup-token")
    monkeypatch.setattr(config, "TOKEN_PATH", tok)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    def runner(cmd, **kw):
        raise AssertionError("runner must not be called when the token file is missing")

    with pytest.raises(dream.DreamPlanError) as excinfo:
        dream.invoke_claude("hi", runner=runner)
    assert "hippo init" in str(excinfo.value)
    assert "sk-ant" not in str(excinfo.value)


def test_invoke_api_key_mode_passes_key_through(monkeypatch, tmp_path):
    seen = {}

    def runner(cmd, **kw):
        seen["env"] = kw.get("env")
        return subprocess.CompletedProcess(cmd, 0, stdout='{"result":"{}"}', stderr="")

    monkeypatch.setattr(config, "AUTH_MODE", "api-key")
    monkeypatch.setattr(config, "MODEL", "")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-test")
    dream.invoke_claude("hi", runner=runner)
    assert seen["env"]["ANTHROPIC_API_KEY"] == "sk-ant-api-test"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen["env"]


def test_headless_env_marks_hippo_in_dream(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AUTH_MODE", "api-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-test")
    env = dream._headless_env()
    assert env["HIPPO_IN_DREAM"] == "1"


def test_invoke_retries_a_timeout_then_succeeds(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AUTH_MODE", "api-key")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-test")
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        return subprocess.CompletedProcess(cmd, 0, stdout='{"result":"{}"}', stderr="")

    out = dream.invoke_claude("hi", retries=1, backoff=0, sleep=lambda s: None, runner=runner)
    assert out == '{"result":"{}"}'
    assert len(calls) == 2


def test_invoke_raises_dream_plan_error_after_repeated_timeouts(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AUTH_MODE", "api-key")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-test")

    def runner(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    with pytest.raises(dream.DreamPlanError) as excinfo:
        dream.invoke_claude("hi", timeout=600, retries=1, backoff=0, sleep=lambda s: None,
                            runner=runner)
    assert "timed out" in str(excinfo.value)


def test_invoke_raises_dream_plan_error_when_claude_is_not_on_path(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AUTH_MODE", "api-key")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-test")

    def runner(cmd, **kw):
        raise FileNotFoundError("claude")

    with pytest.raises(dream.DreamPlanError) as excinfo:
        dream.invoke_claude("hi", retries=2, backoff=0, sleep=lambda s: None, runner=runner)
    assert "could not run claude" in str(excinfo.value)


def test_no_vault_law_or_verdicts_in_source():
    import inspect
    src = inspect.getsource(dream)
    for needle in ("vault_law", "vault-law", "verdict", "ADR-004", "laurel"):
        assert needle not in src


if __name__ == "__main__":
    unittest.main()
