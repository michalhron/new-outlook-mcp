"""Search by meaning: embedding the archive and querying it.

Indexing: `backfill` embeds messages that have no chunks yet, newest first, and commits after
every group, so it can be interrupted and resumed. `embed_after_sync` tops up new mail after a
sync, but only when embeddings were initialized before.

Querying: `semantic_search` fuses the FTS5 keyword ranking and the vector ranking with
Reciprocal Rank Fusion. `find_similar` averages the vectors of an email or attachment.
Every result path passes its candidate ids through `privacy.filter_allowed_message_ids`.

This module imports without the optional extra. numpy, sqlite-vec and sentence-transformers
are imported only when a function needs them.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import attachments as att_mod
from . import tools
from .chunking import chunk_body, chunk_plain, kept_lines, line_key
from .db import Archive, _now
from .embedder import (MISSING_EXTRA, NOT_EMBEDDED, Embedder, SemanticUnavailable, get_embedder,
                       semantic_installed)
from .privacy import filter_allowed_message_ids
from .tools import ToolInputError
from .vectors import VEC_MAX_K, VectorStore, pick_backend

log = logging.getLogger(__name__)

RRF_K = 60
ATTACHMENT_TEXT_CAP = 60_000  # characters read from one attachment
ATTACHMENT_CHUNK_CAP = 60
SNIPPET_CHARS = 300
SYNC_EMBED_MAX_MESSAGES = 2000
#: A body line seen in at least this many messages is a footer or banner, not content, and is not embedded.
BOILERPLATE_MIN_MESSAGES = 10
#: Lines shorter than this are never counted as boilerplate on their own ("Hi all", "Thanks").
BOILERPLATE_MIN_CHARS = 12
#: Version of the rules that turn mail into chunks. Embeddings made with an older version still work,
#: but `new-outlook embed --reembed` rebuilds them with the current rules (v2: boilerplate lines dropped).
CHUNKING_VERSION = 2


class ModelMismatch(SemanticUnavailable):
    pass


# ---------------------------------------------------------------------- state

def _meta_get(archive: Archive, key: str) -> str | None:
    r = archive.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return r[0] if r else None


def _meta_set(archive: Archive, key: str, value: str) -> None:
    archive.conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                         (key, value))


def index_state(archive: Archive) -> dict | None:
    """Model, dimension and backend of the embeddings in this archive, or None if never initialized."""
    model = _meta_get(archive, "embed_model")
    if not model:
        return None
    return {"model": model, "dim": int(_meta_get(archive, "embed_dim") or 0),
            "backend": _meta_get(archive, "embed_backend") or "blob"}


def status(archive: Archive) -> dict:
    st = index_state(archive) or {}
    c = archive.conn
    total = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    done = c.execute("SELECT COUNT(*) FROM embedded_messages").fetchone()[0]
    chunking = int(_meta_get(archive, "embed_chunking") or 1) if st else CHUNKING_VERSION
    return {**st, "messages": total, "messages_embedded": done, "chunks": c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
            "installed": semantic_installed(), "chunking": chunking, "chunking_outdated": chunking < CHUNKING_VERSION}


def _check_model(state: dict, embedder: Embedder) -> None:
    if state["model"] != embedder.name or state["dim"] != embedder.dim:
        raise ModelMismatch(
            f"This archive was embedded with {state['model']} ({state['dim']} dimensions), not {embedder.name} "
            f"({embedder.dim}). Vectors from different models cannot be mixed. "
            f"Use `new-outlook embed --model {state['model']}`, or start over with "
            f"`new-outlook embed --reembed --model {embedder.name}`.")


def init_index(archive: Archive, embedder: Embedder) -> VectorStore:
    """Record the model on first use, refuse a different one afterwards, and open the vector store."""
    state = index_state(archive)
    if state is None:
        with archive.transaction():
            backend = pick_backend(archive)
            _meta_set(archive, "embed_model", embedder.name)
            _meta_set(archive, "embed_dim", str(embedder.dim))
            _meta_set(archive, "embed_backend", backend)
            _meta_set(archive, "embed_chunking", str(CHUNKING_VERSION))
        state = index_state(archive)
    _check_model(state, embedder)
    store = VectorStore(archive, state["dim"], state["backend"])
    archive.conn.commit()
    return store


def reset_index(archive: Archive) -> None:
    """Forget all embeddings so a different model can be used."""
    state = index_state(archive)
    with archive.transaction():
        if state:
            VectorStore(archive, state["dim"], state["backend"]).drop()
        archive.conn.execute("DELETE FROM chunks")
        archive.conn.execute("DELETE FROM embedded_messages")
        archive.conn.execute(
            "DELETE FROM meta WHERE key IN ('embed_model', 'embed_dim', 'embed_backend', 'embed_chunking')")


def _open_for_query(archive: Archive) -> tuple[Embedder, VectorStore]:
    if not semantic_installed():
        raise SemanticUnavailable(MISSING_EXTRA)
    state = index_state(archive)
    if state is None:
        raise SemanticUnavailable(NOT_EMBEDDED)
    embedder = get_embedder(state["model"])
    _check_model(state, embedder)
    return embedder, VectorStore(archive, state["dim"], state["backend"])


# ------------------------------------------------------------------- chunking

@dataclass
class ChunkSpec:
    kind: str
    ord: int
    text: str
    start: int | None
    end: int | None
    attachment_id: int | None = None
    embed_text: str = ""

    def __post_init__(self):
        self.embed_text = self.embed_text or self.text


def boilerplate_lines(archive: Archive, min_messages: int = BOILERPLATE_MIN_MESSAGES) -> frozenset[str]:
    """Body lines (as `line_key`) that occur in at least `min_messages` messages: footers, disclaimers, banners.

    Counted over the text that remains after quote and signature stripping, so a line
    quoted in a long thread does not count, but a footer under every newsletter does.
    """
    counts: dict[str, int] = {}
    for (body,) in archive.conn.execute("SELECT body_text FROM messages WHERE body_text IS NOT NULL"):
        for key in {line_key(line) for line in kept_lines(body)}:
            if len(key) >= BOILERPLATE_MIN_CHARS:
                counts[key] = counts.get(key, 0) + 1
    return frozenset(k for k, n in counts.items() if n >= min_messages)


def build_chunks(archive: Archive, pk: int, boilerplate: frozenset[str] = frozenset()) -> list[ChunkSpec]:
    """Subject, body and local attachment text of one message as chunks. Body lines in `boilerplate` are left out."""
    row = archive.conn.execute("SELECT subject, body_text FROM messages WHERE id = ?", (pk,)).fetchone()
    if row is None:
        return []
    specs: list[ChunkSpec] = []
    subject = (row["subject"] or "").strip()
    if subject:
        specs.append(ChunkSpec("subject", 0, subject, 0, len(subject)))
    for i, c in enumerate(chunk_body(row["body_text"], boilerplate)):
        specs.append(ChunkSpec("body", i, c.text, c.start, c.end))
    for att in tools._attachment_rows(archive, pk):
        if att.is_inline and att.is_small_inline_image:
            continue
        try:
            if not att_mod.available(archive, att):
                continue
            text, _kind = att_mod.extract_text(archive, att)
        except Exception as exc:
            log.debug("no text from attachment %s: %s", att.id, exc)
            continue
        if not text:
            continue
        for i, c in enumerate(chunk_plain(text[:ATTACHMENT_TEXT_CAP])[:ATTACHMENT_CHUNK_CAP]):
            specs.append(ChunkSpec("attachment", i, c.text, c.start, c.end, att.id,
                                   embed_text=f"{att.display_name}\n{c.text}"))
    return specs


# ------------------------------------------------------------------- indexing

def _embed_group(archive: Archive, embedder: Embedder, store: VectorStore, pks: list[int], batch: int,
                 boilerplate: frozenset[str] = frozenset()) -> int:
    """Chunk, embed and store a group of messages in one transaction. Returns the chunk count."""
    specs: list[tuple[int, ChunkSpec]] = []
    for pk in pks:
        try:
            specs.extend((pk, s) for s in build_chunks(archive, pk, boilerplate))
        except Exception as exc:  # a bad message must not block the backfill
            log.warning("could not chunk message %s: %s", pk, exc)
    matrix = embedder.embed_passages([s.embed_text for _, s in specs], batch)
    now = _now()
    counts = {pk: 0 for pk in pks}
    with archive.transaction():
        marks = ",".join("?" * len(pks))
        old = [r[0] for r in archive.conn.execute(f"SELECT id FROM chunks WHERE message_pk IN ({marks})", pks)]
        if old:
            store.delete(old)
            archive.conn.execute(f"DELETE FROM chunks WHERE message_pk IN ({marks})", pks)
        ids = []
        for pk, s in specs:
            cur = archive.conn.execute(
                "INSERT INTO chunks(kind, message_pk, attachment_id, ord, text, char_start, char_end, model, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (s.kind, pk, s.attachment_id, s.ord, s.text, s.start, s.end, embedder.name, now))
            ids.append(cur.lastrowid)
            counts[pk] += 1
        if ids:
            store.add(ids, matrix)
        archive.conn.executemany(
            "INSERT OR REPLACE INTO embedded_messages(message_pk, model, n_chunks, embedded_at) VALUES (?, ?, ?, ?)",
            [(pk, embedder.name, n, now) for pk, n in counts.items()])
    return len(specs)


def pending_count(archive: Archive) -> int:
    return archive.conn.execute(
        "SELECT COUNT(*) FROM messages m WHERE NOT EXISTS (SELECT 1 FROM embedded_messages e WHERE e.message_pk = m.id)"
    ).fetchone()[0]


def _next_pending(archive: Archive, n: int) -> list[int]:
    return [r[0] for r in archive.conn.execute(
        "SELECT m.id FROM messages m WHERE NOT EXISTS (SELECT 1 FROM embedded_messages e WHERE e.message_pk = m.id)"
        " ORDER BY m.date_ts DESC, m.id DESC LIMIT ?", (n,))]


@dataclass
class BackfillResult:
    messages: int = 0
    chunks: int = 0
    pending_at_start: int = 0
    remaining: int = 0
    seconds: float = 0.0
    interrupted: bool = False
    notes: list[str] = field(default_factory=list)


Progress = Callable[[int, int, float, float | None], None]


def backfill(
    archive: Archive,
    embedder: Embedder,
    *,
    limit: int | None = None,
    batch: int = 32,
    progress: Progress | None = None,
    stop: Callable[[], bool] | None = None,
) -> BackfillResult:
    """Embed messages that have no embeddings yet, newest first.

    Each group of `batch` messages is committed on its own. Stopping (Ctrl-C or `stop()`) loses at
    most the group in flight, and the next run continues with what is still pending.
    """
    store = init_index(archive, embedder)
    res = BackfillResult(pending_at_start=pending_count(archive))
    total = min(res.pending_at_start, limit) if limit else res.pending_at_start
    started = time.monotonic()
    batch = max(1, int(batch))
    boilerplate = boilerplate_lines(archive) if total else frozenset()
    try:
        while res.messages < total:
            pks = _next_pending(archive, min(batch, total - res.messages))
            if not pks:
                break
            res.chunks += _embed_group(archive, embedder, store, pks, batch, boilerplate)
            res.messages += len(pks)
            elapsed = max(time.monotonic() - started, 1e-9)
            rate = res.messages / elapsed
            if progress:
                progress(res.messages, total, rate, (total - res.messages) / rate if rate else None)
            if stop and stop():
                res.interrupted = res.messages < total
                break
    except KeyboardInterrupt:
        res.interrupted = True
    res.seconds = time.monotonic() - started
    res.remaining = pending_count(archive)
    return res


def embed_after_sync(archive: Archive, max_messages: int = SYNC_EMBED_MAX_MESSAGES) -> int:
    """Embed new mail after a sync. Does nothing unless embeddings were initialized and the extra is installed.

    Never downloads a model. Returns the number of messages embedded.
    """
    state = index_state(archive)
    if state is None or not semantic_installed():
        return 0
    try:
        embedder = get_embedder(state["model"])
        return backfill(archive, embedder, limit=max_messages).messages
    except SemanticUnavailable as exc:
        log.warning("skipped embedding new mail: %s", exc)
        return 0


# ------------------------------------------------------------------- fusion

def rrf(rankings: list[list[int]], k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion: score(d) = sum over lists of 1 / (k + rank), ranks starting at 1."""
    scores: dict[int, float] = {}
    first: dict[int, int] = {}
    for ranking in rankings:
        for rank, doc in enumerate(ranking, 1):
            scores[doc] = scores.get(doc, 0.0) + 1.0 / (k + rank)
            first.setdefault(doc, len(first))
    return sorted(scores.items(), key=lambda kv: (-kv[1], first[kv[0]]))


