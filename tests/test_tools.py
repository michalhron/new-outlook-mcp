from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from new_outlook_mcp import tools
from new_outlook_mcp.db import normalize_subject
from new_outlook_mcp.model import MessageRecord


def test_search_fts_and_snippet(loaded):
    res = tools.search_emails(loaded, "zebra")
    assert res["total"] == 1
    assert "[zebra]" in res["results"][0]["snippet"]


def test_search_diacritics_and_prefix(archive):
    from datetime import datetime, timezone

    with archive.transaction():
        archive.upsert(MessageRecord(source="t", source_key="1", subject="Schůzka v Brně",
                                     date=datetime(2026, 1, 1, tzinfo=timezone.utc), body_text="Přijďte včas"))
    assert tools.search_emails(archive, "brne")["total"] == 1
    assert tools.search_emails(archive, "prijd*")["total"] == 1


def test_search_invalid_syntax_falls_back(loaded):
    res = tools.search_emails(loaded, 'budget "unclosed')
    assert "note" in res


def test_search_filters(loaded):
    assert tools.search_emails(loaded, None, from_="erin")["total"] == 1
    assert tools.search_emails(loaded, None, to="carol@")["total"] == 1
    assert tools.search_emails(loaded, None, folder="projects")["total"] == 1
    assert tools.search_emails(loaded, None, account="uni.example")["total"] == 4
    assert tools.search_emails(loaded, None, has_attachment=True)["total"] == 2
    r = tools.search_emails(loaded, None, date_from="2024-03-06", date_to="2024-05-10")
    assert {x["subject"] for x in r["results"]} == {"RE: Quarterly budget review", "Field trip logistics"}
    assert tools.search_emails(loaded, "budget", date_to="2024-03-05")["total"] == 1
    with pytest.raises(tools.ToolInputError):
        tools.search_emails(loaded, None, date_from="March")


def test_search_like_wildcards_are_literal(loaded):
    assert tools.search_emails(loaded, None, from_="%")["total"] == 0


def test_paging(loaded):
    r1 = tools.search_emails(loaded, None, limit=2)
    assert r1["total"] == 4 and r1["next_offset"] == 2
    r2 = tools.search_emails(loaded, None, limit=2, offset=2)
    assert "next_offset" not in r2
    assert {x["id"] for x in r1["results"]}.isdisjoint({x["id"] for x in r2["results"]})


def test_list_recent_newest_first(loaded):
    dates = [r["date"] for r in tools.list_recent(loaded, limit=10)["results"]]
    assert dates == sorted(dates, reverse=True)


def test_get_email_body_paging(archive):
    with archive.transaction():
        pk = archive.upsert(MessageRecord(source="t", source_key="x", body_text="a" * 250 + "b" * 250)).pk
    first = tools.get_email(archive, str(pk), max_chars=300)
    assert first["truncated"] and first["next_offset"] == 300 and first["body_length"] == 500
    rest = tools.get_email(archive, str(pk), offset=first["next_offset"], max_chars=300)
    assert not rest["truncated"] and rest["body"] == "b" * 200


def test_get_email_unknown(loaded):
    with pytest.raises(tools.ToolInputError):
        tools.get_email(loaded, "999999")


def test_get_thread_by_headers(loaded):
    t = tools.get_thread(loaded, "alpha-2@example.net")
    assert t["thread_size"] == 2
    assert [m["subject"] for m in t["messages"]] == ["Quarterly budget review", "RE: Quarterly budget review"]
    t2 = tools.get_thread(loaded, "alpha-1@example.org")
    assert t2["thread_size"] == 2


def test_get_thread_subject_fallback(archive):
    from datetime import datetime, timezone

    with archive.transaction():
        a = archive.upsert(MessageRecord(source="t", source_key="1", subject="Room booking",
                                         date=datetime(2026, 2, 1, tzinfo=timezone.utc))).pk
        archive.upsert(MessageRecord(source="t", source_key="2", subject="AW: Room booking",
                                     date=datetime(2026, 2, 2, tzinfo=timezone.utc)))
        archive.upsert(MessageRecord(source="t", source_key="3", subject="Room booking",
                                     date=datetime(2020, 1, 1, tzinfo=timezone.utc)))
    t = tools.get_thread(archive, str(a))
    assert t["matched_by"] == "subject" and t["thread_size"] == 2


def test_normalize_subject():
    assert normalize_subject("RE: Fwd: AW: Hello  world") == "hello world"
    assert normalize_subject("Re[2]: x") == "x"


def test_dedup_hash_fallback(archive):
    from datetime import datetime, timezone

    d = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    with archive.transaction():
        r1 = archive.upsert(MessageRecord(source="a", source_key="1", subject="S", from_addr="X@EXAMPLE.org", date=d))
        r2 = archive.upsert(MessageRecord(source="b", source_key="9", subject="S", from_addr="x@example.org", date=d,
                                          body_text="filled later"))
    assert r1.inserted and not r2.inserted and r1.pk == r2.pk
    assert tools.get_email(archive, str(r1.pk))["body"] == "filled later"


def test_list_folders_and_status(loaded):
    folders = {f["folder"]: f["messages"] for f in tools.list_folders(loaded)["folders"]}
    assert folders == {"Inbox": 2, "Inbox/Projects": 1, "Sent Items": 1}
    st = tools.archive_status(loaded)
    cov = {c["source"]: c for c in st["coverage_by_source"]}
    assert cov["legacy"]["n"] == 5
    assert st["last_sync_by_source"][0]["status"] == "ok"


def test_build_mailto():
    url = tools.build_mailto(["a@example.org", "b@example.org"], cc="c@example.org", subject="Hi & bye",
                             body="Line 1\nŘádek 2")
    parts = urlsplit(url)
    assert parts.scheme == "mailto" and unquote(parts.path) == "a@example.org,b@example.org"
    q = parse_qs(parts.query)
    assert q["subject"] == ["Hi & bye"] and q["cc"] == ["c@example.org"]
    assert q["body"] == ["Line 1\r\nŘádek 2"]


def test_build_mailto_rejects_header_injection():
    with pytest.raises(tools.ToolInputError):
        tools.build_mailto("a@example.org\r\nBcc: x@example.org")
    with pytest.raises(tools.ToolInputError):
        tools.build_mailto("not-an-address")


def test_create_draft_uses_opener_and_never_sends():
    opened = []
    res = tools.create_draft(["a@example.org"], subject="S", body="B", opener=opened.append)
    assert res["opened"] and opened[0].startswith("mailto:a@example.org?")
    with pytest.raises(tools.ToolInputError):
        tools.create_draft(["a@example.org"], body="x" * 40000, opener=opened.append)
