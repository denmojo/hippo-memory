"""Deterministic recurrence + external-artifact pre-pass over a session .jsonl.

Closes the gap interest.py leaves. interest.py scans USER turns only, but new
information can arrive in content the user merely points at (a
WebFetch'd announcement, a search result, a pasted page). That is exactly the
class of miss that bit us on 2026-06-12: a government order cutting Fable 5
access was judged from the *fetch action* ("ran a news brief") and silently
dropped, instead of from the fetched *content*.

This scanner reads ALL session text - user turns, assistant prose, tool_use
inputs, and tool_result bodies - and surfaces two things the dream must account
for before it may drop anything:

  1. recurrence - text that lexically matches an already-tracked open memory.
                  The thread came up again: promote, recur, or justify dropping.
  2. artifact   - an external fetch/search result or a user-supplied URL. Its
                  CONTENT must be read before classification; it may never be
                  judged from the action that carried it.

Broad by design (Phase 1 is lexical, no embeddings): distinctive-token overlap,
a light fuzzy pass for variants, and a small alias seed for multi-name threads.
It decides nothing. Over-surfacing is the accepted cost of never silently
dropping a tracked thread. Embedding nearest-neighbor is the Phase-2 upgrade and
would live here, not in a separate hybrid search (which would index notes, not
sessions or the memory store).
"""
import json
import re

# Structural stopwords plus generic memory-title connective words that would
# otherwise match almost any turn ("shipped", "concern", "launch"...). Keeping
# the keyword sets to distinctive, thread-identifying tokens is what keeps the
# broad scan from flagging every line (everything flagged == nothing flagged).
_STOP = set(
    """a an the of to and or for with on in at by from is are was were be been being
    this that these those it its as into over under about after before more most some
    any all his her their our your my me you he she we they them him i not no do does
    did has have had will would can could should may might must just than then so up
    out now per via also only both each new
    launch launched shipped ship build built building focus shift shifted concern
    concerns pattern thread threads project projects venture access page update updated
    note notes thing things stuff immediate adoption awareness standing topical
    claude code backup done goal book books day live
    found fully same first second third tool tools system store shop site sites
    full open close closed gate review reviewed week month year june july work working""".split()
)

# A token shared across this many or more distinct memories is not thread-
# identifying (it is corpus-generic), so it is auto-dropped from every keyword
# set during indexing - the bright-line guard against noise a manual stoplist
# misses. Tunable during the observation period.
_DF_DROP = 5

_FETCH_TOOLS = re.compile(
    r"(WebFetch|WebSearch|firecrawl_\w+|web_search_exa|web_fetch_exa|"
    r"ask_grok|ask_openai|ask_gemini)",
    re.I,
)
_URL = re.compile(r"https?://[^\s)>\]\"]+", re.I)

# Seeded aliases: tokens denoting the same tracked thread under a different name.
# Tiny and explicit on purpose - the curated escape hatch for what plain token
# overlap misses (a product's second name). Extend as threads reveal aliases.
ALIASES = {
    "fable": {"mythos"},
}


def tokens(text):
    toks = set()
    for w in re.split(r"[^a-z0-9]+", (text or "").lower()):
        if len(w) >= 4 and w not in _STOP:
            toks.add(w)
    return toks


def memory_keywords(title):
    """Identifying tokens for a memory: title tokens only. Bodies were tried and
    rejected - they dragged in corpus-generic proper nouns that matched every
    line. Titles are curated and discriminating."""
    return tokens(title)


def _expand_aliases(kw):
    for base, alts in ALIASES.items():
        if base in kw:
            kw = kw | alts
    return kw