# ---------------------------------------------------------------- retrieval

_JOIN = " LEFT JOIN folders f ON f.id = m.folder_id LEFT JOIN accounts a ON a.id = m.account_id"
_CHUNK_COLS = "id, kind, message_pk, attachment_id, text"


def _in_batches(items: list, size: int = 800):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _passing_filters(archive: Archive, pks: list[int], where: list[str], params: list) -> set[int]:
    if not where:
        return set(pks)
    out: set[int] = set()
    for part in _in_batches(pks):
        sql = (f"SELECT m.id FROM messages m{_JOIN} WHERE " + " AND ".join(where)
               + f" AND m.id IN ({','.join('?' * len(part))})")
        out.update(r[0] for r in archive.conn.execute(sql, [*params, *part]))
    return out


def _vector_hits(archive: Archive, store: VectorStore, qvec, where: list[str], params: list, pool: int,
                 *, skip: Callable[[sqlite3.Row], bool] | None = None) -> list[dict]:
    """Best chunk per message, nearest first, after filters, privacy and `skip`."""
    k = pool * 3
    if where or skip:  # filters apply after the nearest-neighbour search, so look deeper
        k = max(store.count(), 1) if store.backend == "blob" else VEC_MAX_K
    hits = store.knn(qvec, k)
    if not hits:
        return []
    sims = dict(hits)
    rows: dict[int, sqlite3.Row] = {}
    for part in _in_batches(list(sims)):
        for r in archive.conn.execute(f"SELECT {_CHUNK_COLS} FROM chunks WHERE id IN ({','.join('?' * len(part))})", part):
            rows[r["id"]] = r
    best: dict[int, dict] = {}
    order: list[int] = []
    for cid, sim in hits:
        r = rows.get(cid)
        if r is None or (skip and skip(r)) or r["message_pk"] in best:
            continue
        best[r["message_pk"]] = {"pk": r["message_pk"], "score": sim, "kind": r["kind"],
                                 "attachment_id": r["attachment_id"], "text": r["text"]}
        order.append(r["message_pk"])
    ok = _passing_filters(archive, order, where, params)
    allowed = set(filter_allowed_message_ids(archive, [pk for pk in order if pk in ok]))
    return [best[pk] for pk in order if pk in allowed][:pool]


