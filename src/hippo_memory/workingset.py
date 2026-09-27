"""Adaptive working-set selection for the SessionStart hook.

Picks a small, bounded, salience-ranked carryover set from the store and renders
it as the additionalContext block. Tiers per the Recall and delivery spec:
open loops (execution threads), standing lessons (charged corrections, reserved
seats), salience-ranked recent (interests + lower loops), and a one-line pointer
tail. Adaptive only downward: it surfaces min(available, cap), so a quiet day
injects less; the caps are the hard ceiling.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from hippo_memory import config, handoff, store

_LOOP_KINDS = ("episodic", "project")
_DATE_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\b")


def _tie_aware_cut(rows, cap):
    """Take up to ``cap`` rows, but never split an exact salience tie at the
    boundary: drop the whole tied group that straddles the cap so no thread is
    kept or dropped by rowid luck. ``rows`` is salience-descending. Falls back to
    a hard ``cap`` slice when the entire candidate set shares one salience (no gap
    to stop at), so a fully-tied tier is never emptied."""
    if len(rows) <= cap:
        return rows
    boundary = rows[cap]["salience"]            # first row that would be excluded
    kept = [r for r in rows if r["salience"] > boundary]
    return kept if kept else rows[:cap]


def select(conn, session_id=""):
    """Return the tiers as a dict: loops, lessons, recent, handoffs,
    older (int).

    Lessons get reserved seats: they are selected only against each other and
    never enter the recent pool, so an interest burst (a motorcycle daydream)
    can never displace a behavioral scar, and vice versa."""
    open_rows = store.ranked(conn, status="open")  # salience DESC, last_touched DESC

    lesson_candidates = [r for r in open_rows if r["kind"] == "lesson"]
    lesson_rows = _tie_aware_cut(lesson_candidates, config.WS_LESSON_CAP)
    lesson_tier = [dict(r) for r in lesson_rows]
    lesson_ids = {r["id"] for r in lesson_tier}

    loop_candidates = [r for r in open_rows if r["kind"] in _LOOP_KINDS]
    loops = _tie_aware_cut(loop_candidates, config.WS_OPEN_LOOP_CAP)
    loop_ids = {r["id"] for r in loops}

    # Handoff notes are excluded from every ranked tier; the fourth section
    # surfaces them by rule in their own section.
    recent_candidates = [
        r for r in open_rows
        if r["kind"] not in ("lesson", "handoff") and r["id"] not in loop_ids
    ]
    recent = _tie_aware_cut(recent_candidates, config.WS_RECENT_CAP)
    surfaced = lesson_ids | loop_ids | {r["id"] for r in recent}

    # Fourth section: notes to self, chosen by rule in handoff.select (carried
    # loop still open, or the single newest uncarried), never by score. Carried
    # ids ride along so render() needs no connection.
    handoffs = [
        dict(r, carries=handoff.carries(conn, r["id"]))
        for r in handoff.select(conn, cap=config.WS_HANDOFF_CAP)
    ]

    # Handoff rows are never "older threads": they are not threads.
    older = len([r for r in open_rows if r["kind"] != "handoff"]) - len(surfaced)

    return {"loops": loops, "lessons": lesson_tier, "recent": recent,
            "handoffs": handoffs, "older": older}


def last_dream_date(journal_path=None):
    """Return the newest 'YYYY-MM-DD' heading in the Dream Journal, or None."""
    p = Path(journal_path) if journal_path else Path(config.JOURNAL_PATH)
    if not p.exists():
        return None
    for line in p.read_text().splitlines():
        m = _DATE_RE.match(line.strip())
        if m:
            return m.group(1)   # journal is newest-first, so first match is latest
    return None


def stale_days(now=None, journal_path=None):
    """Whole calendar days since the last dream, or None when the journal is missing.

    The journal headings are local calendar dates (the dream runs at 02:30 local), so
    staleness is a local-date difference, not elapsed wall-clock time. Defaulting to a
    UTC now and taking ``.days`` of the elapsed delta previously skewed the count by up
    to a day near a UTC boundary (e.g. a same-day dream reading as "1 day ago" in the
    evening on the US west coast)."""
    d = last_dream_date(journal_path)
    if d is None:
        return None
    now = now or datetime.now()
    last = datetime.strptime(d, "%Y-%m-%d").date()
    return (now.date() - last).days


def dream_failure(failure_path=None):
    """Return the {date, error} of the last failed dream, or None if none pending.

    Written by `hippo dream` when the headless invocation fails (e.g. a
    2:30 OAuth-token expiry) and cleared on the next success, so its presence
    means the most recent attempt died and the carryover is stale."""
    p = Path(failure_path) if failure_path else Path(config.DREAM_FAILURE_PATH)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (ValueError, OSError):
        return None


def render_motd(failure=None, last_at=None, journal_date=None, now=None):
    """One user-facing line for the SessionStart greeting (a hook systemMessage):
    when the last dream ran, to the minute. A pending failure takes precedence;
    without a recorded time it falls back to the Journal date, and to nothing if no
    dream has ever run. Pure formatter so it tests without a store, file, or hook."""
    if failure:
        return f"Last dream FAILED ({failure.get('date', '?')}) - re-run: hippo dream"
    now = now or datetime.now()
    if last_at:
        try:
            dt = datetime.fromisoformat(last_at)
        except ValueError:
            dt = None
        if dt:
            hm = dt.strftime("%H:%M")
            days = (now.date() - dt.date()).days
            if days <= 0:
                when = f"today at {hm}"
            elif days == 1:
                when = f"yesterday at {hm}"
            else:
                when = f"on {dt.strftime('%b')} {dt.day} at {hm}"
            return f"Last dream happened {when}"
    if journal_date:
        return f"Last dream: {journal_date}"
    return ""


def motd(conn):
    """The one-line SessionStart greeting, store-aware. Fetches the state
    render_motd needs (when the last dream completed) and renders it. Shared
    by cli.cmd_motd and the session-start hook so the two surfaces can never
    drift apart on wording."""
    last_at = store.get_state(conn, "last_dream_at")
    return render_motd(failure=dream_failure(), last_at=last_at,
                       journal_date=last_dream_date())


def render(selection, stale=None, failure=None, last_at=None, now=None):
    """Render the working-set as the additionalContext block. Empty selection
    renders the empty string, which the hook treats as 'inject nothing'."""
    if (not selection["loops"] and not selection["recent"]
            and not selection.get("lessons") and not selection.get("handoffs")):
        return ""

    lines = ["# Hippocampus working-set (episodic carryover from prior sessions)"]

    dt = None
    if last_at:
        try:
            dt = datetime.fromisoformat(last_at)
        except ValueError:
            dt = None

    if failure:
        # A failed attempt outranks the staleness note: say so plainly instead of
        # presenting stale carryover as if a dream simply had not run recently.
        date = failure.get("date", "?")
        reason = (failure.get("error") or "").splitlines()[0][:120] or "unknown error"
        lines.append(
            f"_Last dream FAILED ({date}): {reason}. Carryover is stale; "
            f"re-run `hippo dream`._"
        )
    elif dt is not None:
        # Prefer the recorded completion time: an absolute date+timestamp, not a
        # day-granular "today"/"N days ago" that only bounds the dream to a 24h
        # window. Staleness becomes a parenthetical, not the headline.
        now = now or datetime.now()
        stamp = dt.strftime("%Y-%m-%d %H:%M")
        days = (now.date() - dt.date()).days
        if days >= config.WS_STALE_DAYS:
            lines.append(
                f"_Last dream: {stamp} ({days} days ago; carryover may be stale)._"
            )
        else:
            lines.append(f"_Last dream: {stamp}._")
    elif stale is not None:
        # Fallback when no completion time was recorded: day granularity only.
        if stale <= 0:
            lines.append("_Last dream: today._")
        elif stale >= config.WS_STALE_DAYS:
            lines.append(f"_Last dream: {stale} days ago; carryover may be stale._")
        else:
            unit = "day" if stale == 1 else "days"
            lines.append(f"_Last dream: {stale} {unit} ago._")

    # Each line leads with its memory id so a carried summary is addressable:
    # `hippo show <id>` expands it in one call. Without the id a title is
    # only a label, and expanding it degenerates into a search-and-grep hunt.
    if selection["loops"]:
        lines.append("")
        lines.append("## Open loops")
        for r in selection["loops"]:
            lines.append(f"- [{r['id']}] {r['salience']:.2f} {r['title']}")

    if selection.get("lessons"):
        lines.append("")
        lines.append("## Standing lessons")
        for r in selection["lessons"]:
            lines.append(f"- [{r['id']}] {r['salience']:.2f} {r['title']}")

    if selection.get("handoffs"):
        lines.append("")
        lines.append(handoff.SECTION_HEADER)
        for r in selection["handoffs"]:
            lines.append(handoff.format_line(r, r.get("carries", ())))

    if selection["recent"]:
        lines.append("")
        lines.append("## Recent context")
        for r in selection["recent"]:
            lines.append(
                f"- [{r['id']}] {r['salience']:.2f} {r['kind']}: {r['title']}"
            )

    lines.append("")
    lines.append(
        "_Expand any line above with `hippo show <id>` (full body, one call)._"
    )

    if selection["older"] > 0:
        lines.append(
            f"_The store also holds {selection['older']} older open thread(s); "
            f"query `hippo list` or `hippo search` to pull them._"
        )

    return "\n".join(lines) + "\n"
