"""Pure vector ops: pack/unpack float32, cosine, top-k, min-max normalize.

No database access and no model. Brute-force cosine is intentional: at this
scale (hundreds to low thousands of vectors) a linear scan is sub-millisecond
and a vector database would be overkill.
"""
import math
from array import array


def pack(vec):
    return array("f", vec).tobytes()


def unpack(blob):
    a = array("f")
    a.frombytes(blob)
    return list(a)


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def top_k(query_vec, candidates, k=None):
    scored = [(ref, cosine(query_vec, vec)) for ref, vec in candidates]
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored if k is None else scored[:k]


def minmax_norm(values):
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]
