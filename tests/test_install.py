import argparse
import json
import os
from pathlib import Path

from hippo_memory import config, hooks, install


def _args(**kw):
    base = dict(owner="Alex", sessions_dir=None, model="sonnet", dream_hour="03:15",
                auth="setup-token", download_model=False, no_schedule=True, non_interactive=True)
    base.update(kw)
    return argparse.Namespace(**base)


def test_merge_hooks_is_idempotent_and_preserves_others():
    settings = {"model": "x", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "echo mine"}]}]}}
    once = install.merge_hooks(json.loads(json.dumps(settings)))
    twice = install.merge_hooks(json.loads(json.dumps(once)))
    assert once == twice
    assert once["model"] == "x"
    assert once["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "echo mine"
    assert any(h["command"].endswith(" hook prompt")
               for e in once["hooks"]["UserPromptSubmit"] for h in e["hooks"])


def test_merge_hooks_registers_absolute_hippo_path(monkeypatch):
    monkeypatch.setattr(install, "_hippo_bin", lambda: "/opt/hippo/bin/hippo")
    once = install.merge_hooks({})
    cmd = once["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert cmd == "/opt/hippo/bin/hippo hook prompt"


def test_merge_hooks_stays_idempotent_after_a_path_change(monkeypatch):
    # A PATH change (or a reinstall to a new prefix) between two `hippo init`
    # runs must replace the old registration, not duplicate it: _is_hippo
    # matches on the basename before " hook <event>", not the exact command.
    monkeypatch.setattr(install, "_hippo_bin", lambda: "/old/bin/hippo")
    settings = install.merge_hooks({})
    monkeypatch.setattr(install, "_hippo_bin", lambda: "/new/bin/hippo")
    settings = install.merge_hooks(settings)
    entries = settings["hooks"]["UserPromptSubmit"]
    assert len(entries) == 1
    assert entries[0]["hooks"][0]["command"] == "/new/bin/hippo hook prompt"


def test_unmerge_removes_only_hippo_entries():
    settings = {"hooks": {"UserPromptSubmit": [
        {"hooks": [{"type": "command", "command": "echo theirs"}]},
        {"hooks": [{"type": "command", "command": "hippo hook prompt"}]}]}}
    out = install.unmerge_hooks(install.merge_hooks(settings))
    assert out["hooks"]["UserPromptSubmit"] == [{"hooks": [{"type": "command", "command": "echo theirs"}]}]
    assert "SessionStart" not in out["hooks"]


def test_init_writes_env_store_settings_and_commands(tmp_path):
    sessions = tmp_path / "proj"; sessions.mkdir()
    rc = install.cmd_init(_args(sessions_dir=str(sessions)))
    assert rc == 0
    env = Path(config.ENV_FILE).read_text()
    assert "HIPPO_OWNER=Alex" in env and f"HIPPO_SESSIONS_DIR={sessions}" in env
    assert config.DB_PATH.exists()
    settings = json.loads((Path.home() / ".claude" / "settings.json").read_text())
    assert "SessionStart" in settings["hooks"]
    cmds = Path.home() / ".claude" / "commands"
    assert sorted(p.name for p in cmds.iterdir()) == [
        "hippo-dream.md", "hippo-handoff.md", "hippo-recall.md", "hippo-remember.md"]
    before = (Path.home() / ".claude" / "settings.json").read_bytes()
    install.cmd_init(_args(sessions_dir=str(sessions)))
    assert (Path.home() / ".claude" / "settings.json").read_bytes() == before


def test_uninstall_restores_settings_and_keeps_data(tmp_path):
    sessions = tmp_path / "proj"; sessions.mkdir()
    sp = Path.home() / ".claude" / "settings.json"
    sp.parent.mkdir(parents=True)
    sp.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}, indent=2) + "\n")
    install.cmd_init(_args(sessions_dir=str(sessions)))
    (Path.home() / ".claude" / "commands" / "mine.md").write_text("keep")
    rc = install.cmd_uninstall(argparse.Namespace(purge=False))
    assert rc == 0
    after = json.loads(sp.read_text())
    assert after["permissions"] == {"allow": ["Bash(ls)"]} and "hooks" not in after
    assert (Path.home() / ".claude" / "commands" / "mine.md").exists()
    assert not (Path.home() / ".claude" / "commands" / "hippo-recall.md").exists()
    assert config.DB_PATH.exists()


