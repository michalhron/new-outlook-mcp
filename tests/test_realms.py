"""Realms: work and private mail in one archive, and the server fence. Synthetic data only."""

from __future__ import annotations

import json

import anyio
import pytest
from mcp import Client
from test_eml import HEY, write_eml

from new_outlook_mcp import caltools, cli, privacy, realms, tools
from new_outlook_mcp.config import load_config, save_config
from new_outlook_mcp.importers import eml
from new_outlook_mcp.server import build_server
from new_outlook_mcp.sync import sync

WORK = "me@uni.example.edu"  # the synthetic legacy account


@pytest.fixture(autouse=True)
def _no_fence():
    realms.set_fence(None)
    yield
    realms.set_fence(None)


@pytest.fixture
def both(loaded, tmp_path):
    """The synthetic legacy archive (work) plus two HEY messages (private)."""
    folder = tmp_path / "hey-archive"
    write_eml(folder, "101.eml", subject="Flat contract", body="The landlord sent the signed contract zebra.",
              mid="flat-1@hey.example", attach=("notes.txt", b"Deposit is due on Friday."))
    write_eml(folder, "102.eml", subject="Dinner", body="Shall we book the quince place?", mid="dinner-1@hey.example")
    eml.add_folder("hey", str(folder), HEY)
    res = sync(loaded, ["eml"], snapshot_base=tmp_path / "snaps")[0]
    assert res.inserted == 2
    realms.add("work", [WORK])
    realms.add("private", [HEY])
    return loaded


def _pk(archive, mid):
    return archive.conn.execute("SELECT id FROM messages WHERE message_id = ?", (mid,)).fetchone()[0]


def _accounts(results):
    return {r["account"] for r in results}


# ----------------------------------------------------------------- config

def test_realm_of_matching():
    table = {"work": ["me@uni.example.edu", "@uni.example.edu"], "private": ["@hey.example", "boss@uni.example.edu"],
             "files": "work"}
    assert realms.realm_of("Me@Uni.Example.edu", table) == "work"
    assert realms.realm_of("someone@dept.uni.example.edu", table) == "work"  # subdomain
    assert realms.realm_of("boss@uni.example.edu", table) == "private"  # exact wins over domain
    assert realms.realm_of("me@hey.example", table) == "private"
    assert realms.realm_of("me@nothey.example", table) is None
    assert realms.realm_of(None, table) is None


def test_add_moves_between_realms_and_keeps_other_sections():
    cfg = load_config()
    cfg["my_addresses"] = ["me@uni.example.edu"]
    save_config(cfg)
    assert realms.add("work", ["X@Example.org"]) == 1
    assert realms.add("work", ["x@example.org"]) == 0
    assert realms.add("private", ["x@example.org"]) == 1
    table = realms.load_realms()
    assert table["work"] == [] and table["private"] == ["x@example.org"]
    assert load_config()["my_addresses"] == ["me@uni.example.edu"]
    assert realms.remove(["x@example.org"]) == 1 and "realms" not in load_config()


@pytest.mark.parametrize("bad", [{"work": "x", "colour": ["y"]}, {"work": [1]}, {"files": "home"}])
def test_bad_config_fails_closed(loaded, bad):
    cfg = load_config()
    cfg["realms"] = bad
    save_config(cfg)
    with pytest.raises(realms.RealmConfigError):
        realms.load_realms()
    with pytest.raises((realms.RealmConfigError, privacy.PrivacyConfigError)):
        tools.search_emails(loaded, "zebra")
    realms.set_fence("work")
    with pytest.raises((realms.RealmConfigError, privacy.PrivacyConfigError)):
        tools.get_email(loaded, "alpha-1@example.org")


def test_default_view_and_fence(monkeypatch):
    monkeypatch.delenv(realms.FENCE_ENV, raising=False)
    assert realms.default_view() == "all"  # nothing configured: searches cover everything
    realms.add("work", [WORK])
    assert realms.default_view() == "work"
    cfg = load_config()
    cfg["realms"]["default"] = "all"
    save_config(cfg)
    assert realms.default_view() == "all"
    assert realms.default_fence() == "all"  # the hard fence is opt-in
    assert realms.default_fence("private") == "private"
    monkeypatch.setenv(realms.FENCE_ENV, "all")
    assert realms.default_fence() == "all"
    with pytest.raises(realms.RealmConfigError):
        realms.default_fence("home")


