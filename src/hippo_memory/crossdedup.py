"""Cross-store dedup pass at dream apply time.

The store grows satellites of stock rules faster than blend weights can be
retuned: a hippo row paraphrasing a stock feedback file competes with the rule
itself in ranked recall, and the salience bonus can let the paraphrase outrank
the rule it restates unless TIEBREAK_EPS holds. This pass stops new satellites
at the door: every planned add is embedded and
compared against the stock vectors already in the embeddings table; at or above
the threshold the row is NOT filed and is escalated in the Dream Journal as a
merge-candidate naming the parent stock file. Stock files are never auto-edited
(the pinned-floor discipline): the escalation is a prompt for review, not an
automatic merge.

Only vectors stamped with the current model are compared, matching search.py's
rule that a model change never mixes incompatible embeddings. Stale stock
vectors (file edited since its last index) still divert correctly in spirit:
the comparison is against the rule as last embedded, and a false negative just
means the satellite survives until the next indexed dream, the pre-pass
failure mode this whole module accepts (over-surface later, never block).
"""
from hippo_memory import retrieve, store


def stock_vectors(conn, model):
    """All stock-source vectors stamped with `model`, as (ref, vec) pairs."""
    return [
        (r["ref"], retrieve.unpack(r["vec"]))
        for r in store.get_embeddings(conn, source="stock")
        if r["model"] == model
    ]

def divert(conn, adds, embedder, model, threshold):
    """Split plan adds into (kept, diversions).

    A diversion is {"add", "ref", "cosine"}: the add whose title+body scored at
    or above `threshold` cosine against a stock vector, the stock file ref it
    paraphrases, and the score. With no stock vectors for `model` every add is
    kept: fail-open, the dream must never lose material to a missing index.
    """
    stocks = stock_vectors(conn, model)
    if not stocks or not adds:
        return list(adds), []
    texts = [
        f"{(a.get('title') or '').strip()}\n{a.get('body') or ''}" for a in adds
    ]
    vecs = embedder.embed(texts)
    kept, diversions = [], []
    for a, v in zip(adds, vecs):
        ref, cos = max(
            ((ref, retrieve.cosine(v, sv)) for ref, sv in stocks),
            key=lambda t: t[1],
        )
        if cos >= threshold:
            diversions.append({"add": a, "ref": ref, "cosine": cos})
        else:
            kept.append(a)
    return kept, diversions
