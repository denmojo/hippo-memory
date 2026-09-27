"""hippo init / doctor / uninstall: the env file, the store, the hook
registrations in ~/.claude/settings.json, the four command files, and the
nightly schedule."""
import getpass
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
from importlib import resources
from pathlib import Path

from hippo_memory import config, hooks

HIPPO_COMMANDS = ("hippo-remember.md", "hippo-recall.md", "hippo-handoff.md", "hippo-dream.md")


class SettingsError(Exception):
    """~/.claude/settings.json exists but is not valid JSON. Callers must
    refuse rather than merge/unmerge into it, so a hand-edited-into-corruption
    file is never silently overwritten."""


def _settings_path():
    return Path.home() / ".claude" / "settings.json"


def _claude_commands_dir():
    return Path.home() / ".claude" / "commands"


def _is_hippo(entry):
    # Matches on the basename before " hook <event>", not the full command
    # string, so an entry registered under one PATH (or one install location)
    # is still recognized, and replaced rather than duplicated, after the
    # PATH changes or the package moves.
    for h in entry.get("hooks", []):
        command, _, rest = str(h.get("command", "")).partition(" hook ")
        if rest and os.path.basename(command) == "hippo":
            return True
    return False


def merge_hooks(settings):
    hippo = _hippo_bin()
    hk = settings.setdefault("hooks", {})
    for event, entries in hooks.REGISTRATIONS.items():
        existing = [e for e in hk.get(event, []) if not _is_hippo(e)]
        fresh = json.loads(json.dumps(entries))
        for entry in fresh:
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                if cmd.startswith("hippo "):
                    h["command"] = hippo + cmd[len("hippo"):]
        hk[event] = existing + fresh
    return settings


def unmerge_hooks(settings):
    hk = settings.get("hooks")
    if not hk:
        return settings
    for event in list(hk):
        kept = [e for e in hk[event] if not _is_hippo(e)]
        if kept:
            hk[event] = kept
        else:
            del hk[event]
    if not hk:
        del settings["hooks"]
    return settings


def _read_settings():
    p = _settings_path()
    if not p.exists():
        return {}
    text = p.read_text()
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise SettingsError(f"{p} is not valid JSON ({e}); leaving it untouched") from e


def _write_settings(settings):
    """Merge/unmerge write: preserves the caller's dict verbatim aside from the
    hippo hook entries, and writes it atomically (temp file + rename) with a
    backup of whatever was there before, so a crash mid-write can never leave
    settings.json half-written or the prior version unrecoverable.

    Writes to the path a symlink resolves to, rather than replacing the
    symlink itself, so a dotfiles-managed settings.json stays linked. Carries
    the prior file's mode onto the new one, so an `env` block with secrets in
    it does not go from 600 to whatever the umask gives a freshly created
    file. A one-time settings.json.hippo-preinit copy of the file as found
    before hippo ever wrote to it is kept alongside the rotating .bak, and no
    later write here overwrites it, so the content from before init survives
    an uninstall too.
    """
    p = _settings_path()
    target = p.resolve() if p.exists() else p
    target.parent.mkdir(parents=True, exist_ok=True)
    preinit = Path(str(target) + ".hippo-preinit")
    if target.exists() and not preinit.exists():
        shutil.copy2(target, preinit)
    text = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    mode = target.stat().st_mode if target.exists() else None
    if target.exists():
        if target.read_text() == text:
            return
        shutil.copy2(target, Path(str(target) + ".bak"))
    tmp = Path(str(target) + f".tmp.{os.getpid()}")
    tmp.write_text(text)
    if mode is not None:
        os.chmod(tmp, stat.S_IMODE(mode))
    os.replace(tmp, target)


