"""Deterministic CLI for the manual dream. Judgment stays with the caller."""
import argparse
import glob
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from hippo_memory import (
    __version__, config, corrections, coverage, digest, dream, embed,
    handoff, hooks, indexer, install, interest, journal, lessons,
    recall_check, search, store, workingset,
)


def ensure_store():
    """Create the store (run schema init/migrations) if needed. Returns the
    resolved db path. No-arg entry point used directly by hooks and the
    installer, which only need the store to exist."""
    conn = store.connect()
    store.init_schema(conn)
    conn.close()
    return str(os.environ.get("HIPPO_DB") or config.DB_PATH)


def _connect():
    ensure_store()
    return store.connect()


def cmd_add(args):
    conn = _connect()
    mid = store.upsert_memory(
        conn, args.kind, args.title, body=args.body or "",
        dedup_key=args.dedup_key, weight=args.weight, pinned=args.pin,
    )
    conn.close()
    print(mid)
    return 0


def cmd_link(args):
    conn = _connect()
    store.add_link(conn, args.from_id, args.to_id, args.kind)
    conn.close()
    return 0


# --- handoff: notes to self written by the in-session agent ----------------

def cmd_handoff_add(args):
    conn = _connect()
    try:
        session = handoff.resolve_session(args.session)
        mid = handoff.add(conn, args.title, args.body, session, carries=args.carries)
    except handoff.HandoffError as e:
        conn.close()
        print(f"refused: {e}")
        return 1
    # Print the projected fourth section so the write is auditable: what the
    # next SessionStart will carry, with this note in it.
    block = handoff.render_section(conn, handoff.select(conn))
    conn.close()
    print(f"{mid}  handoff:{session}")
    print(block, end="")
    return 0


def cmd_handoff_list(args):
    conn = _connect()
    rows = handoff.list_rows(conn, session=args.session, include_closed=args.all)
    for r in rows:
        ids = handoff.carries(conn, r["id"])
        tail = f"  carries {','.join(map(str, ids))}" if ids else ""
        print(f"[{r['id']}] {r['status']:<6} {r['dedup_key']}  {r['title']}{tail}")
    conn.close()
    return 0


def cmd_handoff_close(args):
    conn = _connect()
    try:
        handoff.close(conn, args.id, note=args.note)
    except handoff.HandoffError as e:
        conn.close()
        print(f"refused: {e}")
        return 1
    conn.close()
    return 0


def cmd_handoff_housekeep(args):
    # The manual dream's counterpart to the housekeeping run_dream does after
    # apply: close notes whose loops all closed, fold superseded, retire
    # uncarried notes past one cycle. Prints what it did.
    conn = _connect()
    hk = handoff.housekeep(conn)
    conn.close()
    for hid in hk["closed_done"]:
        print(f"closed [{hid}]: every carried loop closed")
    for old, new in hk["superseded"]:
        print(f"folded [{old}] into [{new}]: superseded")
    for hid in hk["closed_uncarried"]:
        print(f"closed [{hid}]: rode one cycle uncarried")
    if not any(hk.values()):
        print("nothing to do")
    return 0


def cmd_handoff_latest(args):
    conn = _connect()
    row = handoff.latest(conn, session=args.session)
    if row is None:
        conn.close()
        scope = f" for session {args.session}" if args.session else ""
        print(f"no open handoff{scope}")
        return 1
    ids = handoff.carries(conn, row["id"])
    print(f"[{row['id']}] {row['dedup_key']}  {row['title']}")
    if ids:
        print(f"carries: {', '.join(map(str, ids))}")
    print(row["body"])
    conn.close()
    return 0


def cmd_close(args):
    conn = _connect()
    store.close_memory(conn, args.id, note=args.note)
    conn.close()
    return 0


def cmd_fold(args):
    # Retire a duplicate a durable rule already covers. Unlike close, this drops
    # the row's vector so it stops competing in recall with the rule it restates.
    conn = _connect()
    row = store.fold_memory(conn, args.id, into=args.into, note=args.note)
    conn.close()
    print(f"folded [{row['id']}] {row['title']}\n  into: {row['folded_into']}\n"
          f"  body kept as evidence; vector dropped, so it no longer competes in recall")
    return 0


