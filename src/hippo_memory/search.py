"""Relevance retrieval blended with salience, plus a lexical fallback.

``run`` is pure given an embedder; the embedder is injected so this stays
model-free and testable. Only vectors stamped with the query's model are
considered, so a model change never mixes incompatible embeddings.

``lexical`` needs no embedder at all: a plain substring match over open
memories, ordered by salience. It is what recall falls back to when
``embed.get_embedder`` raises ``EmbedderUnavailable`` (no model installed),
so semantic search being unavailable never means search is unavailable.
"""
from hippo_memory import config, retrieve, store


def _candidates(conn, source, model):
    rows = store.get_embeddings(conn, source=None if source == "all" else source)
    sal = {str(m["id"]): m["salience"] for m in store.ranked(conn)}
    out = []
    for r in rows:
        if r["model"] != model:
            continue
        meta = {
            "source": r["source"],
            "ref": r["ref"],
            "salience": sal.get(r["ref"], 0.0) if r["source"] == "hippo" else 0.0,
        }
        if r["source"] == "hippo":
            m = store.get_memory(conn, int(r["ref"]))
            meta["title"] = m["title"] if m else r["ref"]
            meta["status"] = m["status"] if m else None
        else:
            meta["title"] = r["ref"].rsplit("/", 1)[-1]
            meta["status"] = None
        out.append((meta, retrieve.unpack(r["vec"])))
    return out


def run(conn, query, embedder, model, k=10, source="all", status=None):
    # relevance is raw cosine (absolute and interpretable). salience becomes a
    # bounded absolute factor s/(s+scale) in [0,1), not a min-max over the set, so
    # equal saliences map to equal factors and cannot invert a relevance win. Stock
    # rows have salience 0, so they rank purely on cosine.
    cands = _candidates(conn, source, model)
    if status:
        cands = [(m, v) for m, v in cands if m.get("status") in (status, None)]
    if not cands:
        return []
    qv = embedder.embed([query])[0]
    w = config.BLEND
    scale = config.SALIENCE_SCALE
    hits = []
    for m, vec in cands:
        rel = retrieve.cosine(qv, vec)
        s = m["salience"]
        sal_factor = s / (s + scale)
        h = dict(m)
        h["relevance"] = rel
        h["salience_factor"] = sal_factor
        h["blended"] = w["relevance"] * rel + min(
            w["salience"] * sal_factor, config.TIEBREAK_EPS
        )
        hits.append(h)
    hits.sort(key=lambda h: h["blended"], reverse=True)
    return hits[:k]


def lexical(conn, query, limit=10):
    """Substring search over open memories, no embedder required.

    Every term in the (lowercased) query must match title or body; matches
    are ordered by salience then recency, same tiebreak as ``store.ranked``.
    """
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return []
    where = " AND ".join("(lower(title) LIKE ? OR lower(body) LIKE ?)" for _ in terms)
    params = []
    for t in terms:
        params += [f"%{t}%", f"%{t}%"]
    rows = conn.execute(
        f"SELECT * FROM memories WHERE status='open' AND {where} "
        f"ORDER BY salience DESC, last_touched_at DESC LIMIT ?", params + [limit]
    ).fetchall()
    return [dict(r) for r in rows]