# --- fix wave 2: --purge only rmtrees a directory it can confirm is a
# hippo-memory data dir, and also removes the env file. ---------------------

def test_purge_removes_data_dir_and_env_file(tmp_path):
    sessions = tmp_path / "proj"; sessions.mkdir()
    install.cmd_init(_args(sessions_dir=str(sessions)))
    assert config.DATA_DIR.exists() and config.ENV_FILE.exists()
    rc = install.cmd_uninstall(argparse.Namespace(purge=True))
    assert rc == 0
    assert not config.DATA_DIR.exists()
    assert not config.ENV_FILE.exists()


def test_purge_refuses_a_data_dir_with_no_marker(tmp_path, monkeypatch):
    decoy = tmp_path / "unrelated"
    decoy.mkdir()
    (decoy / "some-other-file").write_text("not hippo's")
    monkeypatch.setattr(config, "DATA_DIR", decoy)
    rc = install.cmd_uninstall(argparse.Namespace(purge=True))
    assert rc == 0
    assert decoy.exists()
    assert (decoy / "some-other-file").exists()


def test_purge_refuses_when_data_dir_is_home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", Path.home())
    rc = install.cmd_uninstall(argparse.Namespace(purge=True))
    assert rc == 0
    assert Path.home().exists()


def test_init_on_missing_settings_creates_it(tmp_path):
    sessions = tmp_path / "proj"; sessions.mkdir()
    sp = Path.home() / ".claude" / "settings.json"
    assert not sp.exists()
    rc = install.cmd_init(_args(sessions_dir=str(sessions)))
    assert rc == 0
    assert sp.exists()
    assert "SessionStart" in json.loads(sp.read_text())["hooks"]


def test_init_refuses_on_unparseable_settings_and_leaves_it_untouched(tmp_path):
    sessions = tmp_path / "proj"; sessions.mkdir()
    sp = Path.home() / ".claude" / "settings.json"
    sp.parent.mkdir(parents=True)
    bad = "{not valid json"
    sp.write_text(bad)
    rc = install.cmd_init(_args(sessions_dir=str(sessions)))
    assert rc != 0
    assert sp.read_text() == bad
    assert not config.ENV_FILE.exists()


def test_uninstall_refuses_on_unparseable_settings_and_leaves_it_untouched(tmp_path):
    sessions = tmp_path / "proj"; sessions.mkdir()
    install.cmd_init(_args(sessions_dir=str(sessions)))
    sp = Path.home() / ".claude" / "settings.json"
    bad = "{not valid json"
    sp.write_text(bad)
    rc = install.cmd_uninstall(argparse.Namespace(purge=False))
    assert rc != 0
    assert sp.read_text() == bad


def test_write_settings_is_atomic_and_backs_up_prior_version(tmp_path):
    sp = Path.home() / ".claude" / "settings.json"
    sp.parent.mkdir(parents=True)
    sp.write_text(json.dumps({"model": "x"}) + "\n")
    install._write_settings(install.merge_hooks(install._read_settings()))
    backup = Path(str(sp) + ".bak")
    assert backup.exists()
    assert json.loads(backup.read_text()) == {"model": "x"}
    tmp_leftovers = list(sp.parent.glob("settings.json.tmp.*"))
    assert tmp_leftovers == []


