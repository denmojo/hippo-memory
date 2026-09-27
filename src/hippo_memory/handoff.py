"""Notes to self: handoff rows written by the in-session agent.

A handoff is the working instance's own close-out, written while it still holds
the context: state, decisions and why, dead ends, assumptions, next step,
calibration observations, and what it does not know. The dream reads these as a
session's primary account; it never writes one (dream.VALID_KINDS excludes the
kind), and salience.score returns 0.0 for the kind, so a handoff never competes
in the ranked tiers. The fourth working-set section draws from ``select`` here,
by rule rather than score.
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from hippo_memory import config, store

LABELS = ("State", "Decided", "Dead ends", "Assumed", "Next", "Calibration", "Unsure")
_LABEL_RE = re.compile(r"^([A-Za-z][A-Za-z ]{0,20}):\s?(.*)$")
_LABEL_SET = {l.lower(): l for l in LABELS}


class HandoffError(Exception):
    """A refusal the CLI prints plainly and exits 1 on."""


def parse_body(body):
    """Validate a labelled body and return {Label: text}.

    Lines that open with ``<Label>:`` start a section; other non-empty lines
    continue the current section. An opener whose label is not in LABELS is
    refused (no invented fields), as is a body with no label at all or one
    over the character cap."""
    if len(body) > config.HANDOFF_BODY_MAX:
        raise HandoffError(
            f"body is {len(body)} chars; cap is {config.HANDOFF_BODY_MAX}. "
            "That ceiling is a rail against a runaway write; check whether a "
            "transcript or a loop got pasted in."
        )
    sections, current = {}, None
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _LABEL_RE.match(line)
        if m:
            key = m.group(1).strip().lower()
            if key not in _LABEL_SET:
                raise HandoffError(
                    f"unknown label {m.group(1).strip()!r}; allowed: " + ", ".join(LABELS)
                )
            current = _LABEL_SET[key]
            sections[current] = m.group(2).strip()
            continue
        if current is None:
            raise HandoffError(
                "body must open with a label; allowed: " + ", ".join(LABELS)
            )
        sections[current] = (sections[current] + "\n" + line).strip()
    if not sections:
        raise HandoffError("empty body; allowed labels: " + ", ".join(LABELS))
    return sections


def resolve_session(explicit, path=None):
    """The session a note belongs to: --session if given, else the first line
    of the file hippo-session-track.sh maintains, else a refusal. Never
    invented."""
    if explicit:
        return explicit
    # Env is read at call time (not import) so a test or a one-off override can
    # point at another file without reloading config.
    p = Path(path or os.environ.get("HIPPO_CURRENT_SESSION") or config.CURRENT_SESSION_PATH)
    try:
        first = p.read_text().splitlines()[0].strip()
    except (OSError, IndexError):
        first = ""
    if not first:
        raise HandoffError(
            "no session id: pass --session or make sure hippo-session-track.sh "
            f"has written {p}"
        )
    return first


def _next_seq(conn, session):
    n = conn.execute(
        "SELECT count(*) FROM memories WHERE kind='handoff' AND dedup_key LIKE ?",
        (f"handoff:{session}:%",),
    ).fetchone()[0]
    return n + 1


def add(conn, title, body, session, carries=()):
    """Write one handoff row and its carries links. Validates the body and
    that every carried id exists. Returns the new id."""
    parse_body(body)
    title = (title or "").strip()
    if not title:
        raise HandoffError("title is required")
    carries = [int(c) for c in carries or ()]
    for c in carries:
        if store.get_memory(conn, c) is None:
            raise HandoffError(f"carried id {c} does not exist")
    key = f"handoff:{session}:{_next_seq(conn, session)}"
    mid = store.upsert_memory(conn, "handoff", title, body=body, dedup_key=key)
    conn.execute("UPDATE memories SET salience = 0.0 WHERE id = ?", (mid,))
    for c in carries:
        store.add_link(conn, mid, c, kind="carries")
    conn.commit()
    return mid


def carries(conn, mid):
    return [r["to_id"] for r in conn.execute(
        "SELECT to_id FROM links WHERE from_id = ? AND kind = 'carries' ORDER BY to_id",
        (mid,),
    )]


def list_rows(conn, session=None, include_closed=False):
    sql = "SELECT * FROM memories WHERE kind = 'handoff'"
    params = []
    if not include_closed:
        sql += " AND status = 'open'"
    if session:
        sql += " AND dedup_key LIKE ?"
        params.append(f"handoff:{session}:%")
    sql += " ORDER BY created_at DESC, id DESC"
    return conn.execute(sql, params).fetchall()


def latest(conn, session=None):
    rows = list_rows(conn, session=session)
    return rows[0] if rows else None


def close(conn, mid, note=None):
    row = store.get_memory(conn, mid)
    if row is None or row["kind"] != "handoff":
        raise HandoffError(f"no handoff with id {mid}")
    store.close_memory(conn, mid, note=note)


def select(conn, cap=None):
    """The fourth-section rule: every open handoff carrying at least one
    still-open loop, newest first; plus the single newest open handoff that
    carries nothing; ceiling ``cap`` as a safety only."""
    cap = config.WS_HANDOFF_CAP if cap is None else cap
    picked, free = [], None
    for r in list_rows(conn):
        ids = carries(conn, r["id"])
        if not ids:
            if free is None:
                free = r
            continue
        live = conn.execute(
            "SELECT count(*) FROM memories WHERE status='open' AND id IN (%s)"
            % ",".join("?" * len(ids)), ids,
        ).fetchone()[0]
        if live:
            picked.append(r)
    if free is not None:
        picked.append(free)
    picked.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
    return picked[:cap]


def _stamp(iso):
    """created_at is stored in UTC; render in the reader's local time."""
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return (iso or "")[:16]
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M")


