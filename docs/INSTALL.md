# Installing hippo-memory (procedure for an assistant)

Follow these steps in order on the user's machine. Stop and report at the
first failure.

1. Confirm prerequisites: `python3 --version` is 3.11 or newer; `pipx --version`
   works (else `python3 -m pip install --user pipx && python3 -m pipx ensurepath`);
   `claude --version` works.
2. Install: `pipx install "git+https://github.com/denmojo/hippo-memory"`.
   For semantic recall, add the extra instead:
   `pipx install "hippo-memory[recall] @ git+https://github.com/denmojo/hippo-memory"`.
3. Ask the user three things and run `init` with them, non-interactively:
   their name; auth mode (`setup-token` for a Claude subscription, `api-key`
   for an API key); model id (`sonnet` is a sound default). Leave
   `--sessions-dir` out unless the user wants a project other than the one
   this install is running in watched; init derives it on its own from the
   current working directory (`~/.claude/projects/<cwd with "/" and "."
   turned into "-">`) and warns if that directory does not exist yet.
   `hippo init --non-interactive --owner "<name>" --auth <mode> --model <id> --dream-hour 02:30`
4. If auth is `setup-token`: this credential must never pass through this
   chat. Tell the user to open their own terminal and run `hippo init` once
   more, with no flags. It re-asks the questions from step 3 (press Enter at
   each to keep the answer already on file) and, once it reaches the token
   step, prompts with a hidden `getpass` line rather than an argument or a
   command this assistant would see. The user pastes the token there and it
   is written straight to `~/.local/share/hippo-memory/.oauth-token` at mode
   600; this assistant plays no part in that step and must not ask the user
   to paste the token here, run `claude setup-token` on their behalf, or
   write the token to a file itself. If auth is `api-key`: make sure
   `ANTHROPIC_API_KEY` is exported in the environment the scheduler will see
   (a login shell profile for cron, or the launchd/systemd unit's own
   environment).
5. Optional: `hippo init --download-model --non-interactive` to fetch the
   embedding model for semantic recall.
6. Verify: `hippo doctor`. Every line must read `ok` except the optional
   embedding-model line.
7. Prove the dream: `hippo dream --dry-run`. It prints either
   `dream: no new session activity` or `DRY RUN.` followed by the proposed
   plan as JSON. Either is success; a Python traceback or a non-zero exit is
   not.
8. Tell the user: the working set appears at the top of their next Claude
   Code session; `/hippo-handoff` writes a note before a long session
   compacts; the dream runs nightly at the hour chosen (`hippo doctor` shows
   the installed schedule entry for the platform).
