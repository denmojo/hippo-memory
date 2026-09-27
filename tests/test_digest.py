import json
import time
from pathlib import Path

import pytest

from hippo_memory import digest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def pin_tz(monkeypatch):
    """Pin the process timezone so [HH:MM] stamps and the span line are
    deterministic regardless of the host's TZ. monkeypatch.undo() is called
    explicitly (not left to monkeypatch's own finalizer) so the tzset() that
    re-syncs libc's timezone state after restoring TZ runs before this
    fixture returns control, not after - monkeypatch reverts the environment
    variable but has no idea tzset() exists, so a bare `yield` here without
    an explicit undo()+tzset() would leave every later test in the process
    running under America/Los_Angeles."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _line(kind, text, ts="2026-09-26T10:00:00Z"):
    return json.dumps({"type": kind, "timestamp": ts,
                       "message": {"role": kind, "content": [{"type": "text", "text": text}]}})


def test_digest_keeps_user_turns_and_assistant_prose(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join([_line("user", "please rotate the logs"),
                            _line("assistant", "Rotating now.")]) + "\n")
    out = digest.digest_path(p)
    assert "please rotate the logs" in out
    assert "Rotating now." in out


def test_find_jsonls_uses_sessions_dir(tmp_path):
    (tmp_path / "a.jsonl").write_text("")
    found = digest.find_jsonls(tmp_path, days=1)
    assert [f.name for f in found] == ["a.jsonl"]


def test_digest_path_matches_reference_script_output(pin_tz):
    """digest_path must produce byte-identical text to ~/bin/session-digest's
    stdout for the same transcript. The expected text below was captured once
    by running that script (with TZ=America/Los_Angeles, matching pin_tz
    above) against this fixture; this test does not invoke the script
    itself, only compares against the stored text with the source path
    substituted back in.

    Deliberate one-line divergence (Ruling T6-1): the script's banner reads
    "...replaced by line counts unless --full)"; --full does not exist in
    this module (Ruling B6), so the banner here reads "...replaced by line
    counts)" and the golden fixture was edited to match on that one line
    only. Every other byte is the script's literal, unedited output."""
    path = FIXTURES / "digest_sample.jsonl"
    template = (FIXTURES / "digest_sample.expected.txt").read_text()
    expected = template.replace("{PATH}", str(path))
    assert digest.digest_path(path) == expected
