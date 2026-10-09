"""Second-stage reranking for search by meaning. Everything runs on this Mac.

Vector search scores the query and each passage separately, so with a small embedding
model a vague description ("payment request for personal training") scores about the
same against the right email as against hundreds of others. A cross-encoder reads the
query and a candidate together and orders the best few far more reliably.

Default model: `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`, multilingual (trained on
mMARCO, 14 languages including Dutch), about 470 MB. Reranking the top 50 candidates
takes under a second on Apple Silicon.

On the author's archive (9,225 messages, 12 descriptive test queries), reranking the
top 50 hybrid results moved the right email to first place in 4 of 12 queries (2 before)
and into the top 5 in 6 (4 before).

Like the embedding model, the weights are fetched only by `new-outlook embed --download`.
Without them, or with NEW_OUTLOOK_RERANK=off, search works as before, unreranked.
"""

from __future__ import annotations

import os
import re
from typing import Protocol

from .embedder import FAKE_ENV, _fold, _pick_device

DEFAULT_RERANKER = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
APPROX_DOWNLOAD = "about 470 MB"
RERANK_ENV = "NEW_OUTLOOK_RERANK"
#: How many of the best first-stage results are reranked. More finds more but also adds distractors.
RERANK_POOL = 50


class Reranker(Protocol):
    name: str

    def score(self, query: str, texts: list[str]) -> list[float]: ...


class OverlapReranker:
    """Deterministic stand-in for tests: the share of query words found in the text."""

    name = "fake-overlap"

    def score(self, query: str, texts: list[str]) -> list[float]:
        words = set(re.findall(r"\w+", _fold(query)))
        out = []
        for t in texts:
            have = set(re.findall(r"\w+", _fold(t)))
            out.append(len(words & have) / len(words) if words else 0.0)
        return out


class CrossEncoderReranker:
    def __init__(self, model_name: str = DEFAULT_RERANKER, *, allow_download: bool = False):
        from sentence_transformers import CrossEncoder

        self.name = model_name
        self._model = CrossEncoder(model_name, device=_pick_device(), local_files_only=not allow_download)

    def score(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        out = self._model.predict([(query, t) for t in texts], batch_size=32, show_progress_bar=False)
        return [float(x) for x in out]


_cache: dict[str, Reranker | None] = {}


def disabled() -> bool:
    return os.environ.get(RERANK_ENV, "").strip().lower() in ("off", "0", "false", "no")


def get_reranker(*, allow_download: bool = False) -> Reranker | None:
    """The reranker, or None when it is turned off or its model is not on this Mac. Never downloads unless asked."""
    mode = os.environ.get(RERANK_ENV, "").strip().lower()
    if mode == "fake":
        return OverlapReranker()
    if disabled() or os.environ.get(FAKE_ENV, "").strip().lower() == "fake":
        return None
    key = DEFAULT_RERANKER
    if key in _cache and not (allow_download and _cache[key] is None):
        return _cache[key]
    try:
        _cache[key] = CrossEncoderReranker(DEFAULT_RERANKER, allow_download=allow_download)
    except Exception:  # model missing, extra not installed: search falls back to the unreranked order
        if allow_download:
            raise
        _cache[key] = None
    return _cache[key]