_FTS_SYNTAX = re.compile(r'["()*:^]|\b(AND|OR|NOT|NEAR)\b')


#: Function words left out of the keyword half of hybrid search, which ORs the words of a question:
#: "the invoice from my physiotherapist" must not match every message containing "the" or "from".
#: English, Dutch, Czech, German, French and Danish, the languages the mail stripping rules know.
STOPWORDS = frozenset("""
the and for from with that this was were are has have had not but you your our their them they
about into over after before when what which who whom whose where why how all any can could would should
will just than then there these those its also been being does did doing more most other some such only
own same very out off per via
het een van voor met dat die deze niet maar ook als bij naar aan wat wie waar hoe zijn hebben heeft
was werd wordt worden mijn jouw onze hun uit over door tot nog wel geen dan
jak jsem jsme jste nebo ale pro jako tak ten tato toto které který která není byl byla bylo
der die das und mit von für nicht ein eine ist sind war wie auch auf aus bei nach über
les des une est pas que qui pour dans par sur avec sont aux
det den der og til fra med som har var ikke men
""".split())  # noqa: SIM905 - a word list reads better as text


def _fts_query(query: str) -> str:
    """Plain questions become an OR of their words so one missing word does not empty the result.

    Function words are dropped unless the question has nothing else.
    """
    if _FTS_SYNTAX.search(query):
        return query
    words = list(dict.fromkeys(w.lower() for w in re.findall(r"\w+", query, flags=re.UNICODE) if len(w) > 2))
    content = [w for w in words if w not in STOPWORDS] or words
    content = content[:16]
    return " OR ".join('"' + w + '"' for w in content) if content else query