# ------------------------------------------------------------------ labels and filter

def test_searches_default_to_work_and_carry_realm(both):
    out = tools.search_emails(both, "zebra")
    assert _accounts(out["results"]) == {WORK} and out["realm_searched"] == "work" and "realm_note" in out
    out = tools.search_emails(both, "zebra", realm="all")
    assert {(r["account"], r["realm"]) for r in out["results"]} == {(WORK, "work"), (HEY, "private")}
    assert "realm_note" not in out
    assert _accounts(tools.search_emails(both, "zebra", realm="private")["results"]) == {HEY}
    assert _accounts(tools.list_recent(both, limit=50)["results"]) == {WORK}
    assert _accounts(tools.list_recent(both, realm="private", limit=50)["results"]) == {HEY}
    # opening a private message by id works without a fence
    pk = _pk(both, "flat-1@hey.example")
    assert tools.get_email(both, str(pk))["account"] == HEY
    with pytest.raises(tools.ToolInputError):
        tools.search_emails(both, "zebra", realm="home")


def test_no_realm_label_without_config(loaded):
    out = tools.search_emails(loaded, "zebra")
    assert "realm" not in out["results"][0] and "realm_searched" not in out


# ------------------------------------------------------------------ the fence

def test_work_fence_hides_private_everywhere(both):
    hey_pk = _pk(both, "flat-1@hey.example")
    realms.set_fence("work")
    assert _accounts(tools.search_emails(both, "zebra", realm="all")["results"]) == {WORK}
    assert _accounts(tools.search_emails(both, None, limit=200, realm="all")["results"]) == {WORK}
    assert _accounts(tools.list_recent(both, limit=200, realm="all")["results"]) == {WORK}
    assert tools.search_emails(both, "zebra", realm="private")["results"] == []
    for fn in (lambda: tools.get_email(both, str(hey_pk)), lambda: tools.get_email(both, "<flat-1@hey.example>"),
               lambda: tools.get_thread(both, str(hey_pk)), lambda: tools.list_attachments(both, str(hey_pk))):
        with pytest.raises(tools.ToolInputError):
            fn()
    att_id = both.conn.execute("SELECT id FROM attachments WHERE message_pk = ?", (hey_pk,)).fetchone()[0]
    with pytest.raises(tools.ToolInputError):
        tools.get_attachment(both, att_id)
    folders = tools.list_folders(both)["folders"]
    assert folders and {f["account"] for f in folders} == {WORK}
    status = tools.archive_status(both)
    assert status["counts"]["accounts"] == 1 and status["privacy"]["realm"] == "work"
    assert {a["account"] for a in status["accounts"]} == {WORK}
    assert caltools.list_calendar_events(both, "2026-10-01", "2026-10-31")["events"]


def test_private_fence_shows_only_private(both):
    realms.set_fence("private")
    assert _accounts(tools.search_emails(both, None, limit=200, realm="all")["results"]) == {HEY}
    assert tools.search_emails(both, None, limit=200)["results"] == []  # default view is work
    assert caltools.list_calendar_events(both, "2026-10-01", "2026-10-31")["events"] == []
    pk = tools.search_emails(both, None, limit=200, realm="private")["results"][0]["id"]
    assert tools.get_email(both, str(pk))["account"] == HEY


def test_unassigned_accounts_are_hidden_behind_a_fence(both):
    realms.remove([HEY])  # HEY now has no realm
    realms.set_fence("work")
    assert HEY not in _accounts(tools.search_emails(both, None, limit=200, realm="all")["results"])
    realms.set_fence("private")
    assert tools.search_emails(both, None, limit=200, realm="all")["results"] == []
    realms.set_fence("all")
    assert HEY in _accounts(tools.search_emails(both, None, limit=200, realm="all")["results"])
    assert HEY not in _accounts(tools.search_emails(both, None, limit=200)["results"])  # not in the work view


def test_fence_never_changes_import_or_purge(both, tmp_path):
    realms.set_fence("work")
    folder = tmp_path / "hey-archive"
    write_eml(folder, "103.eml", subject="Bike", body="Ready on Monday.", mid="bike-1@hey.example")
    assert sync(both, ["eml"], snapshot_base=tmp_path / "snaps")[0].inserted == 1
    out = privacy.purge(both, privacy.load_rules())
    assert out["messages"] == 0
    realms.set_fence(None)
    assert both.conn.execute("SELECT COUNT(*) FROM messages m JOIN accounts a ON a.id = m.account_id"
                             " WHERE a.name = ?", (HEY,)).fetchone()[0] == 3