def test_write_settings_follows_symlink_to_dotfiles_target(tmp_path):
    # A dotfiles manager often links ~/.claude/settings.json to a file it
    # tracks elsewhere. The write must reach that target, not replace the
    # link with a plain file and detach it from the dotfiles repo.
    target = tmp_path / "dotfiles" / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"model": "x"}) + "\n")
    sp = Path.home() / ".claude" / "settings.json"
    sp.parent.mkdir(parents=True)
    sp.symlink_to(target)
    install._write_settings(install.merge_hooks(install._read_settings()))
    assert sp.is_symlink()
    assert sp.resolve() == target
    assert "hooks" in json.loads(target.read_text())


def test_write_settings_preserves_prior_file_mode(tmp_path):
    sp = Path.home() / ".claude" / "settings.json"
    sp.parent.mkdir(parents=True)
    sp.write_text(json.dumps({"model": "x"}) + "\n")
    os.chmod(sp, 0o600)
    install._write_settings(install.merge_hooks(install._read_settings()))
    assert sp.stat().st_mode & 0o777 == 0o600


def test_write_settings_keeps_non_ascii_unescaped(tmp_path):
    sp = Path.home() / ".claude" / "settings.json"
    sp.parent.mkdir(parents=True)
    sp.write_text(json.dumps({"note": "a → b"}, ensure_ascii=False) + "\n")
    install._write_settings(install.merge_hooks(install._read_settings()))
    text = sp.read_text()
    assert "→" in text
    assert "\\u2192" not in text


def test_preinit_backup_survives_init_then_uninstall(tmp_path):
    sessions = tmp_path / "proj"; sessions.mkdir()
    sp = Path.home() / ".claude" / "settings.json"
    sp.parent.mkdir(parents=True)
    original = json.dumps({"permissions": {"allow": ["Bash(ls)"]}}, indent=2) + "\n"
    sp.write_text(original)
    install.cmd_init(_args(sessions_dir=str(sessions)))
    preinit = Path(str(sp) + ".hippo-preinit")
    assert preinit.exists()
    assert preinit.read_text() == original
    install.cmd_uninstall(argparse.Namespace(purge=False))
    # uninstall's own write must not overwrite the preinit backup with the
    # post-init, hooks-merged state it is about to replace.
    assert preinit.read_text() == original


def test_env_file_written_with_owner_only_mode(tmp_path):
    sessions = tmp_path / "proj"; sessions.mkdir()
    install.cmd_init(_args(sessions_dir=str(sessions)))
    mode = Path(config.ENV_FILE).stat().st_mode & 0o777
    assert mode == 0o600


# --- fix wave 2: a non-existent sessions dir gets a warning, and an
# interactive run gets one re-ask; a bad auth mode or dream hour is refused
# in non-interactive mode rather than written to the env file. -------------

def test_init_warns_on_missing_sessions_dir_non_interactive(tmp_path, capsys):
    missing = tmp_path / "does-not-exist"
    rc = install.cmd_init(_args(sessions_dir=str(missing)))
    assert rc == 0
    assert "does not exist" in capsys.readouterr().out
    assert f"HIPPO_SESSIONS_DIR={missing}" in Path(config.ENV_FILE).read_text()


