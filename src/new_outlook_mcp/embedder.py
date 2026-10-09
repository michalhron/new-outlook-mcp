"""Embedding backends for search by meaning. Everything runs on this Mac.

Default model: `intfloat/multilingual-e5-small`.
  * 384 dimensions, about 118M parameters, a ~470 MB download, 512 token window.
  * Trained on about 100 languages including English, Czech, Danish, Dutch and Finnish.
  * Fast on Apple Silicon: a few hundred short chunks per second on CPU or MPS, so
    an archive of tens of thousands of mails embeds in well under an hour.
  * Needs the prefixes "query: " and "passage: ", which this module adds.
Alternative: `BAAI/bge-m3` (1024 dimensions, 568M parameters, ~2.3 GB, 8k token window).
It is stronger on long and cross-language text but about five times slower and
needs about three times the vector storage. Pick it with `new-outlook embed --model BAAI/bge-m3`.

Weights are downloaded from Hugging Face once, and only when the user runs
`new-outlook embed --download`. Importing this module, starting the MCP server and
running a sync never download anything. No mail text leaves the machine.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol

DEFAULT_MODEL = "intfloat/multilingual-e5-small"
FAKE_ENV = "NEW_OUTLOOK_EMBEDDER"

MISSING_EXTRA = (
    "Search by meaning is not installed. Install new-outlook-mcp[semantic] "
    "(pipx install 'new-outlook-mcp[semantic]') and run `new-outlook embed --download`."
)
NOT_EMBEDDED = (
    "No embeddings yet. Run `new-outlook embed --download` once in a terminal "
    "(it downloads the model and embeds the archive), then try again."
)


class SemanticUnavailable(RuntimeError):
    """The optional extra, the model or the embeddings are missing. The text says what to do."""


@dataclass(frozen=True)
class ModelSpec:
    query_prefix: str = ""
    passage_prefix: str = ""
    approx_download: str = "size unknown"


MODELS: dict[str, ModelSpec] = {
    "intfloat/multilingual-e5-small": ModelSpec("query: ", "passage: ", "about 470 MB"),
    "intfloat/multilingual-e5-base": ModelSpec("query: ", "passage: ", "about 1.1 GB"),
    "BAAI/bge-m3": ModelSpec("", "", "about 2.3 GB"),
}


class Embedder(Protocol):
    name: str
    dim: int

    def embed_passages(self, texts: list[str], batch_size: int = 32): ...

    def embed_query(self, text: str): ...


def require_numpy():
    try:
        import numpy as np
    except ImportError as exc:
        raise SemanticUnavailable(MISSING_EXTRA) from exc
    return np


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


class HashEmbedder:
    """Deterministic stand-in for tests: hashed bag of words and character trigrams.

    Texts that share words get similar vectors. It has no real semantics.
    """

    def __init__(self, dim: int = 256):
        self.dim = dim
        self.name = f"fake-hash-{dim}"

    def _vec(self, text: str):
        np = require_numpy()
        v = np.zeros(self.dim, dtype=np.float32)
        for word in re.findall(r"\w+", _fold(text)):
            self._add(v, "w:" + word, 1.0)
            padded = f"^{word}$"
            for i in range(len(padded) - 2):
                self._add(v, "t:" + padded[i:i + 3], 0.3)
        norm = float(np.linalg.norm(v))
        if norm:
            v /= norm
        return v

    def _add(self, v, token: str, weight: float) -> None:
        h = hashlib.md5(token.encode()).digest()
        idx = int.from_bytes(h[:4], "little") % self.dim
        v[idx] += weight if h[4] & 1 else -weight

    def embed_passages(self, texts: list[str], batch_size: int = 32):
        np = require_numpy()
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.stack([self._vec(t) for t in texts])

    def embed_query(self, text: str):
        return self._vec(text)


class SentenceTransformerEmbedder:
    """Real backend via sentence-transformers, on MPS when available, else CPU."""

    def __init__(self, model_name: str = DEFAULT_MODEL, *, allow_download: bool = False):
        self._np = require_numpy()
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise SemanticUnavailable(MISSING_EXTRA) from exc
        self.name = model_name
        self.spec = MODELS.get(model_name, ModelSpec())
        try:
            self._model = SentenceTransformer(model_name, device=_pick_device(),
                                              local_files_only=not allow_download)
        except Exception as exc:
            if allow_download:
                raise SemanticUnavailable(f"could not load model {model_name}: {exc}") from exc
            raise SemanticUnavailable(
                f"The model {model_name} is not on this Mac yet. Run `new-outlook embed --download` "
                f"once to fetch it from Hugging Face ({self.spec.approx_download})."
            ) from exc
        getter = getattr(self._model, "get_embedding_dimension", None) or self._model.get_sentence_embedding_dimension
        self.dim = int(getter())

    def _encode(self, texts: list[str], batch_size: int):
        out = self._model.encode(texts, batch_size=batch_size, normalize_embeddings=True,
                                 convert_to_numpy=True, show_progress_bar=False)
        return out.astype(self._np.float32, copy=False)

    def embed_passages(self, texts: list[str], batch_size: int = 32):
        if not texts:
            return self._np.zeros((0, self.dim), dtype=self._np.float32)
        return self._encode([self.spec.passage_prefix + t for t in texts], batch_size)

    def embed_query(self, text: str):
        return self._encode([self.spec.query_prefix + text], 1)[0]


def _pick_device() -> str:
    try:
        import torch

        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


_cache: dict[tuple[str, bool], Embedder] = {}


def fake_requested() -> bool:
    return os.environ.get(FAKE_ENV, "").strip().lower() == "fake"


def get_embedder(model_name: str | None = None, *, allow_download: bool = False) -> Embedder:
    """The embedder for a model name. NEW_OUTLOOK_EMBEDDER=fake selects the test backend."""
    if fake_requested():
        return HashEmbedder()
    name = model_name or DEFAULT_MODEL
    key = (name, allow_download)
    if key not in _cache:
        _cache[key] = SentenceTransformerEmbedder(name, allow_download=allow_download)
    return _cache[key]


def semantic_installed() -> bool:
    """True when the packages for the active backend can be imported (cheap, loads no model)."""
    import importlib.util

    needed = ["numpy"] if fake_requested() else ["numpy", "sentence_transformers"]
    try:
        return all(importlib.util.find_spec(n) is not None for n in needed)
    except (ImportError, ValueError):
        return False