def _keyword_hits(archive: Archive, query: str, where: list[str], params: list, pool: int) -> list[dict]:
    sql = (
        "WITH hits AS MATERIALIZED (SELECT rowid AS pk, bm25(messages_fts, 4.0, 2.0, 1.0, 1.0) AS score,"
        " snippet(messages_fts, 3, '[', ']', ' … ', 24) AS snip FROM messages_fts WHERE messages_fts MATCH ?)"
        f" SELECT hits.pk, hits.snip FROM hits JOIN messages m ON m.id = hits.pk{_JOIN}"
        + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY hits.score LIMIT ?"
    )
    q = _fts_query(query)
    try:
        rows = archive.conn.execute(sql, [q, *params, pool]).fetchall()
    except sqlite3.OperationalError:
        q = tools._fts_fallback(query)
        if not q:
            return []
        rows = archive.conn.execute(sql, [q, *params, pool]).fetchall()
    allowed = set(filter_allowed_message_ids(archive, [r["pk"] for r in rows]))
    return [{"pk": r["pk"], "snippet": r["snip"]} for r in rows if r["pk"] in allowed]


def _trim(text: str, n: int = SNIPPET_CHARS) -> str:
    s = re.sub(r"\s+", " ", text).strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + " …"


def _fetch_rows(archive: Archive, pks: list[int]) -> dict[int, sqlite3.Row]:
    out: dict[int, sqlite3.Row] = {}
    for part in _in_batches(pks):
        for r in archive.conn.execute(f"{tools._BASE_SELECT} WHERE m.id IN ({','.join('?' * len(part))})", part):
            out[r["id"]] = r
    return out


