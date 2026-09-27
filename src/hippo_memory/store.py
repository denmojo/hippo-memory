"""SQLite store: schema, connection, CRUD, watermark, rescore."""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from hippo_memory import config, salience

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memories (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  kind            TEXT NOT NULL CHECK (kind IN ('episodic','project','interest','lesson','handoff')),
  title           TEXT NOT NULL,
  body            TEXT NOT NULL DEFAULT '',
  status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  weight          REAL NOT NULL DEFAULT 0.0,
  recurrence      INTEGER NOT NULL DEFAULT 1,
  pinned          INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL,
  last_touched_at TEXT NOT NULL,
  salience        REAL NOT NULL DEFAULT 0.0,
  dedup_key       TEXT UNIQUE,
  folded_into     TEXT
);
CREATE TABLE IF NOT EXISTS links (
  from_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  to_id   INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  kind    TEXT NOT NULL DEFAULT 'relates',
  PRIMARY KEY (from_id, to_id, kind)
);
CREATE TABLE IF NOT EXISTS dream_state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS embeddings (
  source       TEXT NOT NULL CHECK (source IN ('hippo','stock')),
  ref          TEXT NOT NULL,
  dim          INTEGER NOT NULL,
  vec          BLOB NOT NULL,
  model        TEXT NOT NULL,
  source_mtime TEXT,
  embedded_at  TEXT NOT NULL,
  PRIMARY KEY (source, ref)
);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def connect(db_path=None):
    if db_path is None:
        db_path = os.environ.get("HIPPO_DB") or config.DB_PATH
    path = str(db_path)
    if path != ":memory:":
        data_dir = Path(path).parent
        is_new = not data_dir.exists()
        data_dir.mkdir(parents=True, exist_ok=True)
        if is_new:
            # Owner-only: the store built from the user's own transcripts
            # must not be world- or group-readable on a shared host, even
            # though the umask on a fresh directory would otherwise allow it.
            os.chmod(data_dir, 0o700)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# Every kind the CHECK admits. 'handoff' is the agent-written note to
# self: never scored, never dream-written, surfaced by rule in its own section.
KINDS = ("episodic", "project", "interest", "lesson", "handoff")


def init_schema(conn):
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    _migrate_kind_check(conn)
    _migrate_folded_into(conn)