def format_line(row, carried_ids):
    """One working-set line: id, written-at (local), title, carried ids."""
    ids = list(carried_ids or ())
    tail = f"  (carries {', '.join(str(i) for i in ids)})" if ids else ""
    return f"- [{row['id']}] {_stamp(row['created_at'])}  {row['title']}{tail}"


def render_lines(conn, rows):
    """format_line over rows, looking carried ids up from the store."""
    return [format_line(r, carries(conn, r["id"])) for r in rows]


SECTION_HEADER = "## Notes to self (claude-written, prior sessions)"


# --- dream integration ------------------------------------------------------

def session_of(dedup_key):
    """'handoff:<session>:<n>' -> '<session>'."""
    if not dedup_key or not dedup_key.startswith("handoff:"):
        return ""
    return dedup_key[len("handoff:"):].rsplit(":", 1)[0]


def material_block(conn, session):
    """The session's handoff rows as dream material, placed before the digest
    so the dream reads the agent's own account first. Empty string if none."""
    rows = list_rows(conn, session=session, include_closed=True)
    if not rows:
        return ""
    lines = [
        f"--- HANDOFF NOTES (agent-written, session {session}; the session's "
        "primary account, read before the digest; the transcript wins on "
        "contradiction) ---"
    ]
    for r in rows:
        ids = carries(conn, r["id"])
        tail = f"  carries {','.join(map(str, ids))}" if ids else ""
        lines.append(f"[{r['id']}] {r['status']} {_stamp(r['created_at'])}  {r['title']}{tail}")
        lines.append((r["body"] or "").rstrip())
    return "\n".join(lines)


def _aware(iso):
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def housekeep(conn, now=None, cycle_hours=None):
    """Deterministic handoff lifecycle, run by the dream after apply.

    Closes notes whose carried loops have all closed; folds every older open
    note of a session into that session's newest; closes uncarried notes older
    than one cycle (they ride once). Returns what it did, for the journal."""
    now = now or datetime.now(timezone.utc)
    hours = config.HANDOFF_UNCARRIED_HOURS if cycle_hours is None else cycle_hours
    result = {"closed_done": [], "superseded": [], "closed_uncarried": []}
    today = now.date().isoformat()

    for r in list_rows(conn):
        ids = carries(conn, r["id"])
        if not ids:
            continue
        live = conn.execute(
            "SELECT count(*) FROM memories WHERE status='open' AND id IN (%s)"
            % ",".join("?" * len(ids)), ids,
        ).fetchone()[0]
        if live == 0:
            store.close_memory(conn, r["id"],
                               note=f"CLOSED {today}: every carried loop closed")
            result["closed_done"].append(r["id"])

    by_session = {}
    for r in list_rows(conn):                       # open, newest first
        by_session.setdefault(session_of(r["dedup_key"]), []).append(r)
    for sess, rows in by_session.items():
        newest = rows[0]
        for old in rows[1:]:
            store.fold_memory(conn, old["id"], into=f"handoff:{newest['id']}",
                              note=f"SUPERSEDED {today} by [{newest['id']}]")
            result["superseded"].append((old["id"], newest["id"]))

    for r in list_rows(conn):
        if carries(conn, r["id"]):
            continue
        age_h = (now - _aware(r["created_at"])).total_seconds() / 3600.0
        if age_h >= hours:
            store.close_memory(conn, r["id"], note=f"CLOSED {today}: rode one cycle uncarried")
            result["closed_uncarried"].append(r["id"])
    return result


def session_end_reason(session, path=None):
    """The reason recorded by hippo-sessionend.sh for the session's most recent
    end, or None if the session has no record."""
    p = Path(path or os.environ.get("HIPPO_SESSION_ENDS") or config.SESSION_ENDS_PATH)
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return None
    reason = None
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("session_id") == session:
            reason = rec.get("reason") or None
    return reason


def render_section(conn, rows):
    """The section block as it will appear in the working-set; empty string
    when there are no rows."""
    if not rows:
        return ""
    return "\n".join([SECTION_HEADER] + render_lines(conn, rows)) + "\n"
