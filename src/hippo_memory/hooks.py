"""Claude Code hook entry points. Each reads the hook's JSON payload on stdin,
does its work, and prints the response the event expects. Every path that
cannot proceed exits 0 silently, because a hook that fails blocks the user."""
import json
import os
import sys
from datetime import datetime

from hippo_memory import config, handoff, store, workingset

REGISTRATIONS = {
    "SessionStart": [{"matcher": "startup|resume|clear|compact",
                      "hooks": [{"type": "command", "command": "hippo hook session-start"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "hippo hook prompt"}]}],
    "PreCompact": [{"hooks": [{"type": "command", "command": "hippo hook pre-compact"}]}],
    "SessionEnd": [{"hooks": [{"type": "command", "command": "hippo hook session-end"}]}],
}


def _payload():
    try:
        raw = sys.stdin.read()
    except Exception:
        return None
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _connect():
    from hippo_memory import cli
    return store.connect(cli.ensure_store())


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _append(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def _handoff_count(conn, sid):
    return len(handoff.list_rows(conn, session=sid, include_closed=True))


def session_start(payload):
    sid = str(payload.get("session_id") or "")
    conn = _connect()
    sel = workingset.select(conn, session_id=sid)
    last_at = store.get_state(conn, "last_dream_at")
    block = workingset.render(sel, stale=workingset.stale_days(),
                              failure=workingset.dream_failure(), last_at=last_at)
    # Shared with cli.cmd_motd so the hook's systemMessage and the manual
    # `hippo motd` line never drift apart.
    motd = workingset.motd(conn)
    if payload.get("source") == "compact" and sid:
        latest = handoff.latest(conn, session=sid)
        if latest:
            block += (f"\n_Compaction just ran; your last note to self this session is "
                      f"[{latest['id']}] (`hippo show {latest['id']}`)._\n")
    conn.close()
    out = {}
    if block:
        out["hookSpecificOutput"] = {"hookEventName": "SessionStart", "additionalContext": block}
    if motd:
        out["systemMessage"] = motd
    return out


def _last_usage_tokens(transcript_path):
    last = None
    try:
        with open(transcript_path) as f:
            for line in f:
                if '"type": "assistant"' in line or '"type":"assistant"' in line:
                    last = line
    except OSError:
        return None
    if not last:
        return None
    try:
        usage = json.loads(last)["message"]["usage"]
    except (ValueError, KeyError, TypeError):
        return None
    return sum(int(usage.get(k, 0) or 0) for k in
               ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))


def prompt(payload):
    sid = str(payload.get("session_id") or "")
    tp = str(payload.get("transcript_path") or "")
    if not sid or not tp:
        return ""
    config.CURRENT_SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(config.CURRENT_SESSION_PATH) + f".tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(f"{sid}\n{tp}\n")
    os.replace(tmp, config.CURRENT_SESSION_PATH)

    if not os.path.isfile(tp):
        return ""
    tokens = _last_usage_tokens(tp)
    if tokens is None:
        return ""
    crossed = max([b for b in config.NUDGE_TOKENS if tokens >= b], default=0)
    if not crossed:
        return ""
    sentinel = config.CONTEXT_WATCH_DIR / sid
    last_bucket = last_count = 0
    if sentinel.exists():
        parts = sentinel.read_text().split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            last_bucket, last_count = int(parts[0]), int(parts[1])
    if crossed <= last_bucket:
        return ""
    conn = _connect()
    count = _handoff_count(conn, sid)
    conn.close()
    config.CONTEXT_WATCH_DIR.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(f"{crossed} {count}\n")
    if count > last_count:
        return ""
    k = (tokens + 500) // 1000
    since = "since session start" if last_bucket == 0 else "since the last nudge"
    # No percent of HIPPO_CONTEXT_WINDOW here: the default (1000k) assumes a
    # window size that not every plan or model has, and a raw token count
    # cannot mislead the way a percent of the wrong window would.
    return (f"Context at {k}k tokens and no handoff note {since}; "
            f"hand off now (/hippo-handoff) before continuing.\n")


def pre_compact(payload):
    sid = payload.get("session_id")
    if not sid:
        return
    _append(config.COMPACTIONS_PATH, {
        "session_id": sid, "transcript_path": payload.get("transcript_path", ""),
        "trigger": payload.get("trigger", ""), "at": _now()})


def session_end(payload):
    sid = payload.get("session_id")
    if not sid:
        return
    had = False
    try:
        conn = _connect()
        had = _handoff_count(conn, sid) > 0
        conn.close()
    except Exception:
        had = False
    _append(config.SESSION_ENDS_PATH, {"session_id": sid, "reason": payload.get("reason", ""),
                                       "at": _now(), "had_handoff": had})


def run(event):
    # The headless dream's own `claude -p` call loads the user's global
    # settings and so fires these same hooks; _headless_env sets this so the
    # dream's session never gets a working-set injection, a prompt-hook
    # session-id overwrite, or a session-end record of its own.
    if os.environ.get("HIPPO_IN_DREAM"):
        return 0
    payload = _payload()
    if payload is None:
        return 0
    try:
        if event == "session-start":
            out = session_start(payload)
            if out:
                print(json.dumps(out))
        elif event == "prompt":
            text = prompt(payload)
            if text:
                print(text, end="")
        elif event == "pre-compact":
            pre_compact(payload)
        elif event == "session-end":
            session_end(payload)
    except Exception:
        return 0
    return 0