def _migrate_folded_into(conn):
    """Add the folded_into column to pre-existing stores. Purely additive, so a
    plain ALTER does it: no CHECK to rebuild, unlike the lesson-kind migration."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(memories)")}
    if "folded_into" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN folded_into TEXT")
        conn.commit()


def _migrate_kind_check(conn):
    """Widen the memories kind CHECK to admit every kind in KINDS on
    pre-existing stores (lesson on 2026-07-12, handoff on 2026-08-25).

    CREATE TABLE IF NOT EXISTS leaves an existing table's constraint untouched,
    so a store created before a kind rejects its rows forever without this
    rebuild. SQLite cannot alter a CHECK in place; this is the documented
    copy-and-swap, run once per widening (the guard is the constraint text
    itself). links only references memories(id) and the id column survives the
    copy, so the FK edges are unaffected; foreign_keys goes OFF around the swap
    because DROP TABLE on the referenced table would otherwise fail."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories'"
    ).fetchone()
    if row is None or all(f"'{k}'" in row["sql"] for k in KINDS):
        return
    # Copy only the columns the OLD table has: the rebuilt table may be wider
    # (folded_into arrived after lesson), and a lesson-era store already carries
    # folded_into, which the copy must keep.
    old_cols = [r["name"] for r in conn.execute("PRAGMA table_info(memories)")]
    cols = ", ".join(old_cols)
    # New table first, drop old, rename new (the SQLite rebuild order): renaming
    # the OLD table instead would rewrite links' REFERENCES clause to the temp
    # name on SQLite >= 3.26 and leave it dangling after the drop.
    create_new = SCHEMA_SQL.split("CREATE TABLE IF NOT EXISTS links")[0].replace(
        "CREATE TABLE IF NOT EXISTS memories", "CREATE TABLE memories_migrating"
    )
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript(
            "BEGIN;\n"
            + create_new
            + f"INSERT INTO memories_migrating ({cols}) SELECT {cols} FROM memories;\n"
            "DROP TABLE memories;\n"
            "ALTER TABLE memories_migrating RENAME TO memories;\n"
            "COMMIT;"
        )
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def upsert_memory(conn, kind, title, body="", dedup_key=None,
                  weight=0.0, pinned=False):
    if kind == "handoff" and len(body) > config.HANDOFF_BODY_MAX:
        raise ValueError(
            f"handoff body is {len(body)} chars; cap is {config.HANDOFF_BODY_MAX}. "
            "That ceiling is a rail against a runaway write; check whether a "
            "transcript or a loop got pasted in."
        )
    ts = now_iso()
    if dedup_key is not None:
        existing = conn.execute(
            "SELECT id FROM memories WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        if existing:
            mid = existing["id"]
            conn.execute(
                "UPDATE memories SET recurrence = recurrence + 1, "
                "last_touched_at = ? WHERE id = ?",
                (ts, mid),
            )
            conn.commit()
            return mid
    cur = conn.execute(
        "INSERT INTO memories (kind, title, body, weight, pinned, "
        "created_at, last_touched_at, dedup_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, title, body, weight, 1 if pinned else 0, ts, ts, dedup_key),
    )
    conn.commit()
    return cur.lastrowid


def get_memory(conn, mid):
    return conn.execute("SELECT * FROM memories WHERE id = ?", (mid,)).fetchone()


def close_memory(conn, mid, note=None):
    """Close a memory. `note` (e.g. a graduation pointer at a shipped guard,
    or a retirement stamp) is appended to the body so recall keeps the exit
    story with the lesson."""
    if note:
        conn.execute(
            "UPDATE memories SET body = CASE WHEN body = '' THEN ? "
            "ELSE body || char(10) || ? END WHERE id = ?",
            (note, note, mid),
        )
    conn.execute(
        "UPDATE memories SET status = 'closed', last_touched_at = ? WHERE id = ?",
        (now_iso(), mid),
    )
    conn.commit()


def set_weight(conn, mid, weight):
    """Set a memory's manual weight and re-arm its recency. Status is untouched:
    a closed row stays closed. Caller rescores to reflect the new salience."""
    conn.execute(
        "UPDATE memories SET weight = ?, last_touched_at = ? WHERE id = ?",
        (float(weight), now_iso(), mid),
    )
    conn.commit()


def merge_memory(conn, from_id, into_id):
    """Fold `from_id` into `into_id` non-destructively (reversible via snapshot).

    Re-points the absorbed memory's links onto the survivor, folds its recurrence
    in, closes the absorbed row (kept, not deleted), and records a 'merged' edge.
    Callers must ensure neither id is pinned (the floor is never merged)."""
    ts = now_iso()
    conn.execute("UPDATE OR IGNORE links SET from_id = ? WHERE from_id = ?", (into_id, from_id))
    conn.execute("UPDATE OR IGNORE links SET to_id = ? WHERE to_id = ?", (into_id, from_id))
    conn.execute("DELETE FROM links WHERE from_id = to_id")
    row = conn.execute("SELECT recurrence FROM memories WHERE id = ?", (from_id,)).fetchone()
    if row:
        conn.execute(
            "UPDATE memories SET recurrence = recurrence + ?, last_touched_at = ? WHERE id = ?",
            (row["recurrence"], ts, into_id),
        )
    conn.execute(
        "UPDATE memories SET status = 'closed', last_touched_at = ? WHERE id = ?",
        (ts, from_id),
    )
    conn.execute(
        "INSERT OR IGNORE INTO links (from_id, to_id, kind) VALUES (?, ?, 'merged')",
        (into_id, from_id),
    )
    conn.commit()


def count_links(conn, mid):
    return conn.execute(
        "SELECT count(*) FROM links WHERE from_id = ? OR to_id = ?", (mid, mid)
    ).fetchone()[0]


def add_link(conn, from_id, to_id, kind="relates"):
    conn.execute(
        "INSERT OR IGNORE INTO links (from_id, to_id, kind) VALUES (?, ?, ?)",
        (from_id, to_id, kind),
    )
    conn.commit()


def set_state(conn, key, value):
    conn.execute(
        "INSERT INTO dream_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_state(conn, key, default=None):
    row = conn.execute(
        "SELECT value FROM dream_state WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def rescore_all(conn, now=None):
    if now is None:
        now = datetime.now(timezone.utc)
    rows = conn.execute("SELECT * FROM memories").fetchall()
    for row in rows:
        s = salience.score(
            row, now, config.WEIGHTS, config.RECENCY_HALFLIFE_DAYS,
            config.INTEREST_BASELINE, config.INTEREST_HALFLIFE_DAYS,
            config.LESSON_BASELINE, config.LESSON_HALFLIFE_DAYS,
        )
        conn.execute(
            "UPDATE memories SET salience = ? WHERE id = ?", (s, row["id"])
        )
    conn.commit()


# Sort orders for ranked(); every option ends in a deterministic tiebreak.
_RANKED_ORDERS = {
    "salience": "salience DESC, last_touched_at DESC",
    "recency": "last_touched_at DESC, salience DESC",
    "id": "id ASC",
}


def ranked(conn, status=None, limit=None, kind=None, since=None,
           touched_since=None, min_salience=None, order="salience"):
    """Filtered, ordered rows from the store.

    All filters are optional and compose as AND clauses. ``since`` and
    ``touched_since`` are ISO-date floors on created_at / last_touched_at
    (string comparison works because both columns are ISO-8601). ``order``
    is a key in _RANKED_ORDERS; salience is the default and the pre-flag
    behavior."""
    sql = "SELECT * FROM memories"
    clauses, params = [], []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    if since is not None:
        clauses.append("created_at >= ?")
        params.append(since)
    if touched_since is not None:
        clauses.append("last_touched_at >= ?")
        params.append(touched_since)
    if min_salience is not None:
        clauses.append("salience >= ?")
        params.append(min_salience)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY " + _RANKED_ORDERS[order]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def upsert_embedding(conn, source, ref, dim, vec, model, source_mtime=None):
    conn.execute(
        "INSERT INTO embeddings (source, ref, dim, vec, model, source_mtime, embedded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(source, ref) DO UPDATE SET "
        "dim=excluded.dim, vec=excluded.vec, model=excluded.model, "
        "source_mtime=excluded.source_mtime, embedded_at=excluded.embedded_at",
        (source, ref, dim, vec, model, source_mtime, now_iso()),
    )
    conn.commit()


def get_embeddings(conn, source=None):
    if source is None:
        return conn.execute("SELECT * FROM embeddings").fetchall()
    return conn.execute(
        "SELECT * FROM embeddings WHERE source = ?", (source,)
    ).fetchall()


def get_embedding(conn, source, ref):
    return conn.execute(
        "SELECT * FROM embeddings WHERE source = ? AND ref = ?", (source, ref)
    ).fetchone()


def unembedded_hippo(conn, model):
    """How many memories semantic recall cannot see: rows with no vector, or a
    vector from a different model. A row counted here is invisible to `search`
    and `recall` however high its salience. Folded rows are excluded: they are
    unembedded on purpose, and coverage must not chase a deliberate hole."""
    return conn.execute(
        "SELECT COUNT(*) FROM memories m "
        "LEFT JOIN embeddings e "
        "  ON e.source = 'hippo' AND e.ref = CAST(m.id AS TEXT) AND e.model = ? "
        "WHERE e.ref IS NULL AND m.folded_into IS NULL",
        (model,),
    ).fetchone()[0]


def delete_embedding(conn, source, ref):
    conn.execute(
        "DELETE FROM embeddings WHERE source = ? AND ref = ?", (source, ref)
    )
    conn.commit()


def fold_memory(conn, mid, into, note=None):
    """Retire a memory that a durable rule already covers.

    Closing alone does not retire a duplicate: `search` ranks on relevance and
    does not filter by status, so a closed row keeps competing with (and can
    outrank) the very rule it restates. Folding closes the row, records what
    absorbed it, and drops its vector, so it leaves the retrieval pool while the
    body survives in the store as the evidence behind the rule. `index_hippo`
    honours the marker, so a later indexing pass cannot resurrect the vector.

    This is the retroactive arm of `crossdedup`, which until now could only stop
    a satellite at add time and had no way to retire one already in the store.
    """
    row = get_memory(conn, mid)
    if row is None:
        raise ValueError(f"no memory {mid}")
    body = row["body"] or ""
    if note:
        body = f"{body}\n\n{note}".strip()
    conn.execute(
        "UPDATE memories SET status='closed', folded_into=?, body=?, "
        "last_touched_at=? WHERE id=?",
        (into, body, now_iso(), mid),
    )
    conn.commit()
    delete_embedding(conn, "hippo", str(mid))
    return get_memory(conn, mid)