def build_index(memories):
    """memories: iterable of dict-likes with id, title. Returns
    [(id, title, keyword_set)] for open threads. Tokens shared across >= _DF_DROP
    distinct memories are auto-pruned as corpus-generic, then aliases expanded."""
    raw = [(m["id"], m["title"], memory_keywords(m["title"])) for m in memories]
    df = {}
    for _id, _title, kw in raw:
        for t in kw:
            df[t] = df.get(t, 0) + 1
    generic = {t for t, n in df.items() if n >= _DF_DROP}
    idx = []
    for mid, title, kw in raw:
        pruned = _expand_aliases(kw - generic)
        if pruned:
            idx.append((mid, title, pruned))
    return idx


def strong_tokens(memories):
    """Tokens unique to a single memory and long enough to stand alone as a
    thread identifier (e.g. 'denaturalization'). A thread that matches only one
    such token still earns a place on the checklist; a short common token does
    not. Stemming/embedding breadth is deferred to Phase 2."""
    df = {}
    for m in memories:
        for t in memory_keywords(m["title"]):
            df[t] = df.get(t, 0) + 1
    return frozenset(t for t, n in df.items() if n == 1 and len(t) >= 8)


def match_text(text, index):
    """Return {mem_id: sorted matched tokens} for every tracked thread this text
    touches by exact distinctive-token overlap. Fuzzy matching was tried and cut:
    at any useful threshold it pulled false friends (detention~retention,
    impersonal~personal) that cost more than the variant-catching it bought."""
    ttoks = tokens(text)
    hits = {}
    for mid, _title, kw in index:
        matched = ttoks & kw
        if matched:
            hits[mid] = sorted(matched)
    return hits