def _write_secret(path, text):
    """Write a file that may hold a credential at 0600 from the moment it
    exists, rather than write-then-chmod, which leaves the content readable
    at the umask's mode for the interval between the two calls."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)
    os.chmod(path, 0o600)  # in case the file already existed at a wider mode


def write_env(values, path=None):
    path = Path(path or config.ENV_FILE)
    lines = [f"{k}={v}" for k, v in values.items() if v not in (None, "")]
    _write_secret(path, "# hippo-memory settings (KEY=VALUE). Environment variables override these.\n"
                        + "\n".join(lines) + "\n")


def render_template(name, hour, hippo):
    text = resources.files("hippo_memory.templates.scheduler").joinpath(name).read_text()
    h, m = hour.split(":")
    return (text.replace("__HIPPO__", hippo).replace("__HOUR__", str(int(h)) if name.endswith(".plist") else h)
                .replace("__MINUTE__", str(int(m)) if name.endswith(".plist") else m)
                .replace("__HOME__", str(Path.home())).replace("__PATH__", os.environ.get("PATH", "")))


def _hippo_bin():
    # Resolved to an absolute path even on the sys.argv[0] fallback, so a
    # relative invocation (`./hippo`, `python -m hippo_memory.cli`) never
    # bakes a cwd-relative path into the plist/unit file.
    return shutil.which("hippo") or str(Path(sys.argv[0]).resolve())


def install_schedule(hour, runner=subprocess.run):
    hippo = _hippo_bin()
    if platform.system() == "Darwin":
        dest = Path.home() / "Library" / "LaunchAgents" / "com.hippo-memory.dream.plist"
        dest.parent.mkdir(parents=True, exist_ok=True)
        (Path.home() / "Library" / "Logs").mkdir(parents=True, exist_ok=True)
        dest.write_text(render_template("com.hippo-memory.dream.plist", hour, hippo))
        runner(["launchctl", "unload", str(dest)], capture_output=True)
        runner(["launchctl", "load", "-w", str(dest)], check=False)
        return "launchd"
    if shutil.which("systemctl"):
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / "hippo-memory-dream.service").write_text(
            render_template("hippo-memory-dream.service", hour, hippo))
        (unit_dir / "hippo-memory-dream.timer").write_text(
            render_template("hippo-memory-dream.timer", hour, hippo))
        runner(["systemctl", "--user", "daemon-reload"], check=False)
        runner(["systemctl", "--user", "enable", "--now", "hippo-memory-dream.timer"], check=False)
        return "systemd"
    h, m = hour.split(":")
    print(f"No launchd or systemd found. Add this line with `crontab -e`:\n"
          f"{int(m)} {int(h)} * * * {hippo} dream >> {config.DATA_DIR}/dream.log 2>&1")
    return "cron"


def remove_schedule(runner=subprocess.run):
    if platform.system() == "Darwin":
        dest = Path.home() / "Library" / "LaunchAgents" / "com.hippo-memory.dream.plist"
        if dest.exists():
            runner(["launchctl", "unload", str(dest)], capture_output=True)
            dest.unlink()
    elif shutil.which("systemctl"):
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        service = unit_dir / "hippo-memory-dream.service"
        timer = unit_dir / "hippo-memory-dream.timer"
        # Only shell out when we installed a unit/timer; otherwise a
        # bare `hippo uninstall` on a host that merely has systemctl on PATH
        # would make a live, unconditional systemctl call for a schedule that
        # was never there.
        if service.exists() or timer.exists():
            runner(["systemctl", "--user", "disable", "--now", "hippo-memory-dream.timer"],
                   capture_output=True)
        for p in (service, timer):
            if p.exists():
                p.unlink()


def _ask(prompt, default, non_interactive):
    if non_interactive:
        return default
    ans = input(f"{prompt} [{default}]: ").strip()
    return ans or default


def _valid_auth(mode):
    return mode in ("setup-token", "api-key")


def _valid_hour(hhmm):
    try:
        h, m = str(hhmm).split(":")
        return len(h) and len(m) and 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except ValueError:
        return False


def _ask_valid(prompt, default, ni, explicit, valid_fn, complaint):
    """Prompt (or take the explicit CLI value) and keep re-asking, once per
    round, until it passes valid_fn. Non-interactive has no one to re-ask, so
    an invalid explicit value is refused rather than written to the env file
    and left to fail silently at the next dream run."""
    value = explicit or _ask(prompt, default, ni)
    while not valid_fn(value):
        print(complaint(value))
        if ni:
            return None
        value = _ask(prompt, default, ni)
    return value


def _default_sessions_dir():
    return str(config.SESSIONS_DIR)


def _resolve_sessions_dir(explicit, default, ni):
    prompt = "Claude Code project transcript directory to watch"
    sessions = explicit or _ask(prompt, default, ni)
    if not Path(sessions).is_dir():
        print(f"warning: {sessions} does not exist yet; the dream will find no "
              f"session activity to read until it does.")
        if not ni:
            sessions = _ask(f"{prompt} (the previous choice does not exist)", default, ni) or sessions
    return sessions


def cmd_init(args):
    ni = bool(getattr(args, "non_interactive", False))
    # Validate settings.json before touching anything else: a hand-edited file
    # that no longer parses must stop init cold, not get overwritten by the merge.
    try:
        current_settings = _read_settings()
    except SettingsError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    owner = args.owner or _ask("Your name, as the dream should refer to you", config.OWNER, ni)
    sessions = _resolve_sessions_dir(args.sessions_dir, _default_sessions_dir(), ni)
    auth = _ask_valid("Auth mode for the nightly dream (setup-token or api-key)", config.AUTH_MODE, ni,
                       args.auth, _valid_auth,
                       lambda v: f"'{v}' is not setup-token or api-key.")
    if auth is None:
        print("refused: --auth must be setup-token or api-key", file=sys.stderr)
        return 1
    model = args.model or _ask("Model id for the dream", config.MODEL or "sonnet", ni)
    hour = _ask_valid("Dream time, HH:MM", config.DREAM_HOUR, ni,
                       args.dream_hour, _valid_hour,
                       lambda v: f"'{v}' is not a valid 24-hour HH:MM time.")
    if hour is None:
        print("refused: --dream-hour must be a valid 24-hour HH:MM time", file=sys.stderr)
        return 1
    write_env({"HIPPO_OWNER": owner, "HIPPO_SESSIONS_DIR": sessions, "HIPPO_AUTH": auth,
               "HIPPO_MODEL": model, "HIPPO_DREAM_HOUR": hour})
    import importlib
    importlib.reload(config)
    from hippo_memory import cli
    cli.ensure_store()
    # A marker `purge` can trust even when HIPPO_DB points the store itself
    # somewhere else, so a bad HIPPO_DATA_DIR (e.g. left pointing at $HOME by
    # a stale override) is refused rather than rmtree'd.
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (config.DATA_DIR / ".hippo-data-dir").touch()
    _write_settings(merge_hooks(current_settings))
    cdir = _claude_commands_dir()
    cdir.mkdir(parents=True, exist_ok=True)
    for name in HIPPO_COMMANDS:
        text = resources.files("hippo_memory.templates.commands").joinpath(name).read_text()
        dest = cdir / name
        if not dest.exists() or dest.read_text() != text:
            dest.write_text(text)
    if auth == "setup-token" and not config.TOKEN_PATH.exists() and not ni:
        print("Mint a long-lived token with `claude setup-token` and paste it here.")
        tok = getpass.getpass("Token (leave empty to do this later): ").strip()
        if tok:
            _write_secret(config.TOKEN_PATH, tok + "\n")
    if getattr(args, "download_model", False):
        from hippo_memory import embed_onnx
        embed_onnx.download(config.EMBED_MODEL_DIR)
    if not getattr(args, "no_schedule", False):
        mech = install_schedule(hour)
        print(f"schedule installed via {mech} at {hour}")
    print(f"hippo-memory configured for {owner}. Run `hippo doctor` to verify.")
    return 0


def cmd_doctor(args):
    checks = []
    checks.append(("hippo on PATH", bool(shutil.which("hippo"))))
    checks.append(("env file", config.ENV_FILE.exists()))
    checks.append(("store", config.DB_PATH.exists()))
    try:
        s = _read_settings().get("hooks", {})
        hook_ok = all(any(_is_hippo(e) for e in s.get(ev, [])) for ev in hooks.REGISTRATIONS)
    except SettingsError:
        hook_ok = False
    checks.append(("hook registrations", hook_ok))
    checks.append(("command files", all((_claude_commands_dir() / n).exists() for n in HIPPO_COMMANDS)))
    if config.AUTH_MODE == "setup-token":
        checks.append(("setup-token file", config.TOKEN_PATH.exists()))
    else:
        checks.append(("ANTHROPIC_API_KEY set", bool(os.environ.get("ANTHROPIC_API_KEY"))))
    checks.append(("claude CLI", bool(shutil.which("claude"))))
    checks.append(("model id set", bool(config.MODEL)))
    checks.append(("watched transcripts dir", Path(config.SESSIONS_DIR).is_dir()))
    checks.append(("embedding model (optional)", (config.EMBED_MODEL_DIR / "onnx").exists()))
    if platform.system() == "Darwin":
        checks.append(("launchd entry", (Path.home() / "Library/LaunchAgents/com.hippo-memory.dream.plist").exists()))
    elif shutil.which("systemctl"):
        checks.append(("systemd timer", (Path.home() / ".config/systemd/user/hippo-memory-dream.timer").exists()))
    checks.append(("no dream failure sentinel", not config.DREAM_FAILURE_PATH.exists()))
    bad = 0
    for name, ok in checks:
        optional = name.endswith("(optional)")
        status = "ok  " if ok else ("skip" if optional else "FAIL")
        print(f"{status} {name}")
        bad += 0 if ok or optional else 1
    return 1 if bad else 0


def _safe_to_purge(path):
    """A directory is only rmtree'd on --purge when it plainly is a
    hippo-memory data dir: it holds the store or the marker cmd_init leaves,
    and it is not $HOME (a stale or hand-edited HIPPO_DATA_DIR pointed there
    would otherwise wipe the account)."""
    home = Path.home()
    if path == home or path == Path(path.anchor):
        return False
    return (path / "hippo.db").exists() or (path / ".hippo-data-dir").exists()


def cmd_uninstall(args):
    try:
        current_settings = _read_settings()
    except SettingsError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    _write_settings(unmerge_hooks(current_settings))
    for name in HIPPO_COMMANDS:
        p = _claude_commands_dir() / name
        if p.exists():
            p.unlink()
    remove_schedule()
    purge = getattr(args, "purge", False)
    purged = False
    if purge:
        if not config.DATA_DIR.exists():
            purged = True
        elif _safe_to_purge(config.DATA_DIR):
            shutil.rmtree(config.DATA_DIR)
            purged = True
        else:
            print(f"refused to purge {config.DATA_DIR}: no hippo.db or .hippo-data-dir "
                  f"marker there, or it resolves to $HOME", file=sys.stderr)
        if config.ENV_FILE.exists():
            config.ENV_FILE.unlink()
    print("hippo-memory hooks, commands, and schedule removed"
          + (" and data purged" if purged else "; data kept"))
    return 0
