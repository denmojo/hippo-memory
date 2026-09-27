import importlib
from pathlib import Path


def _reload(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from hippo_memory import config
    return importlib.reload(config)


def test_defaults_live_under_data_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("HIPPO_DB", raising=False)
    cfg = _reload(monkeypatch, HIPPO_DATA_DIR=str(tmp_path / "d"))
    assert cfg.DB_PATH == tmp_path / "d" / "hippo.db"
    assert cfg.JOURNAL_PATH == tmp_path / "d" / "Memory Dream Journal.md"
    assert cfg.COMPACTIONS_PATH == tmp_path / "d" / "compactions.jsonl"
    assert cfg.CONTEXT_WATCH_DIR == tmp_path / "d" / "context-watch"


def test_env_file_beats_default_and_env_var_beats_file(monkeypatch, tmp_path):
    envf = tmp_path / "env"
    envf.write_text("HIPPO_OWNER=File Owner\nHIPPO_MODEL=file-model\n")
    monkeypatch.setenv("HIPPO_ENV_FILE", str(envf))
    monkeypatch.setenv("HIPPO_MODEL", "env-model")
    monkeypatch.delenv("HIPPO_OWNER", raising=False)
    cfg = _reload(monkeypatch)
    assert cfg.OWNER == "File Owner"
    assert cfg.MODEL == "env-model"


def test_auth_and_hour_defaults(monkeypatch):
    monkeypatch.delenv("HIPPO_AUTH", raising=False)
    monkeypatch.delenv("HIPPO_DREAM_HOUR", raising=False)
    cfg = _reload(monkeypatch)
    assert cfg.AUTH_MODE == "setup-token"
    assert cfg.DREAM_HOUR == "02:30"
    assert cfg.DREAM_TIMEOUT == 600 and cfg.DREAM_RETRIES == 2


def test_sessions_dir_defaults_to_claude_projects_slug(monkeypatch, tmp_path):
    monkeypatch.delenv("HIPPO_SESSIONS_DIR", raising=False)
    project = tmp_path / "some" / "project"
    project.mkdir(parents=True)
    monkeypatch.chdir(project)
    cfg = _reload(monkeypatch)
    slug = str(project).replace("/", "-").replace(".", "-")
    assert cfg.SESSIONS_DIR == Path.home() / ".claude" / "projects" / slug


def test_no_owner_name_or_vault_path_in_source():
    import inspect
    from hippo_memory import config
    src = inspect.getsource(config)
    for needle in ("/Users/", "laurel", "vault-law", "ADR-"):
        assert needle not in src


def test_data_dir_binds_under_tmp_home_not_dev_home(tmp_path):
    """isolated_env's monkeypatch.setenv calls happen before this module is
    imported/reloaded in conftest, so config's module-level path constants
    must bind under the fixture's tmp HOME/data dir, never the developer's
    own $HOME."""
    from hippo_memory import config

    assert str(config.DATA_DIR).startswith(str(tmp_path))
    assert str(config.DATA_DIR) != str(Path.home() / ".local/share/hippo-memory")
