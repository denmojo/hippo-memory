import importlib
import os
import stat
import sys

import pytest


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Every test runs against its own data dir, env file, and HOME so no test
    can read or write the developer's live store or settings."""
    # Clear every HIPPO_* variable the developer's own shell might export
    # before setting the ones this fixture needs. Four names used to be
    # cleared by name; any other HIPPO_* path override (HIPPO_TOKEN_FILE,
    # HIPPO_JOURNAL, HIPPO_ROLLBACK, ...) still won over the tmp data dir
    # below and could read or write the developer's live files.
    for key in [k for k in os.environ if k.startswith("HIPPO_")]:
        monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HIPPO_ENV_FILE", str(tmp_path / "env"))
    monkeypatch.setenv("HIPPO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HIPPO_DB", str(tmp_path / "data" / "hippo.db"))
    monkeypatch.setenv("HIPPO_OWNER", "Test Owner")
    monkeypatch.setenv("HIPPO_SESSIONS_DIR", str(tmp_path / "sessions"))
    (tmp_path / "sessions").mkdir()
    # No test may depend on, or print, the developer's own shell credentials.
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    # A stand-in `hippo` executable on PATH, ahead of anything else there, so
    # install._hippo_bin() resolves the same way a pipx or venv install
    # would, rather than falling back to the test runner's own argv[0].
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "hippo"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    # config.py binds its path constants (DATA_DIR, DB_PATH, ...) as module-level
    # values at import time, resolved from the environment at that moment. The
    # env vars above are set on this fixture's monkeypatch context, so any
    # already-imported hippo_memory.config module must be reloaded now, or its
    # constants keep pointing at the developer's own $HOME from whenever it
    # first got imported.
    if "hippo_memory.config" in sys.modules:
        importlib.reload(sys.modules["hippo_memory.config"])
    yield tmp_path


class FakeEmbedder:
    """Deterministic 8-dim vectors from character counts; enough for ranking tests."""
    dim = 8
    model = "fake"

    def embed(self, texts):
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for ch in t.lower():
                v[ord(ch) % self.dim] += 1.0
            n = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / n for x in v])
        return out


@pytest.fixture
def fake_embedder():
    return FakeEmbedder()
