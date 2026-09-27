"""Paths, tuning constants, and runtime settings.

Resolution order for each setting: environment variable, then the env file
(HIPPO_ENV_FILE, default ~/.config/hippo-memory/env, KEY=VALUE lines), then
the default under the data directory."""
import os
from pathlib import Path

_HOME = Path.home()


def _load_env_file():
    f = Path(os.environ.get("HIPPO_ENV_FILE", _HOME / ".config/hippo-memory/env"))
    out = {}
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = os.path.expanduser(v.strip())
    return out


ENV_FILE = Path(os.environ.get("HIPPO_ENV_FILE", _HOME / ".config/hippo-memory/env"))

_OVERRIDES = _load_env_file()


def _get(key, default=None):
    return os.environ.get(key) or _OVERRIDES.get(key) or default


def _resolve(key, default):
    return Path(_get(key, default))


def _get_int(key, default):
    # A hook calls this at import, before hooks.run's own try/except is
    # reached, so a malformed override must fall back rather than raise -
    # otherwise every `hippo hook` traces and exits 1, breaking the "hooks
    # exit silently" contract before it even starts.
    try:
        return int(_get(key, default))
    except (TypeError, ValueError):
        return default


def _get_int_list(key, default):
    raw = _get(key, None)
    if raw is None:
        return list(default)
    try:
        return [int(x) for x in raw.split(",") if x.strip()]
    except ValueError:
        return list(default)


DATA_DIR = _resolve("HIPPO_DATA_DIR", _HOME / ".local/share/hippo-memory")
_DATA = DATA_DIR

OWNER = _get("HIPPO_OWNER", "the user")
AUTH_MODE = _get("HIPPO_AUTH", "setup-token")          # or "api-key"
MODEL = _get("HIPPO_MODEL", "")                          # passed as --model when set
DREAM_HOUR = _get("HIPPO_DREAM_HOUR", "02:30")
DREAM_TIMEOUT = _get_int("HIPPO_DREAM_TIMEOUT", 600)
DREAM_RETRIES = _get_int("HIPPO_DREAM_RETRIES", 2)
COMPACTIONS_PATH = _resolve("HIPPO_COMPACTIONS", _DATA / "compactions.jsonl")
CONTEXT_WATCH_DIR = _resolve("HIPPO_CONTEXT_WATCH_DIR", _DATA / "context-watch")
NUDGE_TOKENS = _get_int_list("HIPPO_HANDOFF_NUDGE_TOKENS", [150000, 300000, 500000])
CONTEXT_WINDOW = _get_int("HIPPO_CONTEXT_WINDOW", 1000000)
TOKEN_PATH = _resolve("HIPPO_TOKEN_FILE", _DATA / ".oauth-token")

DB_PATH = _resolve("HIPPO_DB", _DATA / "hippo.db")
JOURNAL_PATH = _resolve("HIPPO_JOURNAL", _DATA / "Memory Dream Journal.md")
JOURNAL_ARCHIVE_PATH = JOURNAL_PATH.with_name("Memory Dream Journal Archive.md")
HIPPO_BACKUP_PATH = _resolve("HIPPO_BACKUP", _DATA / "backup" / "hippo.db")
# Two-line file (session id, transcript path) maintained by the session-track
# hook; `handoff add` reads the id from it when --session is not given.
CURRENT_SESSION_PATH = _resolve("HIPPO_CURRENT_SESSION", _DATA / "current-session")
# Appended by the session-end hook: one JSON line per session end
# {session_id, reason, at, had_handoff}; the dream reads the reason into its
# per-session handoff journal line.
SESSION_ENDS_PATH = _resolve("HIPPO_SESSION_ENDS", _DATA / "session-ends.jsonl")
# Uncarried handoff notes ride one cycle; the dream closes them past this age.
HANDOFF_UNCARRIED_HOURS = 24
# Pre-apply snapshot taken before an autonomous dream mutates the store, so a bad
# run is reversible with `hippo rollback` (distinct from the routine backup).
HIPPO_ROLLBACK_PATH = _resolve("HIPPO_ROLLBACK", _DATA / "backup" / "hippo.pre-dream.db")
# Sentinel written when an autonomous dream fails (e.g. a scheduled OAuth-token
# expiry), cleared on the next success. The working-set header reads it so a
# failed dream surfaces as such instead of silently presenting stale carryover.
DREAM_FAILURE_PATH = _resolve("HIPPO_DREAM_FAILURE", _DATA / "last-dream-error.json")

# Autonomous-dream blast-radius guard: if a single run proposes more than this many
# destructive ops (close + decay + merge), it aborts without applying and saves the
# plan for review instead. Promotions (adds) are additive and never counted.
DREAM_DESTRUCTIVE_CAP = 10

