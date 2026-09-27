# hippo-memory

hippo-memory is an episodic memory store for Claude Code. It keeps a small
SQLite database beside Claude Code's own auto-memory, consolidates it every
night with a headless dream (a `claude -p` call that reads the day's new
session transcripts and saves the open threads, lessons, and interests it
finds there), and surfaces the result at the
start of your next session as a small, ranked working set: open loops,
standing lessons, and recent context.

hippo-memory is an independent project and is not affiliated with or
endorsed by Anthropic.

## Install

```
pipx install "git+https://github.com/denmojo/hippo-memory"
hippo init
hippo doctor
```

`hippo init` asks four things (each can also be passed as a flag, and
`--non-interactive` accepts the defaults for anything not given):

- your name, so the dream can refer to you by it (`--owner`, env `HIPPO_OWNER`)
- the Claude Code project transcript directory to watch (`--sessions-dir`, env
  `HIPPO_SESSIONS_DIR`; default is the current project's own transcript
  directory, `~/.claude/projects/<cwd with "/" and "." turned into "-">`,
  computed when `init` runs; it warns, and an interactive run re-asks once,
  if that directory does not exist yet)
- the auth mode for the nightly dream, `setup-token` or `api-key`
  (`--auth`, env `HIPPO_AUTH`; default `setup-token`)
- the model id the dream should use (`--model`, env `HIPPO_MODEL`)

and one more with its own default: the dream time, `HH:MM` (`--dream-hour`,
env `HIPPO_DREAM_HOUR`; default `02:30`).

With those answers, `hippo init`:

- writes `~/.config/hippo-memory/env` (mode 600) with the settings above
- creates the SQLite store at `~/.local/share/hippo-memory/hippo.db`
- merges its four hooks (`SessionStart`, `UserPromptSubmit`, `PreCompact`,
  `SessionEnd`, each running `hippo hook <event>`) into
  `~/.claude/settings.json`, leaving any other hooks in that file untouched
- writes four command files into `~/.claude/commands/`: `hippo-remember.md`,
  `hippo-recall.md`, `hippo-handoff.md`, `hippo-dream.md`
- if auth is `setup-token` and no token is on file yet and you're running
  interactively, prompts you to paste in a token from `claude setup-token`
  and saves it to `~/.local/share/hippo-memory/.oauth-token` (mode 600)
- with `--download-model`, fetches the optional local embedding model (see
  Recall below)
- unless `--no-schedule`, installs the nightly schedule for your platform
  (see The nightly dream below)

