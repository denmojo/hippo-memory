"""Digest of a Claude Code session transcript.

A digest prints every user turn in full plus all assistant prose and tool
invocations, dropping only verbose tool-result blobs, so a long session that
would otherwise get truncated by a paginated read fits in one pass.
"""
import sys, os, json, glob, datetime, argparse
from pathlib import Path

from hippo_memory import config

USER_NOISE = ("Caveat:", "<command-name>", "<command-message>", "<local-command",
              "<task-notification", "Base directory for this skill",
              "<system-reminder>")


def local(ts):
    """ISO timestamp -> local datetime, or None."""
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def block_text(content):
    if isinstance(content, str):
        return content
    out = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(b.get("text", ""))
    return "".join(out)


def is_user_noise(txt):
    s = txt.strip()
    if not s:
        return True
    return s.startswith(USER_NOISE)


def tool_preview(b):
    name = b.get("name", "?")
    inp = b.get("input", {}) or {}
    if name == "Bash":
        arg = (inp.get("command", "") or "").replace("\n", " ")
    elif name in ("Read", "Edit", "Write", "NotebookEdit"):
        arg = inp.get("file_path", "") or ""
    elif name in ("Grep", "Glob"):
        arg = (inp.get("pattern", "") or "") + " " + (inp.get("path", "") or "")
    elif name == "Skill":
        arg = inp.get("skill", "") or ""
    elif name in ("Agent", "Task"):
        arg = (inp.get("subagent_type", "") or "") + ": " + (inp.get("description", "") or "")
    else:
        arg = json.dumps(inp, ensure_ascii=False)
    arg = arg.strip()
    if len(arg) > 200:
        arg = arg[:200] + "..."
    return "  [tool: {}] {}".format(name, arg)


def parse(path):
    """Return list of records: (kind, dt, payload)."""
    recs = []
    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except Exception:
                continue
            recs.append(o)
    return recs


def session_id(path, recs):
    for o in recs:
        sid = o.get("sessionId")
        if sid:
            return sid
    return os.path.splitext(os.path.basename(path))[0]


def stats(recs):
    users = 0
    asst = 0
    first_prompt = ""
    times = []
    for o in recs:
        dt = local(o.get("timestamp"))
        if dt:
            times.append(dt)
        t = o.get("type")
        m = o.get("message", {}) or {}
        if t == "user":
            txt = block_text(m.get("content"))
            if not is_user_noise(txt):
                users += 1
                if not first_prompt:
                    first_prompt = txt.strip().replace("\n", " ")
        elif t == "assistant":
            asst += 1
    span = (min(times), max(times)) if times else (None, None)
    return users, asst, first_prompt, span


def digest_path(path):
    """Digest one session transcript, returning the report as a string."""
    path = str(path)
    recs = parse(path)
    sid = session_id(path, recs)
    users, asst, _, (start, end) = stats(recs)
    lines = []
    lines.append("== SESSION DIGEST ==")
    lines.append("source:  " + path)
    lines.append("session: " + sid)
    if start and end:
        if start.date() == end.date():
            lines.append("span:    {} {} - {} {}".format(
                start.strftime("%Y-%m-%d"), start.strftime("%H:%M"),
                end.strftime("%H:%M"), end.strftime("%Z")))
        else:
            lines.append("span:    {} - {} {}".format(
                start.strftime("%Y-%m-%d %H:%M"),
                end.strftime("%Y-%m-%d %H:%M"), end.strftime("%Z")))
    lines.append("turns:   {} user / {} assistant".format(users, asst))
    lines.append("-" * 60)
    lines.append("(complete digest: every user turn in full; all assistant prose and")
    lines.append(" tool calls; tool-result bodies replaced by line counts)")
    lines.append("")
    for o in recs:
        t = o.get("type")
        m = o.get("message", {}) or {}
        dt = local(o.get("timestamp"))
        stamp = dt.strftime("%H:%M") if dt else ""
        if t == "user":
            content = m.get("content")
            txt = block_text(content)
            if is_user_noise(txt):
                # may still carry a tool_result; ignore for digest
                continue
            lines.append("### USER  [{}]".format(stamp))
            lines.append(txt.strip())
            lines.append("")
        elif t == "assistant":
            content = m.get("content")
            blines = []
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text":
                        s = b.get("text", "").strip()
                        if s:
                            blines.append(s)
                    elif bt == "tool_use":
                        blines.append(tool_preview(b))
            elif isinstance(content, str) and content.strip():
                blines.append(content.strip())
            if not blines:
                continue
            lines.append("### ASSISTANT  [{}]".format(stamp))
            lines.append("\n".join(blines))
            lines.append("")
    return "\n".join(lines) + "\n"


