"""Headless dream orchestrator.

The interactive dream runs its judgment (candidate extraction, loop-closing,
association, Journal narrative) inside a live Claude session, which a
scheduler cannot drive. This module runs that judgment unattended: it shells
out to headless Claude (`claude -p`) with fresh context, takes back a
structured PLAN of proposed mutations, validates it, and only then applies it
through the store.

Model proposes, orchestrator disposes: Claude never writes to the store, so
the validation step is the single chokepoint where guardrails live. The
deterministic pre-passes and digest assembly happen in the CLI (`cmd_dream`);
the judgment + apply loop is here and is fully testable via an injected
`invoke_fn`.
"""
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from hippo_memory import config, crossdedup, handoff, indexer, journal, lessons, store

PLAN_KEYS = ("adds", "closes", "decays", "merges", "links", "annotate", "offsets", "journal")
# handoff is deliberately absent from VALID_KINDS: the dream reads notes to
# self, it never writes one.
VALID_KINDS = ("episodic", "project", "interest", "lesson")


class DreamPlanError(Exception):
    """Raised when headless Claude's reply cannot be parsed into a plan."""


def _extract_json_block(text):
    """Pull the first balanced JSON object out of free text (handles ``` fences)."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1)
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _coerce(raw):
    """Resolve raw stdout into the plan dict, unwrapping the print-mode envelope."""
    raw = (raw or "").strip()
    try:
        outer = json.loads(raw)
    except (ValueError, TypeError):
        outer = None
    if isinstance(outer, dict):
        # claude -p --output-format json wraps the reply text in `result`
        if isinstance(outer.get("result"), str):
            return _coerce(outer["result"])
        if any(k in outer for k in PLAN_KEYS):
            return outer
    block = _extract_json_block(raw)
    if block is None:
        raise DreamPlanError("no JSON plan found in headless Claude output")
    try:
        return json.loads(block)
    except ValueError as e:
        raise DreamPlanError(f"plan JSON did not parse: {e}")


def parse_plan(raw):
    """Parse headless Claude output into a normalized plan with all keys present."""
    obj = _coerce(raw)
    if not isinstance(obj, dict):
        raise DreamPlanError("plan is not a JSON object")
    return {k: obj.get(k, []) or [] for k in PLAN_KEYS}


def charge_floor(signals):
    """Map the corrections pre-pass signals (union across the run's sessions) to
    the minimum weight a kind=lesson add must carry. charged/interrupt is the
    flashbulb tier; any milder pushback (dissatisfaction, directive) still floors
    above zero. No signals, no floor."""
    signals = set(signals or ())
    if not signals:
        return 0.0
    if "charged" in signals or "interrupt" in signals:
        return config.LESSON_FLOOR_CHARGED
    return config.LESSON_FLOOR_MILD


def _in_refractory(row, now, days):
    """True when `row` is a lesson younger than `days`: too fresh for the dream
    to close, decay, or merge away."""
    if row["kind"] != "lesson":
        return False
    created = datetime.fromisoformat(row["created_at"])
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_days = (now - created).total_seconds() / 86400.0
    return age_days < days


def validate_plan(conn, plan, charge=0.0, now=None):
    """Filter a plan to safe, applicable actions. Returns (clean_plan, rejections).

    Guards: valid kinds and non-empty titles on adds; closes must target an
    existing, non-pinned memory; links must point at existing ids; offsets
    must be non-negative integers. Nothing here can delete a memory or touch
    the pinned floor.

    Lesson guards: kind=lesson adds get their weight floored at `charge` (the
    stamped severity from the corrections pre-pass, via charge_floor), so the
    model cannot under-price a charged correction; and closes/decays/merges may
    not touch a lesson inside its refractory period (LESSON_REFRACTORY_DAYS).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    refractory = config.LESSON_REFRACTORY_DAYS
    rejections = []
    clean = {k: [] for k in PLAN_KEYS}

    for a in plan.get("adds", []):
        kind = a.get("kind")
        title = (a.get("title") or "").strip()
        if kind not in VALID_KINDS:
            rejections.append(f"add rejected: bad kind {kind!r} ({title!r})")
            continue
        if not title:
            rejections.append(f"add rejected: empty title (kind {kind})")
            continue
        if kind == "lesson" and charge:
            w = a.get("weight")
            if not isinstance(w, (int, float)) or isinstance(w, bool):
                w = 0.0
            if float(w) < charge:
                a = dict(a, weight=float(charge))
        clean["adds"].append(a)

    for mid in plan.get("closes", []):
        row = store.get_memory(conn, mid)
        if row is None:
            rejections.append(f"close rejected: no memory {mid}")
            continue
        if row["pinned"]:
            rejections.append(f"close rejected: {mid} is pinned (floor protected)")
            continue
        if _in_refractory(row, now, refractory):
            rejections.append(f"close rejected: {mid} is a lesson in refractory")
            continue
        clean["closes"].append(mid)

    for d in plan.get("decays", []):
        mid, w = d.get("id"), d.get("weight")
        row = store.get_memory(conn, mid)
        if row is None:
            rejections.append(f"decay rejected: no memory {mid}")
            continue
        if row["pinned"]:
            rejections.append(f"decay rejected: {mid} is pinned (floor protected)")
            continue
        if _in_refractory(row, now, refractory):
            rejections.append(f"decay rejected: {mid} is a lesson in refractory")
            continue
        if not isinstance(w, (int, float)) or isinstance(w, bool):
            rejections.append(f"decay rejected: {mid} bad weight {w!r}")
            continue
        clean["decays"].append(d)

    for m in plan.get("merges", []):
        f, into = m.get("from"), m.get("into")
        rf, ri = store.get_memory(conn, f), store.get_memory(conn, into)
        if rf is None or ri is None:
            rejections.append(f"merge rejected: missing id in {f}->{into}")
            continue
        if f == into:
            rejections.append(f"merge rejected: self-merge {f}")
            continue
        if rf["pinned"] or ri["pinned"]:
            rejections.append(f"merge rejected: pinned id in {f}->{into} (floor protected)")
            continue
        if _in_refractory(rf, now, refractory):
            rejections.append(f"merge rejected: {f} is a lesson in refractory")
            continue
        clean["merges"].append(m)

    for lk in plan.get("links", []):
        f, t = lk.get("from"), lk.get("to")
        if store.get_memory(conn, f) is None or store.get_memory(conn, t) is None:
            rejections.append(f"link rejected: missing id in {f}->{t}")
            continue
        clean["links"].append(lk)

    # annotate: fold a handoff note's content into the episodic or project row
    # it corroborates. Additive (body append + source link), but the pinned
    # floor stays untouched and a note may not annotate a note.
    for an in plan.get("annotate", []):
        mid, src = an.get("id"), an.get("source")
        text = (an.get("text") or "").strip()
        row, srow = store.get_memory(conn, mid), store.get_memory(conn, src)
        if row is None or srow is None:
            rejections.append(f"annotate rejected: missing id in {src}->{mid}")
            continue
        if srow["kind"] != "handoff":
            rejections.append(f"annotate rejected: source {src} is not a handoff")
            continue
        if row["kind"] == "handoff":
            rejections.append(f"annotate rejected: target {mid} is a handoff")
            continue
        if row["pinned"]:
            rejections.append(f"annotate rejected: {mid} is pinned (floor protected)")
            continue
        if not text:
            rejections.append(f"annotate rejected: empty text for {mid}")
            continue
        clean["annotate"].append({"id": mid, "source": src, "text": text})

    for off in plan.get("offsets", []):
        val = off.get("value")
        if not isinstance(val, int) or val < 0:
            rejections.append(f"offset rejected: {off.get('session')} value {val!r}")
            continue
        clean["offsets"].append(off)

    clean["journal"] = [str(l) for l in plan.get("journal", [])]
    return clean, rejections


def count_destructive(plan):
    """Destructive ops are close + decay + merge. Promotions (adds) are additive."""
    return (len(plan.get("closes", [])) + len(plan.get("decays", []))
            + len(plan.get("merges", [])))


def apply_plan(conn, plan):
    """Apply a validated plan to the store. Returns a counts summary."""
    added = closed = decayed = merged = linked = offsets = rearmed = 0
    for a in plan["adds"]:
        # dedup_key path of re-offense re-arming: a lesson add whose key
        # matches an existing lesson is an offense repeat, not a new row.
        # Plain upsert would bump recurrence but not weight; rearm does both
        # (and reopens a retired lesson: a relapse un-graduates the scar).
        if a["kind"] == "lesson" and a.get("dedup_key"):
            existing = conn.execute(
                "SELECT id, kind FROM memories WHERE dedup_key = ?",
                (a["dedup_key"],),
            ).fetchone()
            if existing is not None and existing["kind"] == "lesson":
                lessons.rearm(conn, existing["id"])
                rearmed += 1
                continue
        store.upsert_memory(
            conn, a["kind"], a["title"].strip(), body=a.get("body", "") or "",
            dedup_key=a.get("dedup_key"), weight=float(a.get("weight", 0.0) or 0.0),
        )
        added += 1
    for r in plan.get("rearms", []):
        lessons.rearm(conn, r["id"])
        rearmed += 1
    for mid in plan["closes"]:
        store.close_memory(conn, mid)
        closed += 1
    for d in plan["decays"]:
        store.set_weight(conn, d["id"], float(d["weight"]))
        decayed += 1
    for m in plan["merges"]:
        store.merge_memory(conn, m["from"], m["into"])
        merged += 1
    for lk in plan["links"]:
        store.add_link(conn, lk["from"], lk["to"], lk.get("kind", "relates"))
        linked += 1
    annotated = 0
    for an in plan.get("annotate", []):
        row = store.get_memory(conn, an["id"])
        body = (row["body"] or "").rstrip()
        stamp = f"[from handoff {an['source']}] {an['text']}"
        body = f"{body}\n\n{stamp}".strip() if body else stamp
        conn.execute("UPDATE memories SET body = ? WHERE id = ?", (body, an["id"]))
        store.add_link(conn, an["source"], an["id"], "source")
        annotated += 1
    if annotated:
        conn.commit()
    for off in plan["offsets"]:
        store.set_state(conn, f"session:{off['session']}", str(off["value"]))
        offsets += 1
    return {"added": added, "closed": closed, "decayed": decayed, "merged": merged,
            "linked": linked, "annotated": annotated, "offsets": offsets,
            "rearmed": rearmed}


PROMPT_HEADER = """\
You are running the nightly memory dream for {owner}'s memory store, headless
and unattended. Below is the digest of new session activity plus deterministic
pre-pass output (interest / coverage / corrections scans). Apply the dream's
judgment and return ONLY a JSON object describing the mutations to make. Do not
write prose outside the JSON.

Extraction rules (condensed from the dream procedure):
- Promote: capture interests (topics/worries/reactions {owner} engaged with;
  kind=interest), open loops (unfinished threads; kind=episodic), decisions and
  durable project facts (kind=project). Drop one-off chatter, but never drop an
  uncleared corrections-scan hit or an unread external artifact.
- Lessons: when a CORRECTIONS-SCAN block shows {owner} pushing back, correcting,
  or redirecting the assistant, promote the durable behavioral rule as
  kind=lesson, never as episodic. A lesson is a scar, not an open loop: title
  the rule to follow, not the incident. The orchestrator floors the weight of
  lesson adds from the scan's severity, so set weight only to boost beyond
  that floor. Lessons in their first 30 days cannot be closed, decayed, or
  merged; do not propose it. A REPEAT of an already-filed lesson's failure is
  a re-offense: add it with the existing lesson's dedup_key (the orchestrator
  re-arms that lesson's weight; it also folds semantic near-duplicates, but
  the explicit key is the reliable path).
- Reuse a stable dedup_key per recurring thread so recurrence increments.
- Close loops that the sessions resolved (by memory id).
- Decay: lower the weight of a memory whose relevance has faded (negative weight
  demotes it; the floor of pinned memories is off-limits).
- Merge: fold a duplicate/subsumed memory into the one that supersedes it
  ({{"from": absorbed, "into": survivor}}); both must be non-pinned.
- Propose links only between existing memory ids named in the material.
- Write a few Journal lines; every dropped item gets a one-line reason.
Pinned memories are the protected floor: never close, decay, or merge them.

HANDOFF NOTES: a session block may open with the agent's own notes to self
(kind=handoff, labelled State/Decided/Dead ends/Assumed/Next/Calibration/
Unsure). Treat them as that session's primary account and read them before
the digest. The scanners still run and corrections-scan still wins on what
{owner} wanted. Where a note's Decided or Dead ends content corroborates or
extends an existing episodic/project row, fold it in with an "annotate" op
naming the note as source. Where a note contradicts the transcript, journal
the discrepancy ("handoff [id] said X; transcript shows Y") and follow the
transcript. Never add kind=handoff (the orchestrator rejects it), never
promote a note's content to a lesson (lessons come from corrections-scan
only), and never close a handoff row yourself: the orchestrator closes,
folds, and retires them deterministically after apply.

Return JSON with these keys (omit any you do not use):
{{
  "adds":     [{{"kind": "interest|episodic|project|lesson", "title": "...", "body": "...", "dedup_key": "..."}}],
  "closes":   [<memory id>, ...],
  "decays":   [{{"id": <memory id>, "weight": <float, e.g. -1.5>}}],
  "merges":   [{{"from": <absorbed id>, "into": <survivor id>}}],
  "links":    [{{"from": <id>, "to": <id>, "kind": "relates"}}],
  "annotate": [{{"id": <episodic or project id>, "source": <handoff id>, "text": "..."}}],
  "journal":  ["promoted: ...", "closed: ...", "decayed: ...", "merged: ...", "dropped: ... (reason)"]
}}

=== DREAM MATERIAL ===
"""


def build_prompt(material, owner=None):
    return PROMPT_HEADER.format(owner=owner or config.OWNER) + material


# Failure signatures that get a retry rather than an immediate abort:
# ordinary blips (overload, rate-limit, 5xx, network) that clear in seconds.
# Auth failures (401 / authentication_failed) are deliberately NOT here: at 02:30
# nothing rotates the credential inside a retry window, so an expired token is a
# deterministic state, not a blip. Retrying it would mask a credential
# failure on lucky nights and fail the rest; let it surface loudly (the failure
# sentinel + working-set header carry the signal) so the credential gets fixed.
_TRANSIENT = (
    "429", "rate", "overloaded", "529",
    "500", "502", "503", "internal server",
    "timeout", "timed out", "connection", "econnreset",
)


def _is_transient(detail):
    d = detail.lower()
    return any(sig in d for sig in _TRANSIENT)


def _headless_env():
    """Build the environment for the headless `claude -p` subprocess from
    config.AUTH_MODE, never printing or logging the credential it carries."""
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    if config.AUTH_MODE == "setup-token":
        if not config.TOKEN_PATH.exists():
            raise DreamPlanError(
                "no token at "
                f"{config.TOKEN_PATH}; run `hippo init` to store one"
            )
        env["CLAUDE_CODE_OAUTH_TOKEN"] = config.TOKEN_PATH.read_text().strip()
        env.pop("ANTHROPIC_API_KEY", None)
    elif config.AUTH_MODE == "api-key":
        if not env.get("ANTHROPIC_API_KEY"):
            raise DreamPlanError("HIPPO_AUTH=api-key but ANTHROPIC_API_KEY is not set")
    else:
        raise DreamPlanError(f"unknown HIPPO_AUTH mode {config.AUTH_MODE!r}")
    # The headless `claude -p` loads the user's own global settings, so
    # without this the user's own hippo hooks would fire inside the dream's
    # own session (SessionStart injecting the working set into the dream
    # call, session-end logging a record for it). hooks.run checks this and
    # returns before doing anything.
    env["HIPPO_IN_DREAM"] = "1"
    return env


def invoke_claude(prompt, timeout=None, retries=None, backoff=30, sleep=time.sleep,
                  runner=subprocess.run):
    """One headless, fresh-context turn in print mode. The prompt goes on stdin
    (an argv prompt overflows ARG_MAX on a busy day). Runs from the data
    directory so its own transcript never joins the watched project.

    Retries transient failures (429, overload, 5xx, network) with a short
    backoff before giving up, since the nightly run hits these with no one
    watching. An auth failure is not retried: nothing rotates the credential
    inside a retry window, so an expired token is a deterministic state, not
    a blip, and it should surface loudly instead of being masked."""
    timeout = timeout or config.DREAM_TIMEOUT
    retries = config.DREAM_RETRIES if retries is None else retries
    cmd = ["claude", "-p", "--output-format", "json"]
    if config.MODEL:
        cmd += ["--model", config.MODEL]
    env = _headless_env()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    last_err = None
    for attempt in range(retries + 1):
        try:
            proc = runner(cmd, input=prompt, capture_output=True, text=True,
                          timeout=timeout, env=env, cwd=str(config.DATA_DIR))
        except subprocess.TimeoutExpired:
            # A blip like the transient exit codes below, so it gets the same
            # retry-with-backoff rather than escaping the loop uncaught.
            last_err = f"claude -p timed out after {timeout}s"
            if attempt < retries:
                sleep(backoff)
                continue
            break
        except OSError as e:
            # `claude` missing from the scheduler's PATH, or another exec
            # failure: not transient, so it raises immediately rather than
            # spending the retry budget on a command that will never run.
            raise DreamPlanError(f"could not run claude -p: {e}") from e
        if proc.returncode == 0:
            return proc.stdout
        detail = (proc.stderr.strip() or proc.stdout.strip())[:500]
        last_err = f"claude -p exited {proc.returncode}: {detail}"
        if attempt < retries and _is_transient(detail):
            sleep(backoff)
            continue
        break
    raise DreamPlanError(last_err)


def _load_embedder():
    """Best-effort ONNX embedder for the cross-store dedup pass. Returns None
    when the model cannot load (missing model dir, broken onnxruntime): the
    dream must never die, or silently drop material, because dedup could not
    run; it just files the adds and lets a later pass catch satellites."""
    try:
        from hippo_memory import embed
        return embed.get_embedder()
    except Exception:
        return None


def _resolve_embedder(conn, clean, embedder):
    """Load the ONNX embedder at most once, and only when a dedup pass will
    run: there are adds, and either stock vectors exist (satellite
    diversion) or a lesson add faces existing open lessons (re-offense fold).
    Keeps the model load out of unindexed stores and the offline test suite."""
    if embedder is not None or not clean["adds"]:
        return embedder
    need = bool(crossdedup.stock_vectors(conn, config.EMBED_MODEL))
    if not need:
        need = (any(a.get("kind") == "lesson" for a in clean["adds"])
                and bool(lessons.open_lessons(conn)))
    return _load_embedder() if need else None


def _fold_reoffenses(conn, clean, embedder):
    """Within-store dedup for lessons, annotation only: a lesson add that folds
    onto an existing open lesson leaves the plan and becomes an entry in
    clean["rearms"] plus a Journal line; apply_plan performs the
    re-arm, so an aborted plan leaves the store untouched. Runs BEFORE the
    stock diversion pass: a re-offense signal outranks satellite cleanup."""
    clean.setdefault("rearms", [])
    if embedder is None or not clean["adds"]:
        return 0
    kept, folds = lessons.fold_reoffenses(conn, clean["adds"], embedder)
    clean["adds"] = kept
    for f in folds:
        clean["rearms"].append({"id": f["id"]})
        clean["journal"].append(
            f"re-offense: lesson add '{f['add'].get('title', '').strip()}' "
            f"folded into [{f['id']}] '{f['title']}' (cosine {f['cosine']:.2f}); "
            f"weight re-armed"
        )
    return len(folds)


def _divert_satellites(conn, clean, embedder):
    """Run the cross-store dedup pass over the validated adds, in place.
    Diverted adds leave the plan and become Journal merge-candidate lines.
    Returns the diversion count."""
    if embedder is None or not clean["adds"]:
        return 0
    if not crossdedup.stock_vectors(conn, config.EMBED_MODEL):
        return 0
    kept, diversions = crossdedup.divert(
        conn, clean["adds"], embedder, config.EMBED_MODEL,
        config.CROSS_DEDUP_THRESHOLD,
    )
    clean["adds"] = kept
    for d in diversions:
        name = os.path.basename(d["ref"])
        clean["journal"].append(
            f"merge-candidate: add '{d['add'].get('title', '').strip()}' "
            f"(kind {d['add'].get('kind')}) paraphrases stock {name} "
            f"(cosine {d['cosine']:.2f}); not filed - fold the point into the "
            f"stock file or retitle it as something the file does not say"
        )
    return len(diversions)


def _index_store(conn, embedder, notes):
    """Embed every hippo row whose vector is missing or stale, so a memory the
    dream just promoted is retrievable by `search`/`recall` the same night.
    Before this, nothing but a manual `hippo index` ever fed the
    index, so recall went blind to everything recent while `check` kept
    passing on the older rows its probes happened to point at.

    Best-effort: a dream that consolidated correctly must not fail because the
    embedder is down. But a skipped index is written to the Journal rather than
    swallowed, because the failure is otherwise invisible until recall misses.
    """
    emb = embedder or _load_embedder()
    if emb is None:
        notes.append("WARNING: embedder unavailable; tonight's memories are not "
                     "indexed and semantic recall cannot see them until "
                     "`hippo index` runs")
        return 0
    try:
        return indexer.index_hippo(conn, emb, config.EMBED_MODEL)
    except Exception as e:  # noqa: BLE001 - indexing must never kill a good dream
        notes.append(f"WARNING: indexing failed ({e}); semantic recall cannot see "
                     f"tonight's memories until `hippo index` runs")
        return 0


def _save_plan(plan_dir, date, plan):
    """Persist a plan as JSON for review (aborted runs, audit). Returns the path."""
    d = Path(plan_dir) if plan_dir else Path(config.JOURNAL_PATH).parent
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"dream-plan-{date}.json"
    path.write_text(json.dumps(plan, indent=2))
    return str(path)


def run_dream(conn, material, invoke_fn=invoke_claude, *, date,
              journal_path=None, run_label="dream", apply=True,
              destructive_cap=None, plan_dir=None, charge=0.0,
              embedder=None, sessions=()):
    """Orchestrate one headless dream: invoke -> parse -> validate -> apply.

    `invoke_fn(prompt) -> raw_stdout` is injectable so tests drive canned plans;
    the default shells out to `claude -p`. The pre-apply snapshot/backup is the
    caller's job (cmd_dream). A run whose destructive ops (close+decay+merge)
    exceed `destructive_cap` aborts without applying and saves the plan for review.
    `charge` is the lesson weight floor (charge_floor over the corrections
    pre-pass signals), computed by the caller from the same scan the material
    embeds. `embedder` is injectable for tests; None lazy-loads the ONNX
    embedder for the cross-store dedup pass, which quietly skips when no
    embedder or stock vectors are available.
    """
    if journal_path is None:
        journal_path = config.JOURNAL_PATH
    if destructive_cap is None:
        destructive_cap = config.DREAM_DESTRUCTIVE_CAP
    prompt = build_prompt(material)
    raw = invoke_fn(prompt)
    plan = parse_plan(raw)
    clean, rejections = validate_plan(conn, plan, charge=charge)
    emb = _resolve_embedder(conn, clean, embedder)
    folded = _fold_reoffenses(conn, clean, emb)
    diverted = _divert_satellites(conn, clean, emb)

    summary = {"rejections": rejections, "rejected": len(rejections),
               "diverted": diverted, "folded": folded}
    if not apply:
        summary["dry_run"] = True
        summary["plan"] = clean
        return summary

    destructive = count_destructive(clean)
    if destructive > destructive_cap:
        summary["aborted"] = True
        summary["destructive"] = destructive
        summary["cap"] = destructive_cap
        summary["plan_path"] = _save_plan(plan_dir, date, clean)
        return summary

    summary.update(apply_plan(conn, clean))

    # Deterministic lesson retirement, after apply so tonight's re-arms count:
    # a lesson touched by a re-arm tonight is not clean and stays open.
    retired = lessons.retire_clean(conn)
    for r in retired:
        clean["journal"].append(
            f"retired: lesson [{r['id']}] '{r['title']}' closed after "
            f"{config.LESSON_RETIREMENT_DAYS} days clean"
        )
    summary["retired"] = len(retired)

    # Handoff lifecycle, after apply so tonight's closes count when deciding
    # which notes have no live loop left. Deterministic; the model never
    # closes a note itself.
    hk = handoff.housekeep(conn)
    for hid in hk["closed_done"]:
        clean["journal"].append(f"handoff: closed [{hid}], every carried loop closed")
    for old, new in hk["superseded"]:
        clean["journal"].append(f"handoff: folded [{old}] into [{new}] (superseded)")
    for hid in hk["closed_uncarried"]:
        clean["journal"].append(f"handoff: closed [{hid}], rode one cycle uncarried")
    summary["handoffs_closed"] = len(hk["closed_done"]) + len(hk["closed_uncarried"])
    summary["handoffs_folded"] = len(hk["superseded"])
    for sid in sessions:
        n = len(handoff.list_rows(conn, session=sid, include_closed=True))
        reason = handoff.session_end_reason(sid)
        ended = f"; ended: {reason}" if reason else ""
        if n:
            clean["journal"].append(
                f"session {sid}: handoff-backed ({n} note{'s' if n != 1 else ''}){ended}")
        else:
            clean["journal"].append(f"session {sid}: no handoff{ended}")

    store.rescore_all(conn)
    summary["indexed"] = _index_store(conn, emb, clean["journal"])
    if clean["journal"]:
        journal.add_entry(journal_path, date, run_label, clean["journal"])
        summary["journal_lines"] = len(clean["journal"])
    return summary