def cmd_rearm(args):
    # Manual-dream counterpart of the headless re-offense path: one command
    # bumps weight (capped), recurrence, and recency, and reopens a retired
    # lesson. Do NOT also re-add with the same dedup_key; that would
    # double-count the offense.
    conn = _connect()
    row = store.get_memory(conn, args.id)
    if row is None or row["kind"] != "lesson":
        conn.close()
        print(f"no lesson with id {args.id}")
        return 1
    w = lessons.rearm(conn, args.id)
    store.rescore_all(conn)
    row = store.get_memory(conn, args.id)
    conn.close()
    print(f"[{args.id}] re-offense recorded: weight={w:.1f} "
          f"recurrence={row['recurrence']} salience={row['salience']:.2f} "
          f"{row['title']}")
    return 0


def cmd_boost(args):
    # Attention flag ("remember this please"): raise a memory's weight and re-arm
    # recency so it surfaces in future working-sets, lighter than a pin. Rescores
    # so the printed salience is the post-boost value.
    conn = _connect()
    store.set_weight(conn, args.id, args.weight)
    store.rescore_all(conn)
    row = store.get_memory(conn, args.id)
    conn.close()
    if row is None:
        print(f"no memory with id {args.id}")
        return 1
    print(f"[{row['id']}] weight={float(row['weight']):.1f} "
          f"salience={row['salience']:.2f} {row['title']}")
    return 0


def cmd_rescore(args):
    conn = _connect()
    store.rescore_all(conn)
    conn.close()
    return 0


def cmd_list(args):
    # The spec spells the episodic kind 'episode'; the schema CHECK spells
    # it 'episodic'. Accept both, store speaks 'episodic'.
    kind = {"episode": "episodic"}.get(args.kind, args.kind)
    limit = None if args.limit == 0 else args.limit
    conn = _connect()
    # Fetch unbounded and slice here: the store is small, and the remainder
    # count for the more-rows note needs the full match anyway.
    rows = store.ranked(
        conn, status=args.status, kind=kind, since=args.since,
        touched_since=args.touched_since, min_salience=args.min_salience,
        order=args.sort,
    )
    conn.close()
    shown = rows if limit is None else rows[:limit]
    for r in shown:
        if args.dates:
            print(f"[{r['id']}] {r['salience']:.2f} {r['status']:6} "
                  f"{r['created_at'][:10]} {r['last_touched_at'][:10]} {r['title']}")
        else:
            print(f"[{r['id']}] {r['salience']:.2f} {r['status']:6} {r['title']}")
    hidden = len(rows) - len(shown)
    if hidden > 0:
        print(f"... {hidden} more row(s); use --limit 0 for all, "
              f"or narrow with --kind/--status/--since/--min-salience")
    return 0


def cmd_watermark(args):
    conn = _connect()
    if args.action == "set":
        store.set_state(conn, "watermark", args.value)
    else:
        print(store.get_state(conn, "watermark", "") or "")
    conn.close()
    return 0


def cmd_journal(args):
    journal.archive_old(
        config.JOURNAL_PATH, config.JOURNAL_ACTIVE_ENTRIES, config.JOURNAL_ARCHIVE_PATH
    )
    run_label = args.time or datetime.now().strftime("%H:%M")
    journal.add_entry(config.JOURNAL_PATH, args.date, run_label, args.line)
    print(f"journal updated: {config.JOURNAL_PATH}")
    return 0


def cmd_interest_scan(args):
    out = interest.render(interest.scan_file(args.path))
    if out:
        print(out)
    return 0


def cmd_corrections_scan(args):
    # User-correction pre-pass: surfaces the turns where the user pushed back,
    # redirected, or was charged, so a routine-looking session (a daily-note run,
    # a dream-session) cannot be dropped or sealed before its corrections are
    # accounted for. Inverse of interest.py (which suppresses frustration).
    out = corrections.render(corrections.scan_file(args.path))
    if out:
        print(out)
    return 0


