"""Recall smoke test: run probe queries and assert the expected memory is rank 1.

Pure logic - no DB or embedder. Probe loading, per-probe evaluation against a
hit list (as returned by search.run), rendering, and the process exit code.
The caller (cli.cmd_check) supplies the live hits.
"""
import json

_REQUIRED = ("query", "expect", "source")
_SOURCES = ("stock", "hippo")


def load_probes(path):
    """Parse and validate the probe list. Each entry needs query/expect/source,
    with source in {stock, hippo}. Raises ValueError on a malformed entry."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("probe file must be a JSON list")
    for i, p in enumerate(data):
        missing = [k for k in _REQUIRED if k not in p]
        if missing:
            raise ValueError(f"probe {i}: missing field(s) {missing}")
        if p["source"] not in _SOURCES:
            raise ValueError(f"probe {i}: source must be one of {_SOURCES}")
    return data


def _matches(hit, probe):
    if hit["source"] != probe["source"]:
        return False
    if probe["source"] == "stock":
        return hit["title"] == probe["expect"]
    return probe["expect"].lower() in hit["title"].lower()


def evaluate(probe, hits):
    """Compare a probe against its ranked hits. PASS only when the rank-1 hit
    matches both the expected source and answer. expected_rank is the 1-based
    position where the expected item appears (for failure reporting), or None."""
    top = hits[0] if hits else None
    passed = top is not None and _matches(top, probe)
    expected_rank = next(
        (i + 1 for i, h in enumerate(hits) if _matches(h, probe)), None
    )
    return {"probe": probe, "passed": passed, "top": top,
            "expected_rank": expected_rank}


def _line(r):
    p = r["probe"]
    q = p["query"][:34]
    if r["passed"]:
        t = r["top"]
        return f"PASS  [{p['source']}] {q} -> {t['title']} (rel {t['relevance']:.3f})"
    if r["top"] is None:
        got = "no hits"
    else:
        got = f"got [{r['top']['source']}] {r['top']['title']} (rel {r['top']['relevance']:.3f})"
    where = f"at rank {r['expected_rank']}" if r["expected_rank"] else "not in top-k"
    return (f"FAIL  [{p['source']}] {q} -> {got}; "
            f"expected {p['source']} \"{p['expect']}\" {where}")


def render(results):
    """Per-probe PASS/FAIL lines plus per-source and total counts."""
    lines = [_line(r) for r in results]
    counts = {}
    for r in results:
        src = r["probe"]["source"]
        passed, total = counts.get(src, (0, 0))
        counts[src] = (passed + (1 if r["passed"] else 0), total + 1)
    n_pass = sum(1 for r in results if r["passed"])
    summary = "   ".join(f"{src} {p}/{t}" for src, (p, t) in sorted(counts.items()))
    lines.append(f"{summary}   total {n_pass}/{len(results)}")
    return "\n".join(lines)


def coverage_line(unembedded, total, stale=0):
    """Probes sample the index; they cannot detect a hole they never touch. The
    2026-07-13 check passed 7/7 while 157 of 233 rows had no vector, because
    every probe pointed at an older embedded row. So coverage is asserted
    directly rather than inferred from probe results.

    Two ways to be uncoverable: no vector at all, or a vector that has drifted
    from an edited body. Both leave a memory unfindable by what it says now.
    """
    faults = []
    if unembedded:
        faults.append(f"{unembedded}/{total} have no vector")
    if stale:
        faults.append(f"{stale} carry a vector that drifted from their text")
    if faults:
        return (f"FAIL  [coverage] {'; '.join(faults)}; semantic recall cannot "
                f"find them - run `hippo index`")
    return f"PASS  [coverage] all {total} memories embedded and current"


def exit_code(results, unembedded=0, stale=0):
    if unembedded or stale:
        return 1
    return 0 if all(r["passed"] for r in results) else 1