# Number of newest entries kept in the active Journal before archival.
JOURNAL_ACTIVE_ENTRIES = 30

# Salience weights (tunable during the observation period).
WEIGHTS = {
    "open_loop": 2.0,
    "recency": 1.5,
    "recurrence": 1.0,
    "weight": 1.0,
    "pin": 5.0,
}

# Half-life in days for recency decay.
RECENCY_HALFLIFE_DAYS = 7.0

# Interest/concern memories score on a different axis. They are not open loops
# and do not recur in the SQL sense, so the open_loop/recurrence terms would bury
# them. Instead they get a high baseline (so a single-mention interest ranks among
# the operational threads) plus slow recency decay.
#
# Half-life tuned 30 -> 7: at 30 days the recency term was nearly flat across all
# recent interests (a 4-day age span moved score by ~0.1 atop the 3.5 baseline), so
# the SessionStart "recent context" tier compressed into a 0.13-wide band and the
# top-N cut fell mid-bunch. At 7 days the live batches spread the tier to ~0.44,
# recently-touched interests rank above stale ones, and lower loops mix into the
# recent tier as intended. Interests still persist (a two-week-old interest keeps
# ~0.25 recency, well above floor) and the boost weight pins durable ones up; the
# tie-aware cut in workingset handles same-batch exact ties the half-life cannot
# separate.
INTEREST_BASELINE = 3.5
INTEREST_HALFLIFE_DAYS = 7.0

# Lesson memories (charged corrections promoted by the dream) score on their own
# axis, like interests, but tuned for how a traumatic memory decays differently
# from an episodic one: a fresh episodic open loop scores 3.5 (open_loop 2.0 +
# recency 1.5) while a fresh interest scores 5.0 off its baseline alone, so a
# behavioral scar promoted as plain episodic lost to same-day interests by
# construction. The lesson baseline sits above the interest baseline
# deliberately. Half-life retuned 45 -> 14, same day it shipped: 45
# transplanted a human weeks-to-months trauma timescale into a daily-use
# system with nightly dreams, where a lesson that stays clean for a week of
# daily sessions is corrected behavior. Two weeks is the decisive-week
# doubled for margin: a lesson opens at 6.0 and holds 5.25 at one half-life,
# still above a fresh interest.
LESSON_BASELINE = 4.5
LESSON_HALFLIFE_DAYS = 14.0

# Charge stamping: the validator floors the weight of kind=lesson adds from the
# severity the corrections pre-pass observed in the consolidated sessions, so
# arousal at encoding time sets consolidation strength deterministically rather
# than relying on the headless model's judgment. charged/interrupt hits are a
# different event than a mild dissatisfaction or directive.
LESSON_FLOOR_CHARGED = 1.5
LESSON_FLOOR_MILD = 0.5

# Refractory period: the dream may not close, decay, or merge away a lesson
# younger than this many days. The system that caused the injury does not get
# to declare it healed a week later.
LESSON_REFRACTORY_DAYS = 30

# Lesson lifecycle. Re-offense re-arming: each repeat offense bumps the lesson's
# weight (reconsolidation: re-triggered trauma strengthens), with a ceiling
# keeping the max weight contribution (coefficient 1.0) below pin's +5.0 so no
# pile of offenses outranks the pinned floor. Fold threshold: a lesson add
# scoring at/above this cosine against an existing OPEN lesson is folded into
# it (re-arm) instead of filed as a sibling row, so the offense counter does
# not depend on the model reusing dedup_key. Retirement: a lesson untouched
# for ~2 half-lives closes as clean conduct. Retuned 90 -> 30 with the
# half-life: one month of reserved-seat reminding per incident, then out
# unless it re-offends. 30 coincides with the refractory boundary, so a lesson
# becomes retirable the moment its protection lifts; keep
# LESSON_RETIREMENT_DAYS >= LESSON_REFRACTORY_DAYS.
LESSON_REARM_WEIGHT = 0.5
LESSON_WEIGHT_CEILING = 3.0
LESSON_FOLD_THRESHOLD = 0.80
LESSON_RETIREMENT_DAYS = 30

# Cross-store dedup: a planned add whose title+body scores at or above this
# cosine against a stock-file vector is not filed; it is escalated in the
# Dream Journal as a merge-candidate naming the parent stock file. 0.80 is the
# starting point, untuned against live doc-to-doc cosines; observe diversions
# in the Journal and adjust like the other weights.
CROSS_DEDUP_THRESHOLD = 0.80


def _default_sessions_dir():
    # Claude Code's own transcript layout: each project gets a directory
    # under ~/.claude/projects named after its working directory, with "/"
    # and "." both turned into "-". A default under the data dir instead
    # never receives a transcript, so the dream reads nothing forever.
    slug = str(Path.cwd()).replace("/", "-").replace(".", "-")
    return _HOME / ".claude" / "projects" / slug


