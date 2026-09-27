import io
import json

import pytest

from hippo_memory import cli, config, hooks, store

EVENTS = ["session-start", "prompt", "pre-compact", "session-end"]


def _run(event, payload, monkeypatch, capsys):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    rc = hooks.run(event)
    return rc, capsys.readouterr().out


def test_session_start_empty_stdin_is_silent(monkeypatch, capsys):
    rc, out = _run("session-start", "", monkeypatch, capsys)
    assert rc == 0 and out == ""


def test_session_start_bad_json_is_silent(monkeypatch, capsys):
    rc, out = _run("session-start", "{not json", monkeypatch, capsys)
    assert rc == 0 and out == ""


@pytest.mark.parametrize("event", EVENTS)
def test_empty_stdin_is_silent_for_every_event(event, monkeypatch, capsys):
    # Review Focus 2: a hook that errors blocks the user's session, so every
    # event must exit 0 and print nothing on empty stdin.
    rc, out = _run(event, "", monkeypatch, capsys)
    assert rc == 0 and out == ""


@pytest.mark.parametrize("event", EVENTS)
def test_bad_json_is_silent_for_every_event(event, monkeypatch, capsys):
    rc, out = _run(event, "{not json", monkeypatch, capsys)
    assert rc == 0 and out == ""


@pytest.mark.parametrize("event", EVENTS)
def test_missing_store_is_silent_for_every_event(event, monkeypatch, capsys, tmp_path):
    # Simulate a store that cannot be opened/initialized (e.g. an unwritable
    # data dir). Every hook event must still exit 0 and print nothing rather
    # than let the exception surface and block the session.
    def _boom():
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(cli, "ensure_store", _boom)
    tp = tmp_path / "t.jsonl"
    usage = {"input_tokens": 200000}
    tp.write_text(json.dumps({"type": "assistant", "message": {"usage": usage}}) + "\n")
    payload = {"session_id": "s-missing", "transcript_path": str(tp),
               "trigger": "auto", "reason": "clear"}
    rc, out = _run(event, payload, monkeypatch, capsys)
    assert rc == 0 and out == ""


@pytest.mark.parametrize("event", EVENTS)
def test_in_dream_returns_early_for_every_event(event, monkeypatch, capsys):
    # The headless dream's own `claude -p` call loads the user's global
    # settings and fires these same hooks; HIPPO_IN_DREAM (set by
    # dream._headless_env) must short-circuit before stdin is even read, so
    # the dream's own session never gets a working-set injection or a
    # session-end record of its own.
    monkeypatch.setenv("HIPPO_IN_DREAM", "1")

    def _boom():
        raise AssertionError("run() must return before reading stdin when HIPPO_IN_DREAM is set")

    class _NoRead(io.StringIO):
        def read(self, *a, **k):
            _boom()

    monkeypatch.setattr("sys.stdin", _NoRead(""))
    rc = hooks.run(event)
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_session_start_injects_working_set(monkeypatch, capsys):
    db = cli.ensure_store()
    conn = store.connect(db); store.init_schema(conn)
    store.upsert_memory(conn, "episodic", "Finish the antenna mount", "", "k1"); conn.close()
    rc, out = _run("session-start", {"session_id": "s1", "source": "startup"}, monkeypatch, capsys)
    assert rc == 0
    resp = json.loads(out)
    assert "Finish the antenna mount" in resp["hookSpecificOutput"]["additionalContext"]
    assert resp["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_session_start_system_message_present(monkeypatch, capsys):
    # Ruling A5: the SessionStart response must carry a non-empty systemMessage
    # (the same greeting cli.cmd_motd prints), not a hasattr-guarded no-op.
    db = cli.ensure_store()
    conn = store.connect(db); store.init_schema(conn)
    store.set_state(conn, "last_dream_at", "2026-09-25T10:00:00")
    conn.close()
    rc, out = _run("session-start", {"session_id": "s4", "source": "startup"}, monkeypatch, capsys)
    assert rc == 0
    resp = json.loads(out)
    assert resp.get("systemMessage")


def test_prompt_records_session_and_nudges_once(monkeypatch, capsys, tmp_path):
    tp = tmp_path / "t.jsonl"
    usage = {"input_tokens": 100000, "cache_creation_input_tokens": 30000, "cache_read_input_tokens": 30000}
    tp.write_text(json.dumps({"type": "assistant", "message": {"usage": usage}}) + "\n")
    payload = {"session_id": "s2", "transcript_path": str(tp)}
    rc, out = _run("prompt", payload, monkeypatch, capsys)
    assert rc == 0
    assert config.CURRENT_SESSION_PATH.read_text().splitlines() == ["s2", str(tp)]
    assert "Context at 160k tokens" in out and "hand off" in out
    rc, out = _run("prompt", payload, monkeypatch, capsys)
    assert out == ""


def test_pre_compact_and_session_end_append_records(monkeypatch, capsys):
    _run("pre-compact", {"session_id": "s3", "transcript_path": "/x", "trigger": "auto"}, monkeypatch, capsys)
    rec = json.loads(config.COMPACTIONS_PATH.read_text().splitlines()[-1])
    assert rec["session_id"] == "s3" and rec["trigger"] == "auto" and rec["at"]
    _run("session-end", {"session_id": "s3", "reason": "clear"}, monkeypatch, capsys)
    rec = json.loads(config.SESSION_ENDS_PATH.read_text().splitlines()[-1])
    assert rec == {"session_id": "s3", "reason": "clear", "at": rec["at"], "had_handoff": False}
