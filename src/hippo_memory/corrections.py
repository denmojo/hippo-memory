"""Deterministic correction pre-pass over a session .jsonl.

Closes the gap that bit us on 2026-06-13: a session whose TYPE marks it routine
(a "run daily note" run, or a dream-session) gets dropped or sealed wholesale,
and a correction the user delivered *inside* it rides along invisibly. That
day a daily-note run carried a sharp correction (a public short-link pointer
was reported inverted) and the dream nearly cleared it with the run.

interest.py scans USER turns for engagement but deliberately SUPPRESSES
frustration-at-output (its anti-signal). That suppressed signal is exactly what
this scanner SURFACES: the moments the user pushed back on, corrected, or
redirected the assistant. It is the inverse of interest.py, by design.

It reads USER turns only and flags four signals so the dream cannot drop or seal
a session before accounting for them:

  1. dissatisfaction - the user says the assistant got it wrong / off-track.
  2. interrupt       - a [Request interrupted by user] marker (a hard redirect).
  3. directive       - a redirect or new standing rule issued after seeing output.
  4. charged         - profanity or a shouted (consecutive ALL-CAPS) run.

It decides nothing and over-surfaces by design, like interest.py and coverage.py.
A routine command ("run daily note", "go", "implement") stays silent; a pushback
does not. This is the maintenance-vs-correction line: normally drop the routine
session, but TAKE NOTE the moment the user responds rather than just commands.
"""
import json
import re
import string

# 1) Dissatisfaction: a correction aimed at the assistant's action or output.
# Bare "you"/"your" are deliberately excluded - nearly every request addresses
# the assistant, and matching them would flag everything (== flagging nothing).
# Anchored instead on correction-bearing phrasing. The frustration-at-output
# phrases interest.py lists as its SUPPRESSOR are reused here as primary signal.
_DISSATISFACTION = re.compile(
    r"\b("
    r"you (?:said|mean|forgot|ignored|missed|skimmed|screwed|got|get|keep|always|still|misread|misunderstood|didn'?t)|"
    r"did(?:n'?t)? you (?:forget|read|even|just)|did you|didn'?t i|i did ?n'?t say|i did ?n'?t mean|i said|that'?s not what i|"
    r"what you|rephrase|makes? no sense|does(?:n'?t| not) (?:make sense|work)|"
    r"i don'?t follow|lost me|plain english|too (?:complex|dense)|convoluted|"
    r"way out there|word salad|"
    r"(?:that'?s|that is|this is|it'?s) (?:not |n'?t )?(?:wrong|right|correct|redundant)|"
    r"is wrong|not right|incorrect|backwards|screwed up|messed up|"
    r"stop (?:doing|trying|it|with|asking|coming|telling)|cease|knock it off|"
    r"you'?re (?:wrong|bloating|ignoring|forgetting)"
    r")\b",
    re.I,
)

# A turn that opens with a bare "no" is a correction of what just happened.
_LEADING_NO = re.compile(r"^\W*no[,.!\s]", re.I)

# 3) Directive: a redirect or new standing rule, typically issued after seeing
# output. "instead"/"actually" over-surface a little; accepted (checklist only).
_DIRECTIVE = re.compile(
    r"\b(actually|instead|rather than|i'?d rather|from now on|going forward|"
    r"next time|let'?s not|no longer|don'?t (?:do|answer|edit|add|write|jump)|"
    r"lose the|cut the|omit the|remove the|leave .* alone)\b",
    re.I,
)

# 4a) Profanity: a charged-correction marker.
_PROFANITY = re.compile(
    r"\b(fuck\w*|dammit|damn it|god ?dammit|goddamn\w*|shit|bullshit|wtf|geez|jesus)\b",
    re.I,
)

# 2) Harness interrupt marker: the user stopped the assistant mid-action.
_INTERRUPT = re.compile(r"\[request interrupted by user", re.I)

# Harness-injected user-role content that is not the user talking. Mirrors
# interest.py / coverage.py. The interrupt marker is NOT a wrapper - it is signal.
_WRAPPER_PREFIXES = (
    "<local-command", "<command-", "<system-reminder", "caveat:",
    "base directory for this skill",
)


def _user_text(obj):
    """The user's typed text for a turn, or '' for tool-result/wrapper/non-user."""
    if obj.get("type") != "user":
        return ""
    content = obj.get("message", {}).get("content", "")
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
    else:
        return ""
    kept = [
        p for p in parts
        if p.strip() and not p.lstrip().lower().startswith(_WRAPPER_PREFIXES)
    ]
    return " ".join(kept).strip()


def _is_shout(text):
    """True if the text contains a run of >= 2 consecutive ALL-CAPS words
    (>= 2 letters each). A single acronym (ADR, URL, NOW) is not a shout."""
    run = 0
    for tok in text.split():
        clean = tok.strip(string.punctuation)
        if len(clean) >= 2 and clean.isalpha() and clean.isupper():
            run += 1
            if run >= 2:
                return True
        else:
            run = 0
    return False


def signals(text):
    hits = []
    if _DISSATISFACTION.search(text) or _LEADING_NO.search(text):
        hits.append("dissatisfaction")
    if _INTERRUPT.search(text):
        hits.append("interrupt")
    if _DIRECTIVE.search(text):
        hits.append("directive")
    if _PROFANITY.search(text) or _is_shout(text):
        hits.append("charged")
    return hits


def scan_lines(numbered_lines):
    """numbered_lines: iterable of (lineno, raw_json_line). Returns candidates."""
    out = []
    for lineno, raw in numbered_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        text = _user_text(obj)
        if not text:
            continue
        hits = signals(text)
        if hits:
            out.append({"line": lineno, "signals": hits, "text": text})
    return out


def scan_file(path):
    with open(path, encoding="utf-8") as f:
        return scan_lines(enumerate(f, start=1))


def render(candidates, maxlen=140):
    if not candidates:
        return ""
    rows = [
        "== CORRECTIONS (user pushed back / redirected - capture the lesson "
        "before dropping or sealing this session) =="
    ]
    for c in candidates:
        snippet = re.sub(r"\s+", " ", c["text"])[:maxlen]
        rows.append(f"[{'/'.join(c['signals'])}]\tL{c['line']}\t\"{snippet}\"")
    return "\n".join(rows)