Run `hippo doctor` afterward to check the install: it reports one `ok`/`FAIL`
line per prerequisite (hippo on PATH, the env file, the store, the hook
registrations, the command files, the credential for your chosen auth mode,
the `claude` CLI, a model id, the watched transcripts directory, the
platform's schedule entry) and a `skip` line for the optional embedding
model when it is not installed. Exit status is 0 only if every required
line reads `ok`.

## What you get at session start

At the top of a new or resumed session, hippo-memory injects a working set
like this (ids, salience numbers, and titles here are placeholders):

```
# Hippocampus working-set (episodic carryover from prior sessions)
_Last dream: 2026-09-25 02:31._

## Open loops
- [42] 6.10 migrate the report exporter off the deprecated template engine
- [37] 5.40 finish the retry-backoff test for the sync client

## Standing lessons
- [12] 7.50 always confirm the target branch before a force-push

## Notes to self (claude-written, prior sessions)
- [58] 2026-09-24 21:03  exporter migration: schema mapped, tests not yet written  (carries 42)

## Recent context
- [51] 4.20 interest: the team's interest in switching CI runners
- [49] 3.95 episodic: fixed the flaky upload test

_Expand any line above with `hippo show <id>` (full body, one call)._
_The store also holds 6 older open thread(s); query `hippo list` to pull them._
```

Each tier has its own cap so a busy day cannot flood the block, and a quiet
day shows less than the cap rather than padding it out. A one-line greeting
("Last dream happened today at 02:31", or a failure notice if the last
attempt errored) is also set as the session's system message.

## Commands

Four slash commands are installed into `~/.claude/commands/` by `hippo init`
and are meant to be run from inside a session:

- `/hippo-remember` - boost the current topic's memory, or file a new one, so
  it rides into future sessions
- `/hippo-recall <query>` - semantic search over episodic memory, with a
  lexical fallback if the embedding model isn't installed
- `/hippo-handoff` - write a note to your next self before a long session
  compacts or ends
- `/hippo-dream` - run the dream now instead of waiting for the schedule

The rest of the CLI is `hippo <subcommand>`, run directly. The ones you'll
reach for by hand:

- `hippo recall "<query>"` - semantic lookup; prints the top match's full body
- `hippo search "<query>" [--k N] [--source all|hippo|stock] [--status open|closed]`
  - ranked search across both hippo's own rows and Claude Code's stock
    memory files
- `hippo handoff add --title "..." --body "..." [--carries ID,ID]` - write a
  note to self (also see `handoff list`, `handoff close ID`, `handoff latest`)
- `hippo boost ID [weight]` - raise a memory's weight so it surfaces sooner
  (default weight 1.0)
- `hippo list [--status open|closed] [--kind ...] [--since DATE] [--sort salience|recency|id] [--limit N]`
  - browse the store; `--limit 0` for everything
- `hippo show ID` - print one memory's full body and metadata
- `hippo dream [--days N] [--dry-run] [--print-material]` - run the dream by
  hand; `--dry-run` validates and prints the plan without applying it
- `hippo rollback` - restore the snapshot taken just before the last dream
  applied anything, undoing that run wholesale
- `hippo journal --date YYYY-MM-DD --line "..."` - append a line to the Dream
  Journal by hand

Run `hippo <subcommand> --help` for the full flag list of any of these.

## The nightly dream

`hippo init` installs a per-platform schedule that runs `hippo dream` once a
day at the chosen hour (default `02:30`):

- **macOS**: a `launchd` agent at
  `~/Library/LaunchAgents/com.hippo-memory.dream.plist`, loaded with
  `launchctl load -w`. Its output goes to
  `~/Library/Logs/hippo-memory-dream.log`.
- **Linux with systemd**: a user timer,
  `~/.config/systemd/user/hippo-memory-dream.timer`, and its service unit
  `hippo-memory-dream.service`, enabled with
  `systemctl --user enable --now`. Its output goes to the user journal
  (`journalctl --user -u hippo-memory-dream`).
- **Linux without systemd**: `hippo init` prints a `crontab -e` line instead
  of installing anything itself; add it by hand.

The dream authenticates to Claude Code one of two ways, set by `--auth` /
`HIPPO_AUTH` at init time:

- **`setup-token`** (default): the dream reads an OAuth token from
  `~/.local/share/hippo-memory/.oauth-token` and runs `claude -p` under your
  own Claude subscription, the same way an interactive session would.
  Anthropic's own documentation on this: "OAuth authentication is intended
  exclusively for purchasers of Claude Free, Pro, Max, Team, and Enterprise
  subscription plans and is designed to support ordinary use of Claude Code
  and other native Anthropic applications" (see
  <https://code.claude.com/docs/en/legal-and-compliance>). Mint the token
  yourself with `claude setup-token` (it opens a browser); the file is not
  written for you except during an interactive `hippo init`. To rotate it,
  run `claude setup-token` again and overwrite the file (mode 600) with the
  new value.
- **`api-key`**: the dream reads `ANTHROPIC_API_KEY` from the environment the
  scheduler runs in and bills the dream's usage to that key.

Other dream settings, all env-file keys with matching environment variables:
`HIPPO_MODEL` (passed as `--model` to `claude -p` when set; otherwise the
`claude` CLI's own default applies), `HIPPO_DREAM_TIMEOUT` (seconds, default
600), `HIPPO_DREAM_RETRIES` (default 2; only transient failures such as rate
limits or overload are retried, never an auth failure).

If a scheduled run fails, it leaves a sentinel that the next session's
working set surfaces plainly ("Last dream FAILED..."); `hippo doctor` will
also flag credential and schedule problems. To check without waiting for the
schedule or changing the store, run `hippo dream --dry-run`: it gathers
the same material, calls Claude, validates the plan, and prints it without
writing anything to the store.

## Data and privacy

hippo-memory reads Claude Code's session transcripts (`.jsonl` files) from
the directory you pointed it at during `init`, plus its own SQLite store and
Claude Code's stock memory files, when building working sets and search
results. It writes everything it produces - the episodic store, the Dream
Journal, backups, offsets, sentinels - under `~/.local/share/hippo-memory/`
(overridable per path via `HIPPO_*` environment variables) and installs
hooks and command files under `~/.claude/`. Nothing leaves the machine
except the nightly dream's own call to Anthropic, made under your own
credentials (a subscription OAuth token or your own API key, per the auth
mode above) exactly as an interactive Claude Code session would. Semantic
search and indexing run against a locally cached embedding model and never
send text anywhere.

## Recall and the embedding model

Semantic search (`hippo recall`, `hippo search`, `hippo check`, and the
`/hippo-recall` command) needs the optional `recall` extra
(`pipx install "hippo-memory[recall] @ git+..."`) plus a local embedding
model. Fetch the model with `hippo init --download-model` (or later, run it
again) - it downloads `onnx-community/gte-multilingual-base` from Hugging
Face into `~/.local/share/hippo-memory/models/gte-multilingual-base/` and
then runs fully offline. Index the store with `hippo index` after the model
is present.

Without the extra or the model, only `hippo search` falls back to lexical
search, printing a note when it does. `hippo recall` and `hippo check` have
no lexical fallback: each exits with status 2 and a
`hippo-memory[recall]:` or `hippo-memory[check]:` message instead.

## Uninstall

```
hippo uninstall
```

Removes hippo-memory's hook entries from `~/.claude/settings.json`, deletes
the four command files, and removes the scheduled job (unloading the
launchd agent or disabling the systemd timer, whichever applies). The data
directory (`~/.local/share/hippo-memory/`, including the store and the Dream
Journal) is kept. Add `--purge` to delete it, and the env file, too; purge
refuses if the data directory does not look like one of hippo-memory's own
(no `hippo.db` or init marker in it) or if it resolves to `$HOME`.

## Platforms

macOS and Linux, including Linux under WSL, are supported; the scheduler
falls back from systemd to a printed `crontab` line where systemd isn't
available (this covers WSL1 and a WSL2 distribution without systemd
enabled). Windows is not supported.

## Development

```
pip install -e '.[dev]'
python3 -m pytest
```

Tests run fully offline: a fake embedder stands in for the ONNX model, and
every test runs against its own temporary `HOME` and data directory, never
yours.

## License

Apache-2.0. See `LICENSE`.