def cmd_coverage_scan(args):
    # Recurrence + external-artifact pre-pass: surfaces tracked threads that
    # recurred and external content that must be read before being classified,
    # so neither can be silently dropped. Matches against OPEN memories.
    conn = _connect()
    rows = conn.execute(
        "SELECT id, title, body FROM memories WHERE status = 'open'"
    ).fetchall()
    conn.close()
    index = coverage.build_index(rows)
    strong = coverage.strong_tokens(rows)
    titles = {r["id"]: r["title"] for r in rows}
    rec, arts = coverage.scan_file(args.path, index)
    out = coverage.render(rec, arts, titles, strong)
    if out:
        print(out)
    return 0


def cmd_offset(args):
    # Per-session ingestion cursor: how many lines of a session .jsonl have been
    # consolidated. Open sessions are included; each dream reads forward from here.
    conn = _connect()
    key = f"session:{args.session}"
    if args.action == "set":
        store.set_state(conn, key, str(args.value))
    else:
        print(store.get_state(conn, key, "0") or "0")
    conn.close()
    return 0


def _write_snapshot(conn, dst):
    """Consistent online copy of `conn`'s store at `dst`, as ONE self-contained file.

    The backup API carries the source's journal mode into the copy, so a snapshot
    of the WAL-mode live store is itself WAL-mode. Nothing is wrong with the copy,
    but every later reader of it recreates `<name>-wal` and `<name>-shm` alongside.
    Converting the snapshot to a rollback journal checkpoints the WAL into the
    file and removes the sidecars, so reads of the backup stay side-effect free."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(str(dst))
    with out:
        conn.backup(out)
    out.execute("PRAGMA journal_mode=DELETE")
    out.close()
    for suffix in ("-wal", "-shm"):
        dst.with_name(dst.name + suffix).unlink(missing_ok=True)


def cmd_backup(args):
    # WAL-safe online backup: a consistent snapshot even while the live DB is in WAL.
    dst = Path(os.environ.get("HIPPO_BACKUP") or config.HIPPO_BACKUP_PATH)
    src = _connect()
    _write_snapshot(src, dst)
    src.close()
    print(f"backup written: {dst}")
    return 0


def cmd_index(args):
    # Embed stock auto-memory files and hippo rows for semantic recall. Builds
    # the embedder lazily (heavy ONNX import only happens here).
    conn = _connect()
    try:
        embedder = embed.get_embedder(config.EMBED_MODEL)
    except embed.EmbedderUnavailable as e:
        conn.close()
        print(f"hippo-memory[index]: embedding model unavailable: {e}", file=sys.stderr)
        return 2
    both = not (args.stock or args.hippo)
    n_stock = n_hippo = 0
    if args.stock or both:
        n_stock = indexer.index_stock(
            conn, config.MEMORY_DIR, embedder, config.EMBED_MODEL
        )
    if args.hippo or both:
        n_hippo = indexer.index_hippo(conn, embedder, config.EMBED_MODEL)
    conn.close()
    print(f"indexed: stock={n_stock} hippo={n_hippo}")
    return 0


def cmd_search(args):
    conn = _connect()
    try:
        embedder = embed.get_embedder(config.EMBED_MODEL)
    except embed.EmbedderUnavailable as e:
        hits = search.lexical(conn, args.query, limit=args.k)
        conn.close()
        print(f"(lexical fallback only, no embedding model: {e})")
        for h in hits:
            print(f"lex   {h['salience']:.2f} {h['title']}")
        return 0
    hits = search.run(
        conn, args.query, embedder, config.EMBED_MODEL,
        k=args.k, source=args.source, status=args.status,
    )
    conn.close()
    for h in hits:
        print(
            f"{h['source']:5} {h['blended']:.3f} rel={h['relevance']:.3f} "
            f"sal={h['salience']:.2f} {h['title']}"
        )
    return 0


def _render_record(conn, row, relevance=None):
    head = (f"[{row['id']}] {row['kind']}  salience {row['salience']:.2f}  "
            f"{row['status']}")
    if relevance is not None:
        head += f"  rel={relevance:.3f}"
    meta = (f"created {row['created_at'][:16]}  touched {row['last_touched_at'][:16]}"
            f"  recurrence {row['recurrence']}  links {store.count_links(conn, row['id'])}")
    body = row["body"] or "(no body)"
    return f"{head}\n{row['title']}\n{meta}\n\n{body}"


def cmd_show(args):
    conn = _connect()
    row = store.get_memory(conn, args.id)
    if row is None:
        conn.close()
        print(f"no memory with id {args.id}")
        return 1
    block = _render_record(conn, row)
    conn.close()
    print(block)
    return 0


def cmd_recall(args):
    # Semantic front door for "detail on X": find the best-matching hippo memory
    # and print its full body, the thing search/list never surface. hippo-only,
    # since stock rows are files with no stored body to dump.
    conn = _connect()
    try:
        embedder = embed.get_embedder()
    except embed.EmbedderUnavailable as e:
        conn.close()
        print(f"hippo-memory[recall]: embedding model unavailable: {e}", file=sys.stderr)
        return 2
    hits = search.run(
        conn, args.query, embedder, config.EMBED_MODEL,
        k=args.k, source="hippo", status=args.status,
    )
    if not hits:
        conn.close()
        print(f"no match for {args.query!r}")
        return 1
    blocks = []
    for h in hits:
        row = store.get_memory(conn, int(h["ref"]))
        if row is not None:
            blocks.append(_render_record(conn, row, relevance=h["relevance"]))
    conn.close()
    print("\n\n".join(blocks))
    return 0


def cmd_check(args):
    # Recall smoke test: run each probe against the live store and assert the
    # expected memory is rank 1. Read-only unless --reindex. Probe path is
    # resolved env-first (like HIPPO_DB) so it is overridable at call time.
    probes_path = os.environ.get("HIPPO_RECALL_PROBES") or str(config.RECALL_PROBES_PATH)
    probes = recall_check.load_probes(probes_path)
    conn = _connect()
    try:
        embedder = embed.get_embedder(config.EMBED_MODEL)
    except embed.EmbedderUnavailable as e:
        conn.close()
        print(f"hippo-memory[check]: embedding model unavailable: {e}", file=sys.stderr)
        return 2
    if args.reindex:
        indexer.index_stock(conn, config.MEMORY_DIR, embedder, config.EMBED_MODEL)
        indexer.index_hippo(conn, embedder, config.EMBED_MODEL)
    results = []
    for probe in probes:
        hits = search.run(conn, probe["query"], embedder, config.EMBED_MODEL,
                          k=25, source="all")
        results.append(recall_check.evaluate(probe, hits))
    total = len(store.ranked(conn))
    unembedded = store.unembedded_hippo(conn, config.EMBED_MODEL)
    stale = indexer.stale_hippo(conn, config.EMBED_MODEL)
    conn.close()
    print(recall_check.render(results))
    print(recall_check.coverage_line(unembedded, total, stale=stale))
    return recall_check.exit_code(results, unembedded=unembedded, stale=stale)


def _record_dream_failure(date, error):
    """Persist a dream-failure sentinel for the working-set header to surface."""
    path = Path(os.environ.get("HIPPO_DREAM_FAILURE") or config.DREAM_FAILURE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": date, "error": error[:500]}))


def _clear_dream_failure():
    """Remove the failure sentinel after a successful invocation."""
    path = Path(os.environ.get("HIPPO_DREAM_FAILURE") or config.DREAM_FAILURE_PATH)
    path.unlink(missing_ok=True)


def cmd_motd(args):
    # One user-facing greeting line for the SessionStart hook's systemMessage:
    # when the last dream ran, to the minute (model context is the working-set).
    conn = _connect()
    line = workingset.motd(conn)
    conn.close()
    if line:
        print(line)
    return 0


def cmd_working_set(args):
    conn = _connect()
    sel = workingset.select(conn, session_id=getattr(args, "session", "") or "")
    last_at = store.get_state(conn, "last_dream_at")
    conn.close()
    out = workingset.render(
        sel, stale=workingset.stale_days(), failure=workingset.dream_failure(),
        last_at=last_at,
    )
    if out:
        print(out, end="")
    return 0


def cmd_digest(args):
    argv = []
    if args.list:
        argv.append("--list")
    argv += ["--days", str(args.days)]
    if args.out:
        argv += ["--out", args.out]
    if args.target:
        argv.append(args.target)
    return digest.run(argv)


def _recent_sessions(project, days):
    """(_path, session-id) for .jsonl transcripts modified within `days`, oldest first."""
    files = glob.glob(os.path.join(str(project), "*.jsonl"))
    cutoff = datetime.now().timestamp() - days * 86400
    out = []
    for f in files:
        if os.path.getmtime(f) >= cutoff:
            sid = os.path.splitext(os.path.basename(f))[0]
            out.append((f, sid, os.path.getmtime(f)))
    out.sort(key=lambda x: x[2])
    return [(f, sid) for f, sid, _ in out]


def _slice_new(path, offset):
    """New COMPLETE lines past `offset`, and the new complete-line count.

    Only newline-terminated lines count; a trailing partial line is left for the
    next dream (matches the interactive procedure's 'leave a trailing partial')."""
    with open(path, errors="replace") as f:
        complete = [ln for ln in f if ln.endswith("\n")]
    return complete[offset:], len(complete)


def _snapshot(conn, dst):
    """WAL-safe online snapshot of the live store to `dst` (the rollback point)."""
    _write_snapshot(conn, dst)


def _gather(conn, project, days, exclude):
    """Assemble the dream material + the deterministic offset advances per session
    + the union of corrections-scan signals (feeds the lesson charge floor)."""
    open_rows = conn.execute(
        "SELECT id, title, body FROM memories WHERE status = 'open'"
    ).fetchall()
    cov_index = coverage.build_index(open_rows)
    cov_strong = coverage.strong_tokens(open_rows)
    cov_titles = {r["id"]: r["title"] for r in open_rows}

    parts, advances, signals = [], {}, set()
    for path, sid in _recent_sessions(project, days):
        if sid in exclude:
            continue
        off = int(store.get_state(conn, f"session:{sid}", "0") or "0")
        new_lines, total = _slice_new(path, off)
        if not new_lines:
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as tf:
            tf.writelines(new_lines)
            tmp = tf.name
        try:
            try:
                text = digest.digest_path(tmp)
            except Exception as e:  # best-effort: a bad transcript must not kill the dream
                text = f"(digest unavailable: {e})"
            crc = corrections.scan_file(tmp)
            for c in crc:
                signals.update(c["signals"])
            scans = [
                ("INTEREST-SCAN", interest.render(interest.scan_file(tmp))),
                ("CORRECTIONS-SCAN", corrections.render(crc)),
            ]
            rec, arts = coverage.scan_file(tmp, cov_index)
            scans.append(("COVERAGE-SCAN", coverage.render(rec, arts, cov_titles, cov_strong)))
        finally:
            os.unlink(tmp)
        block = [f"## SESSION {sid}  (lines {off} -> {total})"]
        # Notes to self first: the agent's own account of the session precedes
        # the digest the dream would otherwise reconstruct it from.
        notes = handoff.material_block(conn, sid)
        if notes:
            block.append(notes)
        block.append(text)
        block += [f"--- {label} ---\n{txt}" for label, txt in scans if txt]
        parts.append("\n\n".join(block))
        advances[sid] = total
    return ("\n\n====\n\n".join(parts), advances, signals)


def cmd_dream(args):
    # Headless dream: gather new session activity + deterministic pre-passes,
    # hand the judgment to headless Claude, validate and apply the returned
    # plan, then advance offsets and back up. --dry-run skips all writes;
    # --print-material dumps the assembled prompt material and stops.
    project = os.environ.get("HIPPO_SESSIONS_DIR") or config.SESSIONS_DIR
    conn = _connect()
    material, advances, signals = _gather(conn, project, args.days, set(args.exclude or []))
    if not material:
        print("dream: no new session activity")
        conn.close()
        return 0
    if args.print_material:
        print(material)
        conn.close()
        return 0

    date = args.date or datetime.now().strftime("%Y-%m-%d")
    if not args.dry_run:
        # Reversibility: snapshot the live store before any mutation so a bad
        # autonomous run is recoverable with `hippo rollback`.
        _snapshot(conn, os.environ.get("HIPPO_ROLLBACK") or config.HIPPO_ROLLBACK_PATH)
    try:
        summary = dream.run_dream(
            conn, material, date=date, apply=not args.dry_run,
            plan_dir=Path(config.JOURNAL_PATH).parent,
            charge=dream.charge_floor(signals),
            sessions=list(advances.keys()),
        )
    except dream.DreamPlanError as e:
        # A failed invocation (e.g. a scheduled OAuth-token expiry) must not be
        # silent: leave a sentinel the working-set header surfaces, then
        # propagate so the caller (the scheduled job) exits non-zero.
        if not args.dry_run:
            _record_dream_failure(date, str(e))
        conn.close()
        raise
    if not args.dry_run:
        # Invocation succeeded; clear any prior failure marker.
        _clear_dream_failure()

    if args.dry_run:
        conn.close()
        print(f"DRY RUN. rejected={summary['rejected']}")
        print(json.dumps(summary["plan"], indent=2))
        return 0
    if summary.get("aborted"):
        conn.close()
        print(f"dream ABORTED: {summary['destructive']} destructive ops exceed "
              f"cap {summary['cap']}; nothing applied.")
        print(f"  plan saved for review: {summary['plan_path']}")
        print(f"  (raise the cap or apply manually after review)")
        return 0
    # Offsets advance deterministically (not trusted to Claude's plan) so
    # consolidated lines are never reprocessed even if the model omits them.
    for sid, total in advances.items():
        store.set_state(conn, f"session:{sid}", str(total))
    # Record the completion time (the Journal headings carry only the date) so the
    # SessionStart greeting can report "last dream happened at HH:MM".
    store.set_state(conn, "last_dream_at", datetime.now().isoformat(timespec="seconds"))
    conn.close()
    cmd_backup(args)
    print(f"dream: added={summary.get('added', 0)} closed={summary.get('closed', 0)} "
          f"decayed={summary.get('decayed', 0)} merged={summary.get('merged', 0)} "
          f"linked={summary.get('linked', 0)} sessions={len(advances)} "
          f"journal={summary.get('journal_lines', 0)} rejected={summary['rejected']} "
          f"diverted={summary.get('diverted', 0)} rearmed={summary.get('rearmed', 0)} "
          f"retired={summary.get('retired', 0)} "
          f"annotated={summary.get('annotated', 0)} "
          f"handoffs_closed={summary.get('handoffs_closed', 0)} "
          f"handoffs_folded={summary.get('handoffs_folded', 0)}")
    for r in summary["rejections"]:
        print(f"  rejected: {r}")
    return 0


def cmd_rollback(args):
    # Restore the pre-apply snapshot taken by the last dream run, undoing an
    # autonomous run wholesale. Clears the WAL sidecars so no stale overlay remains.
    snap = Path(os.environ.get("HIPPO_ROLLBACK") or config.HIPPO_ROLLBACK_PATH)
    if not snap.exists():
        print(f"no pre-dream snapshot at {snap}")
        return 1
    dst = Path(os.environ.get("HIPPO_DB") or config.DB_PATH)
    shutil.copy(snap, dst)
    for ext in ("-wal", "-shm"):
        side = Path(str(dst) + ext)
        if side.exists():
            side.unlink()
    print(f"rolled back: {dst} restored from {snap}")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="hippo")
    p.add_argument("--version", action="version", version=f"hippo {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add")
    a.add_argument("--kind", required=True,
                   choices=["episodic", "project", "interest", "lesson"])
    a.add_argument("--title", required=True)
    a.add_argument("--body", default="")
    a.add_argument("--dedup-key", dest="dedup_key", default=None)
    a.add_argument("--weight", type=float, default=0.0)
    a.add_argument("--pin", action="store_true")
    a.set_defaults(func=cmd_add)

    lk = sub.add_parser("link")
    lk.add_argument("--from", dest="from_id", type=int, required=True)
    lk.add_argument("--to", dest="to_id", type=int, required=True)
    lk.add_argument("--kind", default="relates")
    lk.set_defaults(func=cmd_link)

    c = sub.add_parser("close")
    c.add_argument("id", type=int)
    c.add_argument("--note", default=None,
                   help="exit story appended to the body, e.g. "
                        "'GRADUATED: enforced by a guard script'")
    c.set_defaults(func=cmd_close)

    ho = sub.add_parser(
        "handoff",
        help="notes to self written by the in-session agent",
    )
    hos = ho.add_subparsers(dest="handoff_cmd", required=True)
    ha = hos.add_parser("add", help="write one note; prints the projected section")
    ha.add_argument("--title", required=True, help="one line naming the task state")
    ha.add_argument("--body", required=True,
                    help="labelled text: State, Decided, Dead ends, Assumed, "
                         "Next, Calibration, Unsure (each optional, no others); "
                         f"cap {config.HANDOFF_BODY_MAX} chars")
    ha.add_argument("--carries", default=[], metavar="IDS",
                    type=lambda s: [int(x) for x in s.split(",") if x.strip()],
                    help="comma-separated open-loop ids this note carries")
    ha.add_argument("--session", default=None,
                    help="session id; default: the current-session file the "
                         "session-track hook writes; refuses if neither is available")
    ha.set_defaults(func=cmd_handoff_add)
    hl = hos.add_parser("list")
    hl.add_argument("--session", default=None)
    hl.add_argument("--all", action="store_true", help="include closed rows")
    hl.set_defaults(func=cmd_handoff_list)
    hc = hos.add_parser("close")
    hc.add_argument("id", type=int)
    hc.add_argument("--note", default=None)
    hc.set_defaults(func=cmd_handoff_close)
    hh = hos.add_parser("housekeep",
                        help="close/fold/retire notes deterministically (manual dream step)")
    hh.set_defaults(func=cmd_handoff_housekeep)
    hla = hos.add_parser("latest", help="newest open note (full body); exit 1 if none")
    hla.add_argument("--session", default=None)
    hla.set_defaults(func=cmd_handoff_latest)

    f = sub.add_parser("fold")
    f.add_argument("id", type=int)
    f.add_argument("--into", required=True,
                   help="what absorbs it, e.g. 'feedback_example.md'")
    f.add_argument("--note", default=None,
                   help="why it was folded, appended to the body")
    f.set_defaults(func=cmd_fold)

    ra = sub.add_parser("rearm")
    ra.add_argument("id", type=int)
    ra.set_defaults(func=cmd_rearm)

    bo = sub.add_parser("boost")
    bo.add_argument("id", type=int)
    bo.add_argument("weight", type=float, nargs="?", default=1.0)
    bo.set_defaults(func=cmd_boost)

    sub.add_parser("rescore").set_defaults(func=cmd_rescore)

    ls = sub.add_parser("list")
    ls.add_argument("--status", default=None, choices=["open", "closed"])
    ls.add_argument("--limit", type=int, default=30,
                    help="max rows (default 30; 0 = all)")
    ls.add_argument("--kind", default=None,
                    choices=["episodic", "episode", "project", "interest", "lesson",
                             "handoff"])
    ls.add_argument("--since", default=None, metavar="DATE",
                    help="only rows created on/after this ISO date")
    ls.add_argument("--touched-since", default=None, metavar="DATE",
                    help="only rows last touched on/after this ISO date")
    ls.add_argument("--min-salience", type=float, default=None, metavar="X")
    ls.add_argument("--sort", default="salience",
                    choices=["salience", "recency", "id"])
    ls.add_argument("--dates", action="store_true",
                    help="append created_at/last_touched_at as YYYY-MM-DD "
                         "after the status column (default off; output is "
                         "byte-identical to the no-flag form otherwise)")
    ls.set_defaults(func=cmd_list)

    wm = sub.add_parser("watermark")
    wm.add_argument("action", choices=["get", "set"])
    wm.add_argument("value", nargs="?", default=None)
    wm.set_defaults(func=cmd_watermark)

    jr = sub.add_parser("journal")
    jr.add_argument("--date", required=True)
    jr.add_argument("--time", default=None, help="run label HH:MM (default: now)")
    jr.add_argument("--line", action="append", required=True)
    jr.set_defaults(func=cmd_journal)

    isc = sub.add_parser("interest-scan")
    isc.add_argument("path", help="path to a session .jsonl")
    isc.set_defaults(func=cmd_interest_scan)

    cov = sub.add_parser("coverage-scan")
    cov.add_argument("path", help="path to a session .jsonl")
    cov.set_defaults(func=cmd_coverage_scan)

    crc = sub.add_parser("corrections-scan")
    crc.add_argument("path", help="path to a session .jsonl")
    crc.set_defaults(func=cmd_corrections_scan)

    of = sub.add_parser("offset")
    of.add_argument("action", choices=["get", "set"])
    of.add_argument("session")
    of.add_argument("value", nargs="?", type=int)
    of.set_defaults(func=cmd_offset)

    sub.add_parser("backup").set_defaults(func=cmd_backup)

    ix = sub.add_parser("index")
    ix.add_argument("--stock", action="store_true", help="embed only stock memory files")
    ix.add_argument("--hippo", action="store_true", help="embed only hippo rows")
    ix.set_defaults(func=cmd_index)

    sr = sub.add_parser("search")
    sr.add_argument("query")
    sr.add_argument("--k", type=int, default=10)
    sr.add_argument("--source", default="all", choices=["all", "hippo", "stock"])
    sr.add_argument("--status", default=None, choices=["open", "closed"])
    sr.set_defaults(func=cmd_search)

    sh = sub.add_parser("show", help="print a memory's full body + metadata by id")
    sh.add_argument("id", type=int)
    sh.set_defaults(func=cmd_show)

    rc = sub.add_parser("recall",
                        help="semantic lookup; print the top match's full body")
    rc.add_argument("query")
    rc.add_argument("--k", type=int, default=1, help="print the top K bodies")
    rc.add_argument("--status", default=None, choices=["open", "closed"])
    rc.set_defaults(func=cmd_recall)

    ck = sub.add_parser("check")
    ck.add_argument("--reindex", action="store_true",
                    help="refresh embeddings before checking")
    ck.set_defaults(func=cmd_check)

    ws = sub.add_parser("working-set")
    ws.add_argument("--session", default="", help="session id; passed through by the "
                    "SessionStart hook")
    ws.set_defaults(func=cmd_working_set)
    sub.add_parser("motd", help="one-line SessionStart greeting: when the last dream ran"
                   ).set_defaults(func=cmd_motd)

    dr = sub.add_parser("dream", help="headless dream: consolidate new sessions via claude -p")
    dr.add_argument("--days", type=int, default=1, help="session window in days (default 1)")
    dr.add_argument("--date", default=None, help="Journal date YYYY-MM-DD (default today)")
    dr.add_argument("--exclude", action="append", default=None,
                    help="session id to skip (repeatable; e.g. the dream's own session)")
    dr.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="invoke + validate but apply nothing; print the plan")
    dr.add_argument("--print-material", dest="print_material", action="store_true",
                    help="print the assembled prompt material and stop (no Claude call)")
    dr.set_defaults(func=cmd_dream)

    sub.add_parser("rollback", help="restore the pre-apply snapshot from the last dream"
                   ).set_defaults(func=cmd_rollback)

    dg = sub.add_parser("digest", help="digest a session transcript")
    dg.add_argument("target", nargs="?")
    dg.add_argument("--list", action="store_true")
    dg.add_argument("--days", type=int, default=1)
    dg.add_argument("--out")
    dg.set_defaults(func=cmd_digest)

    hk = sub.add_parser("hook", help="Claude Code hook entry points (read JSON on stdin)")
    hk.add_argument("event", choices=["session-start", "prompt", "pre-compact", "session-end"])
    hk.set_defaults(func=lambda a: hooks.run(a.event))

    ini = sub.add_parser("init", help="configure, create the store, register hooks and "
                         "commands, install the schedule")
    for flag in ("--owner", "--sessions-dir", "--model", "--dream-hour"):
        ini.add_argument(flag)
    ini.add_argument("--auth", choices=["setup-token", "api-key"])
    ini.add_argument("--download-model", action="store_true")
    ini.add_argument("--no-schedule", action="store_true")
    ini.add_argument("--non-interactive", action="store_true")
    ini.set_defaults(func=lambda a: install.cmd_init(a))
    sub.add_parser("doctor", help="verify the install").set_defaults(func=lambda a: install.cmd_doctor(a))
    un = sub.add_parser("uninstall", help="remove hooks, commands, and the schedule")
    un.add_argument("--purge", action="store_true", help="also delete the data directory")
    un.set_defaults(func=lambda a: install.cmd_uninstall(a))

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
