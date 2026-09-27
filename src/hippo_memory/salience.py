"""Pure salience scoring and recency decay. No database access."""
import math
from datetime import datetime


def _parse(ts):
    return datetime.fromisoformat(ts)


def recency_factor(last_touched_at, now, halflife_days):
    age_days = (now - _parse(last_touched_at)).total_seconds() / 86400.0
    if age_days < 0:
        age_days = 0.0
    return 0.5 ** (age_days / halflife_days)


def score(mem, now, weights, halflife_days,
          interest_baseline=0.0, interest_halflife_days=None,
          lesson_baseline=0.0, lesson_halflife_days=None):
    # Handoff notes are the agent's notes to self, surfaced by rule
    # in their own working-set section. They never compete in the ranking, so
    # they score a constant zero regardless of pin, weight, recency, or status.
    if mem["kind"] == "handoff":
        return 0.0
    pinned = 1.0 if mem["pinned"] else 0.0
    # Lesson memories (charged corrections) are behavioral scars, not open loops:
    # a baseline above the interest baseline plus a weeks-long half-life, so the
    # night's sharpest pushback cannot be outranked by a same-day interest and
    # does not fade on the 7-day operational curve. Weight carries the charge
    # stamped at promotion; no open-loop or recurrence terms (a scar is not
    # execution carryover, and re-offense re-arms recency via last_touched_at).
    if mem["kind"] == "lesson":
        hl = lesson_halflife_days if lesson_halflife_days is not None else halflife_days
        rec = recency_factor(mem["last_touched_at"], now, hl)
        return (
            lesson_baseline
            + weights["recency"] * rec
            + weights["weight"] * float(mem["weight"])
            + weights["pin"] * pinned
        )
    # Interest/concern memories serve the user-as-reader, not work continuity, so
    # they score on a separate axis: a baseline plus slow-decaying recency, never
    # gated on open-loop status or recurrence (which reward execution, not interest).
    if mem["kind"] == "interest":
        hl = interest_halflife_days if interest_halflife_days is not None else halflife_days
        rec = recency_factor(mem["last_touched_at"], now, hl)
        return (
            interest_baseline
            + weights["recency"] * rec
            + weights["weight"] * float(mem["weight"])
            + weights["pin"] * pinned
        )
    rec = recency_factor(mem["last_touched_at"], now, halflife_days)
    open_loop = 1.0 if mem["status"] == "open" else 0.0
    return (
        weights["open_loop"] * open_loop
        + weights["recency"] * rec
        + weights["recurrence"] * math.log1p(mem["recurrence"])
        + weights["weight"] * float(mem["weight"])
        + weights["pin"] * pinned
    )
