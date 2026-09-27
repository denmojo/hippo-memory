import importlib

import pytest

from hippo_memory import config, embed, search, store


def test_get_embedder_raises_clean_error_when_model_missing(monkeypatch, tmp_path):
    """Exercises the failure path from two angles at once: the optional
    onnx/tokenizers/numpy deps aren't installed in this test environment (the
    `recall` extra was never added), and even if they were, HIPPO_MODEL_DIR
    below points at a directory with no model files. Either way get_embedder
    must translate the failure into EmbedderUnavailable, not let an
    ImportError or a raw onnxruntime/tokenizers exception escape."""
    monkeypatch.setenv("HIPPO_MODEL_DIR", str(tmp_path / "nomodel"))
    importlib.reload(config)
    with pytest.raises(embed.EmbedderUnavailable):
        embed.get_embedder(config.EMBED_MODEL)


def test_lexical_search_works_without_embedder(tmp_path):
    conn = store.connect(tmp_path / "s.db")
    store.init_schema(conn)
    store.upsert_memory(conn, "project", "Router firmware rollback plan", "", "k1")
    store.upsert_memory(conn, "interest", "Sourdough starter", "", "k2")
    hits = search.lexical(conn, "router firmware", limit=5)
    assert [h["dedup_key"] for h in hits] == ["k1"]