# Raw session transcripts (.jsonl) the headless dream consolidates. Machine-
# specific, so resolved via HIPPO_SESSIONS_DIR or the env file; the default
# is the current project's own transcript directory, computed at import time
# (init writes it into the env file, so it stays fixed after that).
SESSIONS_DIR = _resolve("HIPPO_SESSIONS_DIR", _default_sessions_dir())

# Stock auto-memory directory: the topic-file store embedded as the 'stock'
# source. Machine-specific, so set via HIPPO_MEMORY_DIR or the env file; the
# repo default lives under the sessions directory.
MEMORY_DIR = _resolve("HIPPO_MEMORY_DIR", SESSIONS_DIR / "memory")

# Embedder identity, stamped on every vector; a model change forces re-embed.
EMBED_MODEL = _get("HIPPO_EMBED_MODEL", "onnx-community/gte-multilingual-base")

# Local model directory (config.json, onnx/model_quantized.onnx, tokenizer.json).
# Machine-specific, so resolved via HIPPO_MODEL_DIR or the env file.
EMBED_MODEL_DIR = _resolve("HIPPO_MODEL_DIR", _DATA / "models" / "gte-multilingual-base")

# Relevance/salience blend weights for ranked semantic search.
# Tuned against observed --source all usage: at salience 1.0 the factor below is a
# near-constant ~0.45 additive bonus on every hippo row (stock rows get 0), which
# swamps the ~0.25 cosine spread and buries stock content (a verbatim-match feedback
# file sank to rank ~68). 0.15 is the largest weight where every clearly-better
# semantic match still wins (4/4 stock-truth + 2/2 hippo-truth probes), leaving
# salience as a tiebreak only (max bonus ~0.068) that keeps a light live-thread
# edge over equal-relevance cold references without overturning a wide relevance gap.
BLEND = {"relevance": 1.0, "salience": 0.15}

# Soft scale for the salience boost: factor = s / (s + SALIENCE_SCALE), a bounded
# absolute transform in [0,1). Equal saliences map to equal factors, so salience
# cannot spuriously invert a relevance win (unlike min-max over the candidate set).
# This only bites in the mixed --source all pool (on-demand lookup); the autonomous
# SessionStart path is --source hippo, where it has little effect. Kept at 6.0: the
# cross-source crowding was tuned out via BLEND["salience"] above, the cleaner lever.
SALIENCE_SCALE = 6.0

# Hard ceiling on the salience bonus. The weight and scale above still allowed a
# bonus up to ~0.07 on hot rows, enough to overturn a wide relevance gap: a hippo
# paraphrase of a stock rule outranked the stock rule itself in check-recall. The
# store grows satellites of stock rules faster than the blend weight can be
# retuned, so the tiebreak intent is enforced directly: the bonus is capped at
# this epsilon, letting salience flip near-ties and nothing else. ~8% of the
# ~0.25 working cosine spread.
TIEBREAK_EPS = 0.02

# SessionStart working-set delivery. Hard caps bound the injected set so a
# pathological day cannot blow out session cost. Selection is adaptive only
# downward: it surfaces min(available, cap), so a quiet day injects less.
WS_OPEN_LOOP_CAP = 7    # tier 1 ceiling: open loops surfaced
# Standing-lessons ceiling: reserved seats, never compete with interests. Raised
# 3 -> 4: all three seats were held by an exact 8.00 tie, so a fresh floor-stamped
# lesson (opens at 7.50) could not enter the working set until an incumbent
# decayed. The fourth seat keeps a lane open for new scars while incumbents
# are hot.
WS_LESSON_CAP = 4
WS_RECENT_CAP = 10      # tier 2 ceiling: salience-ranked recent items surfaced
WS_STALE_DAYS = 2       # surface a "last dream N days ago" note when older than this

# Handoff notes to self. Body cap in characters, enforced at the store: the
# note is for the next instance, and the cap pushes against narrating.
# Section ceiling for the rule-selected fourth working-set tier; the expected
# count is the number of live threads with notes, usually one or two. What
# survives is a bug rail against a runaway write (a pasted transcript), not
# an editorial limit; length is the writer's judgment.
HANDOFF_BODY_MAX = 8000
WS_HANDOFF_CAP = 5

# Token truncation cap for the embedder (gte-multilingual-base supports 8192).
EMBED_MAX_TOKENS = 8192

# Recall smoke-test probe list (hippo check). Personal data (names the
# owner's feedback files and live threads), so resolved via
# HIPPO_RECALL_PROBES or the env file and kept out of the repo; the neutral
# default is empty.
RECALL_PROBES_PATH = _resolve("HIPPO_RECALL_PROBES", _DATA / "recall_probes.json")
