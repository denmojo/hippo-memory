"""Deterministic lesson lifecycle passes.

Charged corrections get their own memory kind with reserved working-set
seats; this module gives them a lifecycle. Two passes, all orchestrator code
with no model judgment:

Re-offense re-arming: a dream add that folds onto an existing open lesson (by
dedup_key in apply, or by semantic fold here) does not file a sibling row; it
bumps the matched lesson's weight by LESSON_REARM_WEIGHT up to
LESSON_WEIGHT_CEILING, increments recurrence, and re-arms recency. Repeated
trauma strengthens, it does not merely refresh, and the counter's integrity
does not depend on the model remembering to reuse a dedup_key.

Graduation: the only exits. (a) Enforce-first promotion: the user (or a
session acting on instruction) closes the lesson with a note pointing at the
shipped guard; manual closes do not pass the dream validator, so the
refractory period does not apply to them. (b) Clean-conduct retirement: a
lesson untouched for LESSON_RETIREMENT_DAYS (~2 half-lives) closes
automatically with a Journal line; the behavior corrected itself.
"""
from datetime import datetime, timezone

from hippo_memory import config, retrieve, store


def open_lessons(conn):
    return conn.execute(
        "SELECT * FROM memories WHERE kind = 'lesson' AND status = 'open'"
    ).fetchall()


def rearm(conn, mid, bump=None, ceiling=None):
    """Apply one re-offense to lesson `mid`: weight up (capped), recurrence up,
    recency re-armed. Returns the new weight."""
    if bump is None:
        bump = config.LESSON_REARM_WEIGHT
    if ceiling is None:
        ceiling = config.LESSON_WEIGHT_CEILING
    row = store.get_memory(conn, mid)
    new_weight = min(float(row["weight"]) + bump, ceiling)
    # status flips back to open: a re-offense against a retired (closed) lesson
    # is a relapse, and a closed row would neither surface nor escalate.
    conn.execute(
        "UPDATE memories SET weight = ?, recurrence = recurrence + 1, "
        "status = 'open', last_touched_at = ? WHERE id = ?",
        (new_weight, store.now_iso(), mid),
    )
    conn.commit()
    return new_weight


def fold_reoffenses(conn, adds, embedder, threshold=None):
    """Split plan adds into (kept, folds): lesson adds that match an existing
    OPEN lesson by cosine similarity become folds ({"add", "id", "title", "cosine"})
    instead of sibling rows. Pure annotation: the caller applies the re-arms,
    so an aborted plan leaves the store untouched.

    Open lessons are embedded live from title+body rather than read from the
    embeddings table, so a lesson filed last night still folds tonight even if
    no index run happened in between; the corpus is small (the reserved tier
    plus overflow) so the cost is a handful of texts."""
    if threshold is None:
        threshold = config.LESSON_FOLD_THRESHOLD
    lesson_adds = [a for a in adds if a.get("kind") == "lesson"]
    rows = open_lessons(conn) if lesson_adds else []
    if not lesson_adds or not rows:
        return list(adds), []
    texts = [f"{(a.get('title') or '').strip()}\n{a.get('body') or ''}"
             for a in lesson_adds]
    row_texts = [f"{r['title']}\n{r['body']}" for r in rows]
    vecs = embedder.embed(texts + row_texts)
    add_vecs, row_vecs = vecs[:len(texts)], vecs[len(texts):]

    folds, folded_ids = [], set()
    for a, v in zip(lesson_adds, add_vecs):
        best_row, best_cos = None, -1.0
        for r, rv in zip(rows, row_vecs):
            c = retrieve.cosine(v, rv)
            if c > best_cos:
                best_row, best_cos = r, c
        if best_row is not None and best_cos >= threshold:
            folds.append({"add": a, "id": best_row["id"],
                          "title": best_row["title"], "cosine": best_cos})
            folded_ids.add(id(a))
    kept = [a for a in adds if id(a) not in folded_ids]
    return kept, folds


def retire_clean(conn, days=None, now=None):
    """Close every open lesson untouched for `days` (clean conduct: no
    re-offense re-armed it). Appends a RETIRED note to the body and returns
    the retired rows."""
    if days is None:
        days = config.LESSON_RETIREMENT_DAYS
    if now is None:
        now = datetime.now(timezone.utc)
    retired = []
    for r in open_lessons(conn):
        touched = datetime.fromisoformat(r["last_touched_at"])
        if touched.tzinfo is None:
            touched = touched.replace(tzinfo=timezone.utc)
        if (now - touched).total_seconds() / 86400.0 < days:
            continue
        store.close_memory(
            conn, r["id"],
            note=f"RETIRED {now.date().isoformat()}: {days} days clean",
        )
        conn.commit()
        retired.append(r)
    return retired