def _result(row: sqlite3.Row, *, score: float, vec: dict | None, kw: dict | None, archive: Archive) -> dict:
    if vec:
        snippet = _trim(vec["text"])
    else:
        snippet = kw["snippet"] if kw and kw.get("snippet") else None
    out = tools._summary(row, snippet, archive.conn)
    out["score"] = round(score, 4)
    out["matched"] = "both" if vec and kw else ("semantic" if vec else "keyword")
    if vec:
        match = {"kind": vec["kind"]}
        if vec["attachment_id"] is not None:
            match["attachment_id"] = vec["attachment_id"]
            r = archive.conn.execute("SELECT filename FROM attachments WHERE id = ?", (vec["attachment_id"],)).fetchone()
            if r:
                match["filename"] = r[0]
        out["match"] = match
    return out


def search_by_mode(archive: Archive, query: str | None, mode: str, where: list[str], params: list, *,
                   sort: str = "relevance", limit: int = 20, offset: int = 0) -> dict:
    """Semantic or hybrid search with pre-built filter clauses (see tools._filter_clauses)."""
    query = (query or "").strip()
    if not query:
        raise ToolInputError("semantic and hybrid search need a query")
    if mode not in ("semantic", "hybrid"):
        raise ToolInputError("mode must be 'semantic' or 'hybrid'")
    if sort not in ("relevance", "date_desc", "date_asc"):
        raise ToolInputError("sort must be 'relevance', 'date_desc' or 'date_asc'")
    embedder, store = _open_for_query(archive)
    pool = max(100, (offset + limit) * 5)
    vec_hits = _vector_hits(archive, store, embedder.embed_query(query), where, params, pool)
    kw_hits = _keyword_hits(archive, query, where, params, pool) if mode == "hybrid" else []
    by_vec = {h["pk"]: h for h in vec_hits}
    by_kw = {h["pk"]: h for h in kw_hits}
    fused = rrf([[h["pk"] for h in vec_hits], [h["pk"] for h in kw_hits]]) if mode == "hybrid" \
        else [(h["pk"], h["score"]) for h in vec_hits]
    rows = _fetch_rows(archive, [pk for pk, _ in fused])
    fused = [(pk, s) for pk, s in fused if pk in rows]
    if sort != "relevance":
        fused.sort(key=lambda x: rows[x[0]]["date_ts"] or 0, reverse=(sort == "date_desc"))
    page = fused[offset:offset + limit]
    results = [_result(rows[pk], score=s, vec=by_vec.get(pk), kw=by_kw.get(pk), archive=archive) for pk, s in page]
    out = {"mode": mode, "total": len(fused), "offset": offset, "count": len(results), "results": results}
    if offset + len(results) < len(fused):
        out["next_offset"] = offset + len(results)
    return out