def find_jsonls(project, days):
    """Return .jsonl paths under project modified within the last `days`,
    oldest first."""
    files = glob.glob(os.path.join(project, "*.jsonl"))
    cutoff = datetime.datetime.now().astimezone() - datetime.timedelta(days=days)
    out = []
    for f in files:
        mt = datetime.datetime.fromtimestamp(os.path.getmtime(f)).astimezone()
        if mt >= cutoff:
            out.append((f, mt))
    out.sort(key=lambda x: x[1])
    return [Path(f) for f, _ in out]


def index(project, days, today_only=False):
    if today_only:
        days = 2  # widen scan, filter to today's date below
    paths = find_jsonls(project, days)
    today = datetime.date.today()
    out = []
    out.append("== SESSION INDEX ({}) ==".format(
        "today" if today_only else "last {}d".format(days)))
    shown = 0
    for p in paths:
        f = str(p)
        mt = datetime.datetime.fromtimestamp(os.path.getmtime(f)).astimezone()
        recs = parse(f)
        users, asst, first, (start, end) = stats(recs)
        anchor = (start or mt).date()
        if today_only and anchor != today:
            continue
        sid = session_id(f, recs)
        st = start.strftime("%H:%M") if start else mt.strftime("%H:%M")
        en = end.strftime("%H:%M") if end else ""
        if first and len(first) > 90:
            first = first[:90] + "..."
        out.append("  {}  {}-{}  {:>2}u/{:>3}a  {}".format(
            sid[:8], st, en, users, asst, first))
        shown += 1
    if not shown:
        out.append("  (none)")
    out.append("")
    out.append("digest one with:  hippo digest <session-id>")
    return "\n".join(out) + "\n"


def resolve(arg, project):
    """Resolve arg to a .jsonl path. Map an export .txt by mtime proximity."""
    if arg.endswith(".jsonl") and os.path.exists(arg):
        return arg, None
    # bare session id
    cand = os.path.join(project, arg + ".jsonl")
    if os.path.exists(cand):
        return cand, None
    cand2 = os.path.join(project, arg)
    if os.path.exists(cand2) and cand2.endswith(".jsonl"):
        return cand2, None
    # bare id prefix (filenames carry the full UUID)
    if not os.path.exists(arg):
        pref = sorted(glob.glob(os.path.join(project, arg + "*.jsonl")))
        if len(pref) == 1:
            return pref[0], None
        if len(pref) > 1:
            sys.stderr.write("hippo digest: '{}' matches {} sessions; be more specific\n".format(arg, len(pref)))
            return None, None
    # export .txt -> nearest jsonl by mtime
    if os.path.exists(arg):
        txt_mt = os.path.getmtime(arg)
        best, best_d = None, None
        for f in glob.glob(os.path.join(project, "*.jsonl")):
            d = abs(os.path.getmtime(f) - txt_mt)
            if best_d is None or d < best_d:
                best, best_d = f, d
        if best is not None:
            return best, best_d
    return None, None


def run(argv=None):
    ap = argparse.ArgumentParser(prog="hippo digest", add_help=True)
    ap.add_argument("target", nargs="?")
    ap.add_argument("--today", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--out")
    ap.add_argument("--project", default=str(config.SESSIONS_DIR))
    a = ap.parse_args(argv)

    if a.today or a.list:
        sys.stdout.write(index(a.project, a.days, a.today))
        return 0

    if not a.target:
        ap.print_help()
        return 2

    path, delta = resolve(a.target, a.project)
    if not path:
        sys.stderr.write("hippo digest: could not resolve '{}'\n".format(a.target))
        return 1
    if delta is not None:
        note = "" if delta <= 120 else "  WARNING: large gap, verify this is the right session"
        sys.stderr.write(
            "hippo digest: mapped export to {} (mtime delta {:.0f}s){}\n".format(
                os.path.basename(path), delta, note))
    report = digest_path(path)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(report)
        print("digest written to {}".format(a.out))
    else:
        sys.stdout.write(report)
    return 0
