"""Embedder adapters.

The rest of the package depends only on the ``embed(texts) -> list[list[float]]``
interface. The ONNX adapter (see ``embed_onnx``) is the single heavy
dependency and is imported lazily, only when embeddings are generated.
Tests use ``FakeEmbedder`` so the suite runs offline with no model.
"""
import hashlib


class EmbedderUnavailable(RuntimeError):
    """Raised when the ONNX model or its runtime is not installed.

    Covers both failure modes behind the single lazy import in
    ``get_embedder``: the optional onnx/tokenizers/numpy dependencies missing
    (the ``recall`` extra was never installed) and the model files themselves
    missing (the extra is installed but the model was never fetched). Callers
    that can carry on without semantic search (``search.lexical``) catch this
    and fall back instead of crashing.
    """


class FakeEmbedder:
    """Deterministic, stdlib-only pseudo-embeddings for tests. Not semantic."""

    def __init__(self, dim=8):
        self.dim = dim

    def embed(self, texts):
        out = []
        for t in texts:
            seed = hashlib.sha256(t.encode("utf-8")).digest()
            vals = []
            while len(vals) < self.dim:
                seed = hashlib.sha256(seed).digest()
                for b in seed:
                    vals.append((b / 255.0) * 2.0 - 1.0)
                    if len(vals) >= self.dim:
                        break
            out.append(vals[: self.dim])
        return out


def get_embedder(model=None):
    """Return the ONNX embedder. Lazy import so onnxruntime and the model
    only load when embeddings are generated, never during tests.

    Raises ``EmbedderUnavailable`` with an actionable message if the optional
    dependencies aren't installed or the model files aren't present, rather
    than letting an ImportError or a raw onnxruntime/tokenizers exception
    surface to the caller.
    """
    try:
        from hippo_memory.embed_onnx import OnnxEmbedder

        return OnnxEmbedder(model)
    except Exception as e:
        raise EmbedderUnavailable(
            f"embedding model unavailable ({e}); install hippo-memory[recall] "
            f"and run `hippo init --download-model`"
        ) from e
