"""Cheap, deterministic interest pre-pass over a session .jsonl.

Flags the USER's turns that trip low-cost interest signals, so the dream's
judgment is prompted instead of relying on the model to spontaneously notice a
charged moment. It never decides what is an interest; it only surfaces
candidates for judgment. Over-surfacing is fine (the dream filters); silently
missing a concern is the failure this guards against.

Three signals, user turns only, no ML:
  1. tangent   - the user opening a new thread unprompted.
  2. affect    - a reaction/emotion word.
  3. curiosity - asking to understand an external SUBJECT, suppressed when the
                 turn is frustration at the assistant's own output instead.
"""
import json
import re

# 1) Self-initiated tangent: a thread the user raises unprompted.
_TANGENT = re.compile(
    r"\b(another (?:unrelated )?(?:question|thing)|side[- ]?(?:note|track|question)|"
    r"unrelated|by the way|off[- ]topic|random (?:thought|question)|tangent|i wonder)\b",
    re.I,
)

# 2) Affect / reaction. Deliberately omits "frustrated" and kin: frustration is
# the anti-signal, handled by the curiosity suppressor below.
_AFFECT = re.compile(
    r"\b(shock\w+|fascinat\w+|alarm\w+|worri\w+|concern\w+|disturb\w+|excit\w+|"
    r"curious|amaz\w+|incredible|outrage\w+|scary|frightening|wow|surpris\w+)\b",
    re.I,
)

# 3a) Topical curiosity: asking to understand an external subject.
_CURIOSITY = re.compile(
    r"\b(explain|what (?:is|are)|how (?:does|do|come)|why (?:does|do|is|are))\b",
    re.I,
)

# 3b) Suppressor: the "confusion" is aimed at MY output, not a subject. Scoped to
# curiosity ONLY on purpose: "confusing"/"makes no sense" can describe a topic
# (affect about the topic itself), so this must not cancel the affect signal,
# only curiosity.
_FRUSTRATION = re.compile(
    r"\b(you|your|you're|you said|you mean|what you|rephrase|makes? no sense|"
    r"does(?:n'?t| not) make sense|confusing|i don'?t follow|lost me|"
    r"plain english|too (?:complex|dense)|convoluted|way out there|word salad)\b",
    re.I,
)

# Harness-injected user-role content that is not the user talking: command
# wrappers, system reminders, and skill-load payloads (the Skill tool injects
# the skill body as a user turn beginning "Base directory for this skill:").
_WRAPPER_PREFIXES = (
    "<local-command", "<command-", "<system-reminder", "caveat:",
    "base directory for this skill",
)


def _user_text(obj):
    """The user's typed text for a turn, or '' for tool-result/wrapper turns."""
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


def signals(text):
    hits = []
    if _TANGENT.search(text):
        hits.append("tangent")
    if _AFFECT.search(text):
        hits.append("affect")
    if _CURIOSITY.search(text) and not _FRUSTRATION.search(text):
        hits.append("curiosity")
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


def render(candidates, maxlen=120):
    rows = []
    for c in candidates:
        snippet = re.sub(r"\s+", " ", c["text"])[:maxlen]
        rows.append(f"[{'/'.join(c['signals'])}]\tL{c['line']}\t\"{snippet}\"")
    return "\n".join(rows)
