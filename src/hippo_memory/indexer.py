"""Embed stock auto-memory files and hippo rows into the embeddings table.

Skips items already embedded with the same model and the same source fingerprint,
so re-indexing is idempotent while a model change or an edit forces a re-embed.
The fingerprint is the file mtime for stock files and a content hash for hippo
rows, which have no mtime to key on.
"""
import hashlib
from pathlib import Path

from hippo_memory import retrieve, store


def _fresh(row, model, source_mtime):
    return (
        row is not None
        and row["model"] == model
        and row["source_mtime"] == source_mtime
    )


def _hippo_text(row):
    return f"{row['title']}\n{row['body']}"


def _fingerprint(text):
    """Content hash standing in for a hippo row's mtime. Keying freshness on the
    model name alone (hippo rows always passed source_mtime=None) meant an edited
    row kept its old vector forever: the memory stayed retrievable by what it used
    to say, and the drift was silent."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stale_hippo(conn, model):
    """Rows whose vector no longer matches their text (or is missing a
    fingerprint), so `check` can see drift instead of trusting a green run."""
    n = 0
    for row in store.ranked(conn):
        if row["folded_into"] is not None:
            continue
        existing = store.get_embedding(conn, "hippo", str(row["id"]))
        if existing is None:
            continue  # a missing vector is a coverage hole, counted elsewhere
        if not _fresh(existing, model, _fingerprint(_hippo_text(row))):
            n += 1
    return n


def index_stock(conn, memory_dir, embedder, model):
    added = 0
    for p in sorted(Path(memory_dir).glob("*.md")):
        if p.name == "MEMORY.md":
            continue
        ref = str(p)
        mtime = str(p.stat().st_mtime)
        if _fresh(store.get_embedding(conn, "stock", ref), model, mtime):
            continue
        vec = embedder.embed([p.read_text()])[0]
        store.upsert_embedding(conn, "stock", ref, len(vec),
                               retrieve.pack(vec), model, mtime)
        added += 1
    return added


def index_hippo(conn, embedder, model):
    added = 0
    for row in store.ranked(conn):
        ref = str(row["id"])
        # A folded row was retired from the retrieval pool on purpose. Re-embedding
        # it here would walk the duplicate straight back into recall on the next
        # dream, silently undoing the fold.
        if row["folded_into"] is not None:
            continue
        text = _hippo_text(row)
        fp = _fingerprint(text)
        if _fresh(store.get_embedding(conn, "hippo", ref), model, fp):
            continue
        vec = embedder.embed([text])[0]
        store.upsert_embedding(conn, "hippo", ref, len(vec),
                               retrieve.pack(vec), model, fp)
        added += 1
    return added
