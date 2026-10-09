"""Vector storage inside the archive database.

Two backends, chosen once per database and recorded in `meta`:
  vec0   the sqlite-vec extension (`chunk_vec` virtual table), when it can be loaded
  blob   float32 blobs in `chunk_vectors`, searched by brute-force cosine with numpy

Python builds that cannot load SQLite extensions (some macOS system Pythons) use blob.
Set NEW_OUTLOOK_VECTOR_STORE=blob or vec0 to force one.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

from .db import Archive
from .embedder import SemanticUnavailable, require_numpy

BACKEND_ENV = "NEW_OUTLOOK_VECTOR_STORE"
VEC_MAX_K = 4096


def _try_load_vec(archive: Archive) -> bool:
    if getattr(archive, "_vec_loaded", None) is not None:
        return archive._vec_loaded
    ok = False
    try:
        import sqlite_vec

        archive.conn.enable_load_extension(True)
        try:
            sqlite_vec.load(archive.conn)
            ok = True
        finally:
            archive.conn.enable_load_extension(False)
    except Exception:
        ok = False
    archive._vec_loaded = ok
    return ok


def pick_backend(archive: Archive) -> str:
    forced = os.environ.get(BACKEND_ENV, "").strip().lower()
    if forced == "blob":
        return "blob"
    if forced == "vec0":
        if not _try_load_vec(archive):
            raise SemanticUnavailable("NEW_OUTLOOK_VECTOR_STORE=vec0 but the sqlite-vec extension cannot be loaded")
        return "vec0"
    return "vec0" if _try_load_vec(archive) else "blob"


class VectorStore:
    """Add, delete and search normalized vectors keyed by chunk id."""

    def __init__(self, archive: Archive, dim: int, backend: str):
        self.archive = archive
        self.conn = archive.conn
        self.dim = dim
        self.backend = backend
        self._np = require_numpy()
        if backend == "vec0":
            if not _try_load_vec(archive):
                raise SemanticUnavailable(
                    "This archive stores vectors with sqlite-vec, but the extension cannot be loaded here. "
                    "Install new-outlook-mcp[semantic] with a Python that allows SQLite extensions, "
                    "or re-create the vectors with `new-outlook embed --reembed`.")
            self.conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vec USING vec0("
                f"embedding float[{int(dim)}] distance_metric=cosine)")
        else:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS chunk_vectors (chunk_id INTEGER PRIMARY KEY, vec BLOB NOT NULL)")

    # ------------------------------------------------------------------ writes

    def add(self, chunk_ids: list[int], matrix) -> None:
        np = self._np
        matrix = np.asarray(matrix, dtype=np.float32)
        if matrix.shape != (len(chunk_ids), self.dim):
            raise ValueError(f"expected {len(chunk_ids)} vectors of dimension {self.dim}, got {matrix.shape}")
        if self.backend == "vec0":
            self.conn.executemany("INSERT INTO chunk_vec(rowid, embedding) VALUES (?, ?)",
                                  [(cid, matrix[i].tobytes()) for i, cid in enumerate(chunk_ids)])
        else:
            self.conn.executemany("INSERT OR REPLACE INTO chunk_vectors(chunk_id, vec) VALUES (?, ?)",
                                  [(cid, matrix[i].tobytes()) for i, cid in enumerate(chunk_ids)])
        self.archive._blob_cache = None

    def delete(self, chunk_ids: Iterable[int]) -> None:
        table, col = ("chunk_vec", "rowid") if self.backend == "vec0" else ("chunk_vectors", "chunk_id")
        self.conn.executemany(f"DELETE FROM {table} WHERE {col} = ?", [(c,) for c in chunk_ids])
        self.archive._blob_cache = None

    def drop(self) -> None:
        self.conn.execute("DROP TABLE IF EXISTS " + ("chunk_vec" if self.backend == "vec0" else "chunk_vectors"))
        self.archive._blob_cache = None

    # ------------------------------------------------------------------- reads

    def count(self) -> int:
        table = "chunk_vec" if self.backend == "vec0" else "chunk_vectors"
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def get(self, chunk_ids: list[int]):
        """Vectors for the given chunk ids as an (n, dim) array, skipping ids without a vector."""
        np = self._np
        rows = []
        for cid in chunk_ids:
            if self.backend == "vec0":
                r = self.conn.execute("SELECT embedding FROM chunk_vec WHERE rowid = ?", (cid,)).fetchone()
            else:
                r = self.conn.execute("SELECT vec FROM chunk_vectors WHERE chunk_id = ?", (cid,)).fetchone()
            if r:
                rows.append(np.frombuffer(r[0], dtype=np.float32))
        return np.stack(rows) if rows else np.zeros((0, self.dim), dtype=np.float32)

    def knn(self, query, k: int) -> list[tuple[int, float]]:
        """The k nearest chunks as (chunk_id, cosine similarity), best first."""
        np = self._np
        q = np.asarray(query, dtype=np.float32).reshape(-1)
        if q.shape[0] != self.dim:
            raise SemanticUnavailable(f"query vector has {q.shape[0]} dimensions, the archive uses {self.dim}")
        norm = float(np.linalg.norm(q))
        if norm:
            q = q / norm
        if self.backend == "vec0":
            rows = self.conn.execute(
                "SELECT rowid, distance FROM chunk_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (q.tobytes(), max(1, min(int(k), VEC_MAX_K)))).fetchall()
            return [(int(r[0]), 1.0 - float(r[1])) for r in rows]
        ids, mat = self._matrix()
        if not len(ids):
            return []
        sims = mat @ q
        k = min(int(k), len(ids))
        top = np.argpartition(-sims, k - 1)[:k]
        top = top[np.argsort(-sims[top], kind="stable")]
        return [(int(ids[i]), float(sims[i])) for i in top]

    def _matrix(self):
        np = self._np
        sig = self.conn.execute("SELECT COUNT(*), COALESCE(MAX(chunk_id), 0), COALESCE(SUM(chunk_id), 0)"
                                " FROM chunk_vectors").fetchone()
        sig = tuple(sig)
        cache = getattr(self.archive, "_blob_cache", None)
        if cache and cache[0] == sig:
            return cache[1], cache[2]
        rows = self.conn.execute("SELECT chunk_id, vec FROM chunk_vectors ORDER BY chunk_id").fetchall()
        ids = np.array([r[0] for r in rows], dtype=np.int64)
        mat = (np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
               if rows else np.zeros((0, self.dim), dtype=np.float32))
        self.archive._blob_cache = (sig, ids, mat)
        return ids, mat