def test_unlinked_orphan_files_follow_the_files_realm(both):
    row = {"message_pk": None, "filename": "report.pdf", "text": "quarterly"}
    realms.set_fence("work")
    assert not privacy.orphan_hidden(both.conn, row)
    realms.set_fence("private")
    assert privacy.orphan_hidden(both.conn, row)


def test_semantic_search_respects_the_fence(both, monkeypatch):
    pytest.importorskip("numpy")
    from new_outlook_mcp import embedder as emb
    from new_outlook_mcp import semantic

    monkeypatch.setenv(emb.FAKE_ENV, "fake")
    semantic.backfill(both, emb.get_embedder())
    hey_pk, work_pk = _pk(both, "flat-1@hey.example"), _pk(both, "alpha-1@example.org")
    assert HEY not in _accounts(tools.semantic_search(both, "landlord contract", mode="semantic")["results"])
    assert HEY in _accounts(tools.semantic_search(both, "landlord contract", mode="semantic", realm="all")["results"])
    assert HEY not in _accounts(tools.find_similar(both, str(work_pk))["results"])
    assert _accounts(tools.find_similar(both, str(hey_pk), realm="private")["results"]) <= {HEY}
    realms.set_fence("work")
    for mode in ("semantic", "hybrid"):
        found = tools.semantic_search(both, "landlord contract", mode=mode, realm="all")["results"]
        assert HEY not in _accounts(found)
    with pytest.raises(tools.ToolInputError):
        tools.find_similar(both, str(hey_pk))
    assert HEY not in _accounts(tools.find_similar(both, str(work_pk), realm="all")["results"])


# ------------------------------------------------------------------ server and CLI

def _payload(result) -> dict:
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


def test_server_realm_option(both):
    def search(fence, args):
        server = build_server(both.path, realm=fence)

        async def go():
            async with Client(server) as c:
                return _payload(await c.call_tool("search_emails", {"query": "zebra", **args}))

        return _accounts(anyio.run(go)["results"])

    assert search("all", {}) == {WORK}
    assert search("all", {"realm": "private"}) == {HEY}
    assert search("all", {"realm": "all"}) == {WORK, HEY}
    assert search("work", {"realm": "all"}) == {WORK}
    assert search("private", {"realm": "all"}) == {HEY}


def test_cli_realm(both, capsys):
    assert cli.main(["--db", str(both.path), "realm", "show"]) == 0
    out = capsys.readouterr().out
    assert f"{HEY}: private" in out and "searches cover by default: work" in out and "server fence: all" in out
    assert cli.main(["realm", "add", "home", "x@example.org"]) == 2
    assert cli.main(["realm", "remove", HEY]) == 0
    assert realms.realm_of(HEY) is None


# ----------------------------------------------------------- merged mail

@pytest.fixture
def merged(both, tmp_path):
    """A HEY copy of the work message alpha-1, merged into it."""
    write_eml(tmp_path / "hey-archive", "103.eml", subject="copy", body="copy", mid="alpha-1@example.org")
    res = sync(both, ["eml"], snapshot_base=tmp_path / "snaps")[0]
    assert res.merged == 1
    return both


def test_merged_message_belongs_to_both_realms(merged):
    pk = _pk(merged, "alpha-1@example.org")
    assert realms.realms_of_message(merged.conn, pk) == ["work", "private"]
    for realm in ("work", "private", "all"):
        ids = {r["id"] for r in tools.search_emails(merged, realm=realm, limit=200)["results"]}
        assert pk in ids, realm
    hit = next(r for r in tools.search_emails(merged, realm="private", limit=200)["results"] if r["id"] == pk)
    assert hit["realm"] == "work+private"


def test_fence_shows_merged_message_on_both_sides(merged):
    pk = _pk(merged, "alpha-1@example.org")
    hey_only = _pk(merged, "flat-1@hey.example")
    realms.set_fence("private")
    ids = {r["id"] for r in tools.search_emails(merged, realm="all", limit=200)["results"]}
    assert pk in ids and hey_only in ids
    realms.set_fence("work")
    ids = {r["id"] for r in tools.search_emails(merged, realm="all", limit=200)["results"]}
    assert pk in ids and hey_only not in ids
