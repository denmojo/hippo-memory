"""Single rolling Dream Journal: render, upsert newest-first, archive old.

One heading per calendar day. Multiple dream runs on the same day append as
``_run HH:MM_`` blocks under that day's heading rather than spawning a second
identical date heading.
"""
import re
from pathlib import Path

TITLE = "# Memory Dream Journal\n"
_ENTRY_SPLIT = re.compile(r"(?m)^(?=## )")


def render_run(run_label, lines):
    body = "\n".join(f"- {line}" for line in lines)
    return f"_run {run_label}_\n{body}\n"


def render_entry(date_str, run_label, lines):
    return f"## {date_str}\n\n{render_run(run_label, lines)}"


def _read(path):
    path = Path(path)
    if path.exists():
        return path.read_text()
    return TITLE + "\n"


def _split_entries(text):
    if text.startswith(TITLE):
        rest = text[len(TITLE):]
    else:
        rest = text
    parts = _ENTRY_SPLIT.split(rest)
    return [p for p in parts if p.strip()]


def _heading_line(entry):
    return entry.lstrip().splitlines()[0].strip() if entry.strip() else ""


def _write(path, entries):
    body = "".join(e.rstrip("\n") + "\n\n" for e in entries)
    path.write_text(TITLE + "\n" + body.rstrip("\n") + "\n")


def add_entry(path, date_str, run_label, lines):
    """Upsert a run into the journal.

    If the newest entry is already today's date heading, append this run as a
    ``_run HH:MM_`` block beneath it (chronological within the day). Otherwise
    prepend a fresh date heading. Days stay newest-first; runs within a day stay
    oldest-first.
    """
    path = Path(path)
    entries = _split_entries(_read(path))
    run_block = render_run(run_label, lines)
    if entries and _heading_line(entries[0]) == f"## {date_str}":
        entries[0] = entries[0].rstrip("\n") + "\n\n" + run_block
    else:
        entries.insert(0, render_entry(date_str, run_label, lines))
    path.parent.mkdir(parents=True, exist_ok=True)
    _write(path, entries)


def archive_old(path, keep_n, archive_path):
    path = Path(path)
    entries = _split_entries(_read(path))
    if len(entries) <= keep_n:
        return
    keep, overflow = entries[:keep_n], entries[keep_n:]
    path.write_text(TITLE + "\n" + "".join(keep).rstrip("\n") + "\n")
    archive_path = Path(archive_path)
    prior = archive_path.read_text() if archive_path.exists() else "# Memory Dream Journal Archive\n"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(prior.rstrip("\n") + "\n\n" + "".join(overflow).rstrip("\n") + "\n")
