"""ONNX embedder: onnxruntime + the locally cached gte-multilingual-base model.

The heavy dependencies (onnxruntime, tokenizers, numpy) live only in this module
and load lazily via ``hippo_memory.embed.get_embedder()``; the deterministic core
and the test suite never import it. Embeddings are local and offline: no text
ever leaves the machine, and the vectors are dimensionally consistent (768-dim)
across every source in the store.

Pooling: CLS (first token) then L2-normalize, matching the gte-multilingual-base
model card. If recall quality argues for it, mean pooling is a one-line change
here, and a later tuning pass is the place for it.
"""
import urllib.request
from pathlib import Path

from hippo_memory import config

# The three files OnnxEmbedder reads out of EMBED_MODEL_DIR, fetched from the
# model's Hugging Face repo when the user opts into `hippo init --download-model`.
_HF_BASE_URL = "https://huggingface.co/onnx-community/gte-multilingual-base/resolve/main/"
_MODEL_FILES = ("onnx/model_quantized.onnx", "tokenizer.json", "config.json")


def download(model_dir):
    """Fetch the local model files into `model_dir` (offline afterward)."""
    model_dir = Path(model_dir)
    for rel in _MODEL_FILES:
        dest = model_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(_HF_BASE_URL + rel, dest)
    return model_dir


class OnnxEmbedder:
    def __init__(self, model=None, model_dir=None):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self._np = np
        self.model = model or config.EMBED_MODEL
        d = Path(model_dir or config.EMBED_MODEL_DIR)
        self._tok = Tokenizer.from_file(str(d / "tokenizer.json"))
        self._tok.enable_truncation(max_length=config.EMBED_MAX_TOKENS)
        self._tok.enable_padding()
        self._sess = ort.InferenceSession(
            str(d / "onnx" / "model_quantized.onnx"),
            providers=["CPUExecutionProvider"],
        )
        self._inputs = {i.name for i in self._sess.get_inputs()}

    def embed(self, texts):
        np = self._np
        encs = self._tok.encode_batch(list(texts))
        ids = np.array([e.ids for e in encs], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
        feed = {}
        if "input_ids" in self._inputs:
            feed["input_ids"] = ids
        if "attention_mask" in self._inputs:
            feed["attention_mask"] = mask
        if "token_type_ids" in self._inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        last_hidden = self._sess.run(None, feed)[0]   # [batch, seq, hidden]
        cls = last_hidden[:, 0, :]                     # CLS pooling
        norm = np.linalg.norm(cls, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        return (cls / norm).astype(float).tolist()
