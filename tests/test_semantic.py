"""Search by meaning, with the deterministic fake embedder. Nothing is downloaded."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

pytest.importorskip("numpy")

from synthetic import add_legacy_message, unix  # noqa: E402

from new_outlook_mcp import embedder as emb  # noqa: E402
from new_outlook_mcp import privacy, semantic, tools  # noqa: E402
from new_outlook_mcp.model import AttachmentInfo, MessageRecord  # noqa: E402
from new_outlook_mcp.sync import sync  # noqa: E402


def _vec0_works() -> bool:
    try:
        import sqlite3

        import sqlite_vec

        c = sqlite3.connect(":memory:")
        c.enable_load_extension(True)
        sqlite_vec.load(c)
        return True
    except Exception:
        return False


@pytest.fixture(autouse=True)
def _fake(monkeypatch):
    monkeypatch.setenv(emb.FAKE_ENV, "fake")
    yield
    privacy.set_filter(None)


@pytest.fixture(params=["blob", "vec0"])
def backend(request, monkeypatch):
    if request.param == "vec0" and not _vec0_works():
        pytest.skip("sqlite-vec cannot be loaded here")
    monkeypatch.setenv("NEW_OUTLOOK_VECTOR_STORE", request.param)
    return request.param


def _add(archive, key, subject, body, *, sender="alice@example.org", folder="Inbox", day=1, atts=()):
    rec = MessageRecord(source="t", source_key=key, message_id=f"<{key}@example.org>", subject=subject,
                        from_name=sender.split("@")[0].title(), from_addr=sender, folder=folder, account="Work",
                        date=datetime(2024, 3, day, 12, tzinfo=timezone.utc), body_text=body,
                        attachments=list(atts))
    with archive.transaction():
        return archive.upsert(rec).pk


@pytest.fixture
def corpus(archive, backend):
    ids = {
        "hype": _add(archive, "hype", "Feedback on the draft",
                     "I suggest reframing the hype paper around construct validity of the measures.\n\n"
                     "On Mon, Bob wrote:\n> the zebra budget is unrelated\n", sender="alice@example.org", day=1),
        "lunch": _add(archive, "lunch", "Lunch on Thursday", "Shall we eat in the canteen on Thursday?",
                      sender="bob@example.net", folder="Personal", day=2),
        "budget": _add(archive, "budget", "Quarterly budget review", "The quarterly budget numbers are attached.",
                       sender="carol@example.org", day=3),
        "cz": _add(archive, "cz", "Schůzka v Brně", "Přijďte včas na schůzku o rozpočtu.\n\nS pozdravem\nAda",
                   sender="dana@example.cz", day=4),
    }
    embedder = emb.get_embedder()
    semantic.backfill(archive, embedder, batch=2)
    return ids


# ------------------------------------------------------------------- fusion

def test_rrf_math():
    out = dict(semantic.rrf([[1, 2, 3], [3, 1]]))
    assert out[1] == pytest.approx(1 / 61 + 1 / 62)
    assert out[2] == pytest.approx(1 / 62)
    assert out[3] == pytest.approx(1 / 63 + 1 / 61)
    assert [d for d, _ in semantic.rrf([[1, 2, 3], [3, 1]])] == [1, 3, 2]
    assert semantic.rrf([[], []]) == []
    # a tie keeps the order in which documents were first seen
    assert [d for d, _ in semantic.rrf([[5], [6]])] == [5, 6]


# ------------------------------------------------------------------ indexing

def test_backfill_chunks_and_state(archive, corpus, backend):
    st = semantic.status(archive)
    assert st["model"] == "fake-hash-256" and st["dim"] == 256 and st["backend"] == backend
    assert st["messages_embedded"] == 4
    kinds = {r[0] for r in archive.conn.execute("SELECT kind FROM chunks")}
    assert kinds == {"subject", "body"}
    texts = " ".join(r[0] for r in archive.conn.execute("SELECT text FROM chunks WHERE kind = 'body'"))
    assert "zebra" not in texts and "S pozdravem" not in texts  # quote and signature stripped
    store = semantic.VectorStore(archive, 256, backend)
    assert store.count() == archive.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]


def test_backfill_is_resumable(archive):
    for i in range(7):
        _add(archive, f"m{i}", f"Subject {i}", f"Body text number {i} about topic {i}.", day=i + 1)
    embedder = emb.get_embedder()
    calls = []
    first = semantic.backfill(archive, embedder, batch=2, progress=lambda *a: calls.append(a),
                              stop=lambda: len(calls) >= 1)
    assert first.interrupted and first.messages == 2 and first.remaining == 5
    assert calls[0][:2] == (2, 7)
    # newest messages come first
    done = {r[0] for r in archive.conn.execute("SELECT message_pk FROM embedded_messages")}
    newest = {r[0] for r in archive.conn.execute("SELECT id FROM messages ORDER BY date_ts DESC LIMIT 2")}
    assert done == newest
    second = semantic.backfill(archive, embedder, batch=2)
    assert not second.interrupted and second.messages == 5 and second.remaining == 0
    chunks = archive.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert chunks == 14 and semantic.status(archive)["messages_embedded"] == 7
    assert semantic.backfill(archive, embedder).messages == 0
    assert archive.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 14


def test_backfill_limit(archive):
    for i in range(5):
        _add(archive, f"m{i}", f"S{i}", f"Body {i} text here.", day=i + 1)
    res = semantic.backfill(archive, emb.get_embedder(), limit=3)
    assert res.messages == 3 and res.remaining == 2


def test_refuses_to_mix_models(archive, corpus):
    other = emb.HashEmbedder(dim=128)
    _add(archive, "late", "Late mail", "arrived later")
    with pytest.raises(semantic.ModelMismatch, match="--reembed"):
        semantic.backfill(archive, other)
    semantic.reset_index(archive)
    assert semantic.index_state(archive) is None and semantic.pending_count(archive) == 5
    res = semantic.backfill(archive, other)
    assert res.messages == 5 and semantic.index_state(archive)["dim"] == 128


def test_attachment_text_is_chunked(archive, tmp_path, backend):
    import docx

    p = tmp_path / "chapter.docx"
    d = docx.Document()
    d.add_paragraph("Draft chapter about lighthouses and their keepers on remote islands.")
    d.save(p)
    att = AttachmentInfo("chapter.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                         local_path=str(p), storage="file")
    missing = AttachmentInfo("gone.pdf", "application/pdf", local_path=None, storage=None)
    pk = _add(archive, "att", "Chapter attached", "Please read.", atts=[att, missing])
    semantic.backfill(archive, emb.get_embedder())
    row = archive.conn.execute("SELECT * FROM chunks WHERE kind = 'attachment'").fetchone()
    assert row["message_pk"] == pk and row["attachment_id"] and "lighthouses" in row["text"]
    res = tools.semantic_search(archive, "lighthouse keepers", mode="semantic")
    top = res["results"][0]
    assert top["id"] == pk and top["match"]["kind"] == "attachment" and top["match"]["filename"] == "chapter.docx"
    # find_similar from the attachment skips the message it came with
    _add(archive, "other", "Islands", "Lighthouse keepers on remote islands have a hard life.")
    semantic.backfill(archive, emb.get_embedder())
    sim = tools.find_similar(archive, attachment_id=row["attachment_id"])
    assert sim["results"] and sim["results"][0]["subject"] == "Islands"


# ----------------------------------------------------------------- retrieval

def test_semantic_finds_without_all_keywords(archive, corpus, backend):
    q = "reframing construct validity zebra lighthouse"
    assert tools.search_emails(archive, q)["total"] == 0  # keyword mode needs every word
    res = tools.search_emails(archive, q, mode="semantic")
    assert res["mode"] == "semantic" and res["results"][0]["id"] == corpus["hype"]
    top = res["results"][0]
    assert top["matched"] == "semantic" and top["match"]["kind"] == "body"
    assert "reframing" in top["snippet"] and "zebra" not in top["snippet"]


def test_hybrid_ranking_and_modes(archive, corpus, backend):
    res = tools.semantic_search(archive, "construct validity of the measures")
    assert res["mode"] == "hybrid"
    top = res["results"][0]
    assert top["id"] == corpus["hype"] and top["matched"] == "both"
    assert top["score"] == pytest.approx(2 / 61, abs=1e-4)
    # one message per result, however many chunks matched
    ids = [r["id"] for r in res["results"]]
    assert len(ids) == len(set(ids))
    cz = tools.semantic_search(archive, "schůzka rozpočet", mode="semantic")
    assert cz["results"][0]["id"] == corpus["cz"]
    kw = tools.search_emails(archive, "quarterly budget")
    assert kw["results"][0]["id"] == corpus["budget"] and "matched" not in kw["results"][0]


def test_hybrid_via_search_emails_pages_and_sorts(archive, corpus):
    r1 = tools.search_emails(archive, "budget numbers", mode="hybrid", limit=2)
    assert r1["count"] == 2 and r1["total"] >= 3 and r1["next_offset"] == 2
    r2 = tools.search_emails(archive, "budget numbers", mode="hybrid", limit=2, offset=2)
    assert {x["id"] for x in r1["results"]}.isdisjoint({x["id"] for x in r2["results"]})
    by_date = tools.search_emails(archive, "budget numbers", mode="hybrid", sort="date_asc", limit=10)
    dates = [x["date"] for x in by_date["results"]]
    assert dates == sorted(dates)


def test_filters_apply(archive, corpus, backend):
    q = "budget numbers canteen"
    allr = tools.semantic_search(archive, q, mode="semantic", limit=10)
    assert len(allr["results"]) == 4
    only = tools.semantic_search(archive, q, mode="semantic", from_="bob")
    assert [r["id"] for r in only["results"]] == [corpus["lunch"]]
    assert [r["id"] for r in tools.semantic_search(archive, q, folder="personal")["results"]] == [corpus["lunch"]]
    dated = tools.semantic_search(archive, q, mode="semantic", date_from="2024-03-03", date_to="2024-03-03")
    assert [r["id"] for r in dated["results"]] == [corpus["budget"]]
    assert tools.semantic_search(archive, q, account="nonexistent")["results"] == []
    assert tools.semantic_search(archive, q, has_attachment=True)["results"] == []
    hybrid = tools.semantic_search(archive, "budget", from_="carol")
    assert [r["id"] for r in hybrid["results"]] == [corpus["budget"]]


def test_find_similar_email(archive, corpus, backend):
    _add(archive, "hype2", "Re: Feedback", "Reframing the hype paper around construct validity sounds right.", day=5)
    semantic.backfill(archive, emb.get_embedder())
    res = tools.find_similar(archive, str(corpus["hype"]))
    assert res["source"]["email_id"] == corpus["hype"]
    assert corpus["hype"] not in [r["id"] for r in res["results"]]
    assert res["results"][0]["subject"] == "Re: Feedback"
    with pytest.raises(tools.ToolInputError, match="exactly one"):
        tools.find_similar(archive, None)
    with pytest.raises(tools.ToolInputError, match="no email"):
        tools.find_similar(archive, "999999")
    with pytest.raises(tools.ToolInputError, match="no attachment"):
        tools.find_similar(archive, None, attachment_id=12345)


# ------------------------------------------------------------- privacy hook

def test_privacy_filter_is_applied_everywhere(archive, corpus, backend):
    seen = []
    hidden = corpus["hype"]

    def scope(arch, ids):
        seen.append(list(ids))
        return [i for i in ids if i != hidden]

    privacy.set_filter(scope)
    q = "construct validity reframing"
    for mode in ("semantic", "hybrid"):
        res = tools.semantic_search(archive, q, mode=mode)
        assert hidden not in [r["id"] for r in res["results"]]
    assert seen, "the filter was never consulted"
    assert any(hidden in ids for ids in seen)
    assert hidden not in [r["id"] for r in tools.search_emails(archive, q, mode="hybrid")["results"]]
    other = corpus["budget"]
    assert hidden not in [r["id"] for r in tools.find_similar(archive, str(other))["results"]]
    with pytest.raises(tools.ToolInputError):
        tools.find_similar(archive, str(hidden))


def test_default_privacy_filter_allows_all(archive):
    assert privacy.filter_allowed_message_ids(archive, [3, 1, 2]) == [3, 1, 2]


# --------------------------------------------------------- missing extra etc.

def test_graceful_message_when_extra_is_missing(archive, corpus, monkeypatch):
    monkeypatch.delenv(emb.FAKE_ENV)
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    assert not emb.semantic_installed()
    with pytest.raises(tools.ToolInputError, match=r"new-outlook-mcp\[semantic\]"):
        tools.semantic_search(archive, "anything")
    with pytest.raises(tools.ToolInputError, match="new-outlook embed"):
        tools.find_similar(archive, "1")
    res = tools.search_emails(archive, "budget", mode="hybrid")
    assert "new-outlook-mcp[semantic]" in res["note"] and res["results"]  # keyword fallback
    with pytest.raises(tools.ToolInputError, match=r"\[semantic\]"):
        tools.search_emails(archive, "budget", mode="semantic")
    # keyword mode and the sync hook are untouched
    assert semantic.embed_after_sync(archive) == 0
    assert tools.search_emails(archive, "budget")["total"] >= 1


def test_numpy_missing_is_reported(archive, monkeypatch):
    monkeypatch.setitem(sys.modules, "numpy", None)
    assert not emb.semantic_installed()
    with pytest.raises(emb.SemanticUnavailable, match=r"\[semantic\]"):
        emb.HashEmbedder().embed_query("x")


def test_not_embedded_yet_message(archive):
    _add(archive, "a", "Hello", "world")
    with pytest.raises(tools.ToolInputError, match="new-outlook embed"):
        tools.semantic_search(archive, "world")
    res = tools.search_emails(archive, "world", mode="hybrid")
    assert "new-outlook embed" in res["note"] and res["total"] == 1


def test_semantic_needs_query(archive, corpus):
    with pytest.raises(tools.ToolInputError, match="need a query"):
        tools.search_emails(archive, None, mode="semantic")
    with pytest.raises(tools.ToolInputError, match="mode must be"):
        tools.search_emails(archive, "x", mode="magic")
    assert tools.search_emails(archive, None, mode="hybrid")["total"] == 4  # no query: plain filter listing


def test_vec0_and_blob_agree(archive):
    import numpy as np

    if not _vec0_works():
        pytest.skip("sqlite-vec cannot be loaded here")
    rng = np.random.default_rng(1)
    mat = rng.normal(size=(40, 16)).astype(np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True)
    ids = list(range(100, 140))
    blob = semantic.VectorStore(archive, 16, "blob")
    vec = semantic.VectorStore(archive, 16, "vec0")
    blob.add(ids, mat)
    vec.add(ids, mat)
    q = rng.normal(size=16).astype(np.float32)
    a, b = blob.knn(q, 5), vec.knn(q, 5)
    assert [i for i, _ in a] == [i for i, _ in b]
    assert [s for _, s in a] == pytest.approx([s for _, s in b], abs=1e-4)
    assert np.allclose(blob.get([105]), vec.get([105]))
    vec.delete([105])
    blob.delete([105])
    assert vec.count() == blob.count() == 39


# --------------------------------------------------------------------- sync

def test_sync_embeds_new_mail_only_when_initialized(archive, legacy_data, tmp_path):
    kw = dict(source_paths={"legacy": legacy_data}, snapshot_base=tmp_path / "snaps")
    first = sync(archive, ["legacy"], **kw)[0]
    assert first.status == "ok" and first.embedded == 0
    assert archive.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0  # nothing without opt-in

    semantic.backfill(archive, emb.get_embedder())
    before = archive.conn.execute("SELECT COUNT(*) FROM embedded_messages").fetchone()[0]
    assert before == archive.counts()["messages"] > 0
    add_legacy_message(legacy_data, 106, "Pelican migration schedule", unix(2025, 2, 3))
    second = sync(archive, ["legacy"], **kw)[0]
    assert second.inserted == 1 and second.embedded == 1
    assert archive.conn.execute("SELECT COUNT(*) FROM embedded_messages").fetchone()[0] == before + 1
    res = tools.semantic_search(archive, "pelican migration", mode="semantic")
    assert res["results"][0]["subject"] == "Pelican migration schedule"

    add_legacy_message(legacy_data, 107, "Another one", unix(2025, 2, 4))
    third = sync(archive, ["legacy"], embed=False, **kw)[0]
    assert third.embedded == 0 and semantic.pending_count(archive) == 1


# ------------------------------------------------------------------- server

def test_server_exposes_semantic_tools(archive, corpus):
    import json

    import anyio
    from mcp import Client

    from new_outlook_mcp.server import build_server

    server = build_server(archive.path)

    async def go():
        async with Client(server) as c:
            tools_ = {t.name: t for t in (await c.list_tools()).tools}
            res = await c.call_tool("semantic_search", {"query": "construct validity", "limit": 3})
            sim = await c.call_tool("find_similar", {"email_id": str(corpus["hype"])})
            return tools_, res, sim

    listed, res, sim = anyio.run(go)
    for name in ("semantic_search", "find_similar"):
        assert listed[name].annotations.read_only_hint is True
    assert "mode" in listed["search_emails"].input_schema["properties"]

    def payload(r):
        sc = r.structured_content
        return (sc.get("result", sc) if isinstance(sc, dict) else sc) if sc is not None else json.loads(r.content[0].text)

    assert payload(res)["results"][0]["id"] == corpus["hype"]
    assert payload(sim)["count"] >= 1


# ------------------------------------------------------------ real model

@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("RUN_REAL_MODEL") != "1", reason="set RUN_REAL_MODEL=1 to load the real model")
def test_real_model_english_czech(monkeypatch):
    monkeypatch.delenv(emb.FAKE_ENV)
    e = emb.get_embedder(allow_download=True)
    assert e.dim == 384
    docs = e.embed_passages(["Návrh na přeformulování článku o hype", "Lunch menu for Thursday"])
    q = e.embed_query("suggestion to reframe the hype paper")
    assert float(docs[0] @ q) > float(docs[1] @ q)


def test_real_privacy_rules_hide_semantic_results(archive, corpus, backend):
    """The default filter is the configured privacy scopes, not allow-all."""
    rules = privacy.Rules()
    rules.add("senders", ["alice@example.org"])
    privacy.save_rules(rules)
    try:
        for mode in ("semantic", "hybrid"):
            res = semantic.semantic_search(archive, "reframing the hype paper", mode=mode)
            assert corpus["hype"] not in {r["id"] for r in res["results"]}, mode
        sim = semantic.find_similar(archive, email_id=str(corpus["budget"]))
        assert corpus["hype"] not in {r["id"] for r in sim["results"]}
        with pytest.raises(tools.ToolInputError):
            semantic.find_similar(archive, email_id=str(corpus["hype"]))
    finally:
        privacy.save_rules(privacy.Rules())


def test_purge_removes_chunks_and_vectors(archive, corpus, backend):
    table, col = ("chunk_vec", "rowid") if backend == "vec0" else ("chunk_vectors", "chunk_id")
    ids = [r[0] for r in archive.conn.execute("SELECT id FROM chunks WHERE message_pk = ?", (corpus["hype"],))]
    assert ids
    rules = privacy.Rules()
    rules.add("senders", ["alice@example.org"])
    privacy.purge(archive, rules)
    assert not archive.conn.execute("SELECT 1 FROM chunks WHERE message_pk = ?", (corpus["hype"],)).fetchone()
    marks = ",".join("?" * len(ids))
    assert archive.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} IN ({marks})", ids).fetchone()[0] == 0
    other = semantic.semantic_search(archive, "quarterly budget", mode="semantic")
    assert corpus["budget"] in {r["id"] for r in other["results"]}


def test_boilerplate_lines_need_many_messages(archive):
    footer = "Please book an appointment with me here."
    for i in range(semantic.BOILERPLATE_MIN_MESSAGES):
        _add(archive, f"news{i}", f"Update {i}", f"News item number {i} is here.\n\n{footer}", day=1 + i % 20)
    _add(archive, "rare", "Rare", "This sentence appears in one message only.")
    boiler = semantic.boilerplate_lines(archive)
    assert "please book an appointment with me here" in boiler
    assert "this sentence appears in one message only" not in boiler
    pk = archive.conn.execute("SELECT id FROM messages WHERE dedup_key LIKE '%news0%'").fetchone()[0]
    bodies = [c.text for c in semantic.build_chunks(archive, pk, boiler) if c.kind == "body"]
    assert bodies == ["News item number 0 is here."]


def test_hybrid_keyword_query_drops_function_words():
    assert semantic._fts_query("the invoice from my physiotherapist") == '"invoice" OR "physiotherapist"'
    assert semantic._fts_query("factuur van de kinesist") == '"factuur" OR "kinesist"'
    assert semantic._fts_query("the from") == '"the" OR "from"'  # nothing else left: keep them


def test_status_flags_embeddings_from_older_chunking_rules(archive, corpus):
    assert semantic.status(archive)["chunking_outdated"] is False
    archive.conn.execute("UPDATE meta SET value = '1' WHERE key = 'embed_chunking'")
    assert semantic.status(archive)["chunking_outdated"] is True