def test_init_reasks_once_on_missing_sessions_dir_interactive(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "does-not-exist"
    good = tmp_path / "proj"; good.mkdir()
    answers = iter([str(missing), str(good)])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(install.getpass, "getpass", lambda prompt="": "")
    rc = install.cmd_init(_args(sessions_dir=None, non_interactive=False))
    assert rc == 0
    assert f"HIPPO_SESSIONS_DIR={good}" in Path(config.ENV_FILE).read_text()


def test_init_refuses_bad_auth_mode_non_interactive(tmp_path, capsys):
    sessions = tmp_path / "proj"; sessions.mkdir()
    rc = install.cmd_init(_args(sessions_dir=str(sessions), auth="carrier-pigeon"))
    assert rc != 0
    assert not config.ENV_FILE.exists()


def test_init_refuses_bad_dream_hour_non_interactive(tmp_path, capsys):
    sessions = tmp_path / "proj"; sessions.mkdir()
    rc = install.cmd_init(_args(sessions_dir=str(sessions), dream_hour="25:00"))
    assert rc != 0
    assert not config.ENV_FILE.exists()


def test_init_reasks_on_bad_dream_hour_interactive(tmp_path, monkeypatch):
    sessions = tmp_path / "proj"; sessions.mkdir()
    answers = iter(["Alex", "25:00", "04:05"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(install.getpass, "getpass", lambda prompt="": "")
    rc = install.cmd_init(_args(sessions_dir=str(sessions), owner=None, dream_hour=None,
                                non_interactive=False))
    assert rc == 0
    assert "HIPPO_DREAM_HOUR=04:05" in Path(config.ENV_FILE).read_text()


def test_schedule_templates_render_hour_and_home():
    plist = install.render_template("com.hippo-memory.dream.plist", hour="04:05", hippo="/usr/local/bin/hippo")
    assert "<integer>4</integer>" in plist and "<integer>5</integer>" in plist and "/usr/local/bin/hippo" in plist
    timer = install.render_template("hippo-memory-dream.timer", hour="04:05", hippo="/usr/local/bin/hippo")
    assert "OnCalendar=*-*-* 04:05:00" in timer


# --- fix wave 2: install_schedule, fake-runner only, on both platform
# branches - rendered file contents and the launchctl/systemctl argv. -------

def test_install_schedule_darwin_writes_plist_and_loads_it(monkeypatch):
    monkeypatch.setattr(install.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(install, "_hippo_bin", lambda: "/opt/hippo/bin/hippo")
    calls = []
    mech = install.install_schedule("04:05", runner=lambda *a, **k: calls.append((a, k)))
    assert mech == "launchd"
    dest = Path.home() / "Library" / "LaunchAgents" / "com.hippo-memory.dream.plist"
    text = dest.read_text()
    assert "/opt/hippo/bin/hippo" in text
    assert "<integer>4</integer>" in text and "<integer>5</integer>" in text
    assert [c[0][0] for c in calls] == [
        ["launchctl", "unload", str(dest)],
        ["launchctl", "load", "-w", str(dest)],
    ]


def test_install_schedule_linux_writes_unit_and_timer_and_enables_it(monkeypatch):
    monkeypatch.setattr(install.platform, "system", lambda: "Linux")
    monkeypatch.setattr(install.shutil, "which",
                        lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.setattr(install, "_hippo_bin", lambda: "/opt/hippo/bin/hippo")
    calls = []
    mech = install.install_schedule("04:05", runner=lambda *a, **k: calls.append((a, k)))
    assert mech == "systemd"
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    service = (unit_dir / "hippo-memory-dream.service").read_text()
    timer = (unit_dir / "hippo-memory-dream.timer").read_text()
    assert "/opt/hippo/bin/hippo" in service
    assert "OnCalendar=*-*-* 04:05:00" in timer
    assert [c[0][0] for c in calls] == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "hippo-memory-dream.timer"],
    ]


def test_install_schedule_falls_back_to_cron_line(monkeypatch, capsys):
    monkeypatch.setattr(install.platform, "system", lambda: "Linux")
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    monkeypatch.setattr(install, "_hippo_bin", lambda: "/opt/hippo/bin/hippo")
    calls = []
    mech = install.install_schedule("04:05", runner=lambda *a, **k: calls.append((a, k)))
    assert mech == "cron"
    assert calls == []
    assert "crontab -e" in capsys.readouterr().out


# --- fix round 1: remove_schedule must never shell out for a schedule that
# was never installed, on either platform, and the runner must be injectable
# so no test needs to invoke launchctl/systemctl. -----------------------------

def test_remove_schedule_darwin_no_op_without_plist(monkeypatch):
    monkeypatch.setattr(install.platform, "system", lambda: "Darwin")
    calls = []
    install.remove_schedule(runner=lambda *a, **k: calls.append((a, k)))
    assert calls == []


def test_remove_schedule_darwin_unloads_existing_plist(monkeypatch):
    monkeypatch.setattr(install.platform, "system", lambda: "Darwin")
    dest = Path.home() / "Library" / "LaunchAgents" / "com.hippo-memory.dream.plist"
    dest.parent.mkdir(parents=True)
    dest.write_text("x")
    calls = []
    install.remove_schedule(runner=lambda *a, **k: calls.append((a, k)))
    assert len(calls) == 1
    assert calls[0][0][0] == ["launchctl", "unload", str(dest)]
    assert not dest.exists()


def test_remove_schedule_linux_no_op_without_timer(monkeypatch):
    monkeypatch.setattr(install.platform, "system", lambda: "Linux")
    monkeypatch.setattr(install.shutil, "which",
                        lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    calls = []
    install.remove_schedule(runner=lambda *a, **k: calls.append((a, k)))
    assert calls == []


def test_remove_schedule_linux_disables_existing_timer(monkeypatch):
    monkeypatch.setattr(install.platform, "system", lambda: "Linux")
    monkeypatch.setattr(install.shutil, "which",
                        lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "hippo-memory-dream.timer").write_text("x")
    calls = []
    install.remove_schedule(runner=lambda *a, **k: calls.append((a, k)))
    assert len(calls) == 1
    assert calls[0][0][0] == ["systemctl", "--user", "disable", "--now", "hippo-memory-dream.timer"]
    assert not (unit_dir / "hippo-memory-dream.timer").exists()


def test_uninstall_never_shells_out_when_no_schedule_was_installed(tmp_path, monkeypatch):
    # End-to-end: init with no_schedule=True, then uninstall on Linux with
    # systemctl on PATH must still make zero scheduler calls.
    sessions = tmp_path / "proj"; sessions.mkdir()
    install.cmd_init(_args(sessions_dir=str(sessions)))
    monkeypatch.setattr(install.platform, "system", lambda: "Linux")
    monkeypatch.setattr(install.shutil, "which",
                        lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    calls = []
    monkeypatch.setattr(install.subprocess, "run", lambda *a, **k: calls.append((a, k)))
    rc = install.cmd_uninstall(argparse.Namespace(purge=False))
    assert rc == 0
    assert calls == []


# --- fix round 1: the setup-token prompt must never echo the secret. -------

def test_setup_token_prompt_uses_getpass_and_is_never_echoed(tmp_path, monkeypatch, capsys):
    sessions = tmp_path / "proj"; sessions.mkdir()
    secret = "sekrit-oauth-token-value"
    monkeypatch.setattr(install.getpass, "getpass", lambda prompt="": secret)
    rc = install.cmd_init(_args(sessions_dir=str(sessions), non_interactive=False))
    assert rc == 0
    assert config.TOKEN_PATH.read_text() == secret + "\n"
    mode = config.TOKEN_PATH.stat().st_mode & 0o777
    assert mode == 0o600
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


# --- fix round 1: a relative sys.argv[0] fallback must resolve to absolute. -

def test_hippo_bin_resolves_relative_argv0_to_absolute(monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    monkeypatch.setattr(install.sys, "argv", ["./relative/hippo"])
    result = install._hippo_bin()
    assert Path(result).is_absolute()
    assert result.endswith("relative/hippo")


# --- fix wave 2: doctor prints "skip", not "FAIL", for an optional check. --

def test_doctor_prints_skip_not_fail_for_optional_check(capsys):
    rc = install.cmd_doctor(argparse.Namespace())
    out = capsys.readouterr().out
    assert "skip embedding model (optional)" in out
    assert "FAIL embedding model (optional)" not in out
    # An optional miss must not fail the overall exit code by itself; with
    # nothing else installed in this tmp HOME, cmd_doctor still returns
    # nonzero on the several non-optional misses, so check the line, not rc.