def _flatten(content):
    """Readable CONTENT under a message value: typed text, assistant prose, and
    tool_result bodies. Tool_use inputs are deliberately excluded - search
    queries, file paths, and the ToolSearch/JSON-schema plumbing they carry are
    not session content and were a noise source (a schema line matching
    'mobile'/'native'). Artifact detection reads tool_use names separately."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                parts.append(b.get("text", ""))
            elif t == "tool_result":
                parts.append(_flatten(b.get("content", "")))
            elif t != "tool_use" and "content" in b:
                parts.append(_flatten(b.get("content", "")))
        return " ".join(p for p in parts if p)
    return ""


def _all_text(obj):
    return _flatten(obj.get("message", {}).get("content", ""))


# Harness-injected user-role content that is not the user talking (command
# wrappers, system reminders, skill-load payloads). Mirrors interest.py.
_WRAPPER_PREFIXES = (
    "<local-command", "<command-", "<system-reminder", "caveat:",
    "base directory for this skill",
)


def _prose_text(obj):
    """What was SAID this turn: user-typed text and assistant prose.
    Excludes tool_result bodies (content merely read) and harness wrappers. A
    thread mentioned here was engaged with, rather than incidentally present in a
    file the assistant happened to read."""
    content = obj.get("message", {}).get("content", "")
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
    else:
        return ""
    text = " ".join(p for p in parts if p).strip()
    if obj.get("type") == "user" and text.lstrip().lower().startswith(_WRAPPER_PREFIXES):
        return ""
    return text


def _result_text(obj):
    """Tool_result bodies only: content the assistant read, not conversation.
    Matches here are incidental unless the same thread also appears in prose."""
    content = obj.get("message", {}).get("content", "")
    if isinstance(content, list):
        return " ".join(
            _flatten(b.get("content", "")) for b in content
            if isinstance(b, dict) and b.get("type") == "tool_result"
        )
    return ""


def _artifacts(obj):
    """External-content markers on this turn: (sorted fetch/search tool names +
    'url', representative url|None). An artifact's content must be read before it
    can be classified; it may never be judged from the fetch action alone."""
    kinds = set()
    url = None
    content = obj.get("message", {}).get("content", "")
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                if _FETCH_TOOLS.search(b.get("name", "")):
                    kinds.add(b.get("name", ""))
                    u = (b.get("input") or {}).get("url")
                    if u and not url:
                        url = u
    if obj.get("type") == "user":
        m = _URL.search(_all_text(obj))
        if m:
            kinds.add("url")
            url = url or m.group(0)
    return sorted(kinds), url


def scan_lines(numbered_lines, index):
    """Returns (recurrence, artifacts):
      recurrence: {mem_id: {"lines": [..], "tokens": set(), "prose": bool}}
        prose=True iff the thread was matched in conversation prose at least
        once, rather than only inside read tool-result bodies.
      artifacts:  [{"line": n, "kinds": [..], "url": str|None}]"""
    recurrence = {}
    artifacts = []
    for lineno, raw in numbered_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        prose_hits = match_text(_prose_text(obj), index)
        result_hits = match_text(_result_text(obj), index)
        for mid, toks in prose_hits.items():
            slot = recurrence.setdefault(
                mid, {"lines": [], "tokens": set(), "prose": False, "prose_lines": 0})
            slot["lines"].append(lineno)
            slot["tokens"].update(toks)
            slot["prose"] = True
            slot["prose_lines"] += 1
        for mid, toks in result_hits.items():
            slot = recurrence.setdefault(
                mid, {"lines": [], "tokens": set(), "prose": False, "prose_lines": 0})
            if lineno not in slot["lines"]:
                slot["lines"].append(lineno)
            slot["tokens"].update(toks)
        kinds, url = _artifacts(obj)
        if kinds:
            artifacts.append({"line": lineno, "kinds": kinds, "url": url})
    return recurrence, artifacts


def scan_file(path, index):
    with open(path, encoding="utf-8") as f:
        return scan_lines(enumerate(f, start=1), index)


def _reportable(slot, strong):
    """A thread makes the checklist if it carries signal:
      - it appears in conversation prose AND carries >= 2 distinct tokens, or
      - it matches a strong singleton (unique, long token) anywhere.
    A thread mentioned only inside read tool-result bodies (a file the assistant
    happened to open) is incidental and filtered, unless it is a strong singleton."""
    toks = slot["tokens"]
    if toks & strong:
        return True
    return slot.get("prose", False) and len(toks) >= 2


def _signal(slot):
    """About-ness proxy: how many prose lines engaged the thread, then token
    breadth. Lexical matching cannot truly tell 'about X' from 'mentioned X';
    prose-prevalence is the cheap stand-in until Phase-2 embeddings."""
    return (slot.get("prose_lines", 0), len(slot["tokens"]), len(slot["lines"]))


def render(recurrence, artifacts, titles, strong=frozenset(), cap=15):
    """titles: {mem_id: title}; strong: tokens that qualify a single-token hit
    (from strong_tokens); cap: max threads listed in full before the tail is
    collapsed. Renders the disposal checklist the dream must clear before
    finalizing - tracked threads that recurred (ranked by about-ness, capped),
    and each external artifact whose content must be read."""
    out = []
    shown = [m for m in recurrence if _reportable(recurrence[m], strong)]
    if shown:
        out.append(
            "== RECURRENCE (tracked threads that came up again "
            "- promote / recur / justify-drop) =="
        )
        shown.sort(key=lambda i: _signal(recurrence[i]), reverse=True)
        for mid in shown[:cap]:
            slot = recurrence[mid]
            lines = ", ".join(f"L{n}" for n in slot["lines"][:12])
            toks = "/".join(sorted(slot["tokens"])[:6])
            out.append(f"[{mid}] {titles.get(mid, '?')}")
            out.append(f"      {lines}  (matched: {toks})")
        tail = shown[cap:]
        if tail:
            ids = " ".join(f"[{m}]" for m in tail)
            out.append(f"… also touched, lower signal (scan before dropping): {ids}")
    if artifacts:
        out.append("")
        out.append(
            "== EXTERNAL ARTIFACTS (read the CONTENT before classifying "
            "- never judge by the fetch) =="
        )
        for a in artifacts:
            tail = f"  {a['url']}" if a["url"] else ""
            out.append(f"L{a['line']}\t{'/'.join(a['kinds'])}{tail}")
    return "\n".join(out)