def semantic_search(archive: Archive, query: str, *, mode: str = "hybrid", limit: int = 10, offset: int = 0,
                    sort: str = "relevance", **filters) -> dict:
    where, params = tools._filter_clauses(archive, **filters)
    return search_by_mode(archive, query, mode, where, params, sort=sort, limit=tools._clamp_limit(limit),
                          offset=max(0, int(offset)))


def find_similar(archive: Archive, *, email_id: str | None = None, attachment_id: int | str | None = None,
                 limit: int = 10, realm: str | None = None) -> dict:
    """Messages (best matching chunk each) whose content resembles an email or one attachment."""
    if (email_id is None) == (attachment_id is None):
        raise ToolInputError("give exactly one of email_id or attachment_id")
    limit = tools._clamp_limit(limit)
    embedder, store = _open_for_query(archive)
    if email_id is not None:
        row = tools._resolve_id(archive, email_id)
        if not filter_allowed_message_ids(archive, [row["id"]]):
            raise ToolInputError(f"no email with id {email_id!r}")
        src_pk = row["id"]
        chunk_ids = [r[0] for r in archive.conn.execute("SELECT id FROM chunks WHERE message_pk = ?", (src_pk,))]
        source = {"email_id": src_pk, "subject": row["subject"] or ""}
        skip = lambda r: r["message_pk"] == src_pk  # noqa: E731
    else:
        try:
            att = att_mod.get_row(archive, attachment_id)
        except att_mod.AttachmentError as exc:
            raise ToolInputError(str(exc)) from exc
        if not filter_allowed_message_ids(archive, [att.message_pk]):
            raise ToolInputError(f"no attachment with id {attachment_id!r}")
        chunk_ids = [r[0] for r in archive.conn.execute("SELECT id FROM chunks WHERE attachment_id = ?", (att.id,))]
        source = {"attachment_id": att.id, "filename": att.display_name, "email_id": att.message_pk}
        skip = lambda r: r["message_pk"] == att.message_pk  # noqa: E731
    vecs = store.get(chunk_ids)
    if not len(vecs):
        raise ToolInputError("this item has no embeddings yet (nothing to compare, or `new-outlook embed` has not "
                             "reached it). Run `new-outlook embed` and try again.")
    q = vecs.mean(axis=0)
    where, params = tools._filter_clauses(archive, realm=realm)
    hits = _vector_hits(archive, store, q, where, params, max(100, limit * 5), skip=skip)[:limit]
    rows = _fetch_rows(archive, [h["pk"] for h in hits])
    results = [_result(rows[h["pk"]], score=h["score"], vec=h, kw=None, archive=archive)
               for h in hits if h["pk"] in rows]
    return {"source": source, "count": len(results), "results": results}
