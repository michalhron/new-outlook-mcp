"""Privacy scopes: excluded mail and events are unreachable through every tool. Synthetic data only."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from mcp import Client

from new_outlook_mcp import caltools, cli, config, feeds, privacy, tools
from new_outlook_mcp.db import Archive
from new_outlook_mcp.importers.base import Importer
from new_outlook_mcp.model import AttachmentInfo, MessageRecord
from new_outlook_mcp.server import build_server
from new_outlook_mcp.sync import run_import, sync

PRAGUE = "Europe/Prague"
URL = "https://outlook.example.invalid/owa/calendar/abc123SECRET/reachcalendar.ics"


def _payload(result) -> dict:
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


def _set_rules(**kw) -> privacy.Rules:
    rules = privacy.Rules()
    for kind, values in kw.items():
        rules.add(kind, values)
    privacy.save_rules(rules)
    return rules


def _mail_id(archive, mid: str) -> int:
    return archive.conn.execute("SELECT id FROM messages WHERE message_id = ?", (mid,)).fetchone()[0]


# Message-ID, a word only that message contains, and the rule that should hide exactly it.
CASES = {
    "accounts": ("me@uni.example.edu", None),  # hides everything: the legacy account
    "folders": ("PROJECTS", "alpha-1@example.org"),
    "senders": ("Ada@Example.org", "alpha-1@example.org"),
    "domains": ("example.net", "alpha-2@example.net"),
    "subject_keywords": ("FIELD TRIP", "beta-1@example.com"),
    "attachment_names": ("*.CSV", "alpha-1@example.org"),
    "recipients": ("frank@example.org", None),  # message 104 has no Message-ID
}
WORDS = {"alpha-1@example.org": "zebra", "alpha-2@example.net": "looks", "beta-1@example.com": "pelican", None: "canteen"}
ALL_MIDS = ["alpha-1@example.org", "alpha-2@example.net", "beta-1@example.com", None]


def _hidden_set(kind: str) -> set:
    if kind == "accounts":
        return set(ALL_MIDS)
    if kind == "recipients":
        return {None}
    return {CASES[kind][1]}


@pytest.fixture
def server(loaded):
    return build_server(loaded.path)


def _run_all_tools(server, loaded, hidden_mids: set):
    """Call every mail tool through MCP. Returns {name: payload or error text}."""
    ids = {mid: loaded.conn.execute(
        "SELECT id FROM messages WHERE message_id IS ?", (mid,)).fetchone()[0] for mid in ALL_MIDS}
    att_ids = {mid: [r[0] for r in loaded.conn.execute("SELECT id FROM attachments WHERE message_pk = ?", (pk,))]
               for mid, pk in ids.items()}
    out: dict = {}

    async def go():
        async with Client(server) as c:
            async def call(name, args):
                res = await c.call_tool(name, args)
                return res.content[0].text if res.is_error else _payload(res)

            for mid in ALL_MIDS:
                tag = str(mid)
                out["kw:" + tag] = await call("search_emails", {"query": WORDS[mid]})
                out["get_id:" + tag] = await call("get_email", {"email_id": str(ids[mid])})
                if mid:
                    out["get_mid:" + tag] = await call("get_email", {"email_id": mid})
                    out["get_mid_br:" + tag] = await call("get_email", {"email_id": f"<{mid.upper()}>"})
                out["atts:" + tag] = await call("list_attachments", {"email_id": str(ids[mid]), "include_inline": True})
                for n, a in enumerate(att_ids[mid]):
                    for mode in ("text", "path"):
                        out[f"att{n}-{mode}:{tag}"] = await call("get_attachment", {"attachment_id": a, "mode": mode})
                out["thread:" + tag] = await call("get_thread", {"email_id": str(ids[mid])})
            out["filters"] = [
                await call("search_emails", {"sender": "ada", "limit": 200}),
                await call("search_emails", {"sender": "bob", "limit": 200}),
                await call("search_emails", {"recipient": "frank", "limit": 200}),
                await call("search_emails", {"folder": "Projects", "limit": 200}),
                await call("search_emails", {"account": "uni", "limit": 200}),
                await call("search_emails", {"has_attachment": True, "limit": 200}),
                await call("search_emails", {"attachment_name": "csv", "limit": 200}),
                await call("search_emails", {"attachment_name": "report", "limit": 200}),
                await call("search_emails", {"date_from": "2020-01-01", "sort": "date_asc", "limit": 200}),
                await call("search_emails", {"query": "budget OR lunch OR field OR thanks", "limit": 200}),
            ]
            out["recent"] = await call("list_recent", {"limit": 200})
            out["recent_folder"] = await call("list_recent", {"limit": 200, "folder": "Inbox"})
            out["folders"] = await call("list_folders", {})
            out["status"] = await call("archive_status", {})

    anyio.run(go)
    return out


def _all_text(obj) -> str:
    return json.dumps(obj, default=str).lower()


@pytest.mark.parametrize("kind", list(CASES))
def test_rule_hides_mail_from_every_tool(kind, server, loaded):
    before = _run_all_tools(server, loaded, set())
    assert "zebra" in _all_text(before["kw:alpha-1@example.org"])  # sanity: visible without rules
    value = CASES[kind][0]
    # Rules are added after the import and after the server started: query-time filtering.
    _set_rules(**{kind: [value]})
    hidden = _hidden_set(kind)
    out = _run_all_tools(server, loaded, hidden)

    secrets = {
        "alpha-1@example.org": ["zebra", "budget.csv", "alpha-1@example.org"],
        "alpha-2@example.net": ["alpha-2@example.net", "looks"],
        "beta-1@example.com": ["pelican", "field trip", "report.pdf", "beta-1@example.com"],
        None: ["canteen", "lunch?"],
    }
    for mid in ALL_MIDS:
        tag = str(mid)
        if mid in hidden:
            assert out["kw:" + tag]["total"] == 0, tag
            assert isinstance(out["get_id:" + tag], str) and "no email" in out["get_id:" + tag]
            if mid:
                assert "no email" in out["get_mid:" + tag] and "no email" in out["get_mid_br:" + tag]
            assert "no email" in out["atts:" + tag]
            assert "no email" in out["thread:" + tag]
            for key, val in out.items():
                if key.startswith("att") and key.endswith(":" + tag) and key != "atts:" + tag:
                    assert "no attachment" in val, key
        else:
            assert out["kw:" + tag]["total"] == 1, tag
            assert out["get_id:" + tag]["body"], tag

    # Nothing a hidden message holds may appear anywhere in aggregated output.
    aggregated = _all_text({"filters": out["filters"], "recent": out["recent"], "recent_folder": out["recent_folder"],
                            "folders": out["folders"], "status": out["status"]})
    for mid in hidden:
        for word in secrets[mid]:
            assert word not in aggregated, (kind, word)

    # Counts reflect only what is visible.
    remaining = len(ALL_MIDS) - len(hidden)
    assert out["recent"]["total"] == remaining
    assert out["filters"][8]["total"] == remaining
    assert out["status"]["counts"]["messages"] == remaining
    assert sum(f["messages"] for f in out["folders"]["folders"]) + out["folders"].get("messages_without_folder", 0) == remaining
    pv = out["status"]["privacy"]
    assert pv["active"] is True and pv["rules"][kind] == 1
    assert pv["hidden_messages"] == len(hidden)
    assert sum(c["n"] for c in out["status"]["coverage_by_source"]) <= remaining + 1  # 105 is a second source of 101


def test_folder_rule_hides_folder_and_counts(loaded):
    names = {f["folder"] for f in tools.list_folders(loaded)["folders"]}
    assert names == {"Inbox", "Inbox/Projects", "Sent Items"}
    _set_rules(folders=["projects"])
    res = tools.list_folders(loaded)
    assert {f["folder"] for f in res["folders"]} == {"Inbox", "Sent Items"}
    assert "id" not in res["folders"][0]
    assert tools.search_emails(loaded, folder="Proj")["total"] == 0


def test_account_rule_hides_folders_and_calendar(loaded):
    _set_rules(accounts=["ME@uni.example.edu"])
    assert tools.list_folders(loaded)["folders"] == []
    assert tools.list_recent(loaded)["total"] == 0
    assert caltools.list_calendar_events(loaded, "2026-10-01", "2026-10-31")["events"] == []
    st = tools.archive_status(loaded)
    assert st["counts"]["messages"] == 0 and st["counts"]["events"] == 0 and st["counts"]["folders"] == 0
    assert st["counts"]["attachments"] == 0 and st["counts"]["event_instances"] == 0
    assert st["coverage_by_source"] == [] and st["calendar_coverage_by_source"] == []


def test_thread_drops_hidden_members(loaded):
    thread = tools.get_thread(loaded, "alpha-2@example.net")
    assert thread["thread_size"] == 2
    _set_rules(senders=["ada@example.org"])
    thread = tools.get_thread(loaded, "alpha-2@example.net")
    assert thread["thread_size"] == 1 and thread["returned"] == 1
    assert "ada@example.org" not in _all_text(thread) or "bob@example.net" in _all_text(thread)
    assert [m["subject"] for m in thread["messages"]] == ["RE: Quarterly budget review"]
    assert "zebra" not in _all_text(thread)


def test_attachment_name_rule_hides_whole_message(loaded):
    att_id = loaded.conn.execute("SELECT id FROM attachments WHERE filename = 'budget.csv'").fetchone()[0]
    _set_rules(attachment_names=["BUDGET*"])
    assert tools.search_emails(loaded, "zebra")["total"] == 0
    assert tools.search_emails(loaded, attachment_name="budget")["total"] == 0
    with pytest.raises(tools.ToolInputError):
        tools.get_attachment(loaded, att_id)
    with pytest.raises(tools.ToolInputError):
        tools.get_email(loaded, "alpha-1@example.org")


# --------------------------------------------------------------- meeting_prep

def _prep_message(archive):
    rec = MessageRecord(
        source="legacy", source_key="900", message_id="prep-1@example.org", subject="Prep SECRETPREP exam",
        from_addr="ada@example.org", to=["frank@example.org"], folder="Grades", account="other@x.org",
        date=datetime.now(timezone.utc) - timedelta(days=5), body_text="prep notes for the kickoff",
        attachments=[AttachmentInfo(filename="grades.xlsx", content_type="application/octet-stream")])
    with archive.transaction():
        archive.upsert(rec)


PREP_RULES = {
    "accounts": "other@x.org", "folders": "grades", "senders": "ada@example.org", "domains": "example.org",
    "subject_keywords": "secretprep", "attachment_names": "GRADES*", "recipients": "frank@example.org",
}


@pytest.mark.parametrize("kind", list(PREP_RULES))
def test_meeting_prep_skips_hidden_threads(kind, loaded):
    _prep_message(loaded)
    pk = loaded.conn.execute("SELECT id FROM events WHERE subject = 'Project kickoff'").fetchone()[0]
    base = caltools.meeting_prep(loaded, pk, days_back=60)
    assert [t["subject"] for t in base["recent_threads"]] == ["Prep SECRETPREP exam"]
    _set_rules(**{kind: [PREP_RULES[kind]]})
    prep = caltools.meeting_prep(loaded, pk, days_back=60)
    assert prep["recent_threads"] == [] and "secretprep" not in _all_text(prep)


# ------------------------------------------------------------------- calendar

EVENT_RULES = {
    "subject_keywords": "KICKOFF",
    "senders": "erin@example.com",
    "domains": "example.com",
}


def _calendar_outputs(server, loaded):
    pk = loaded.conn.execute("SELECT id FROM events WHERE subject = 'Project kickoff'").fetchone()[0]
    out: dict = {}

    async def go():
        async with Client(server) as c:
            async def call(name, args):
                res = await c.call_tool(name, args)
                return res.content[0].text if res.is_error else _payload(res)

            out["list"] = await call("list_calendar_events", {"start": "2026-10-14", "end": "2026-10-14", "timezone": PRAGUE})
            out["get"] = await call("get_calendar_event", {"event_id": str(pk)})
            out["search"] = await call("search_calendar", {"query": "kickoff OR erin OR agenda OR scope"})
            out["freebusy"] = await call("calendar_freebusy", {"start": "2026-10-14", "end": "2026-10-14", "timezone": PRAGUE})
            out["slots"] = await call("find_free_slots", {"duration_minutes": 60, "start": "2026-10-14",
                                                          "end": "2026-10-14", "timezone": PRAGUE, "step_minutes": 60})
            out["prep"] = await call("meeting_prep", {"event_id": str(pk)})
            out["status"] = await call("archive_status", {})

    anyio.run(go)
    return out


@pytest.mark.parametrize("kind", list(EVENT_RULES))
def test_event_rules_hide_events_from_every_calendar_tool(kind, server, loaded):
    base = _calendar_outputs(server, loaded)
    assert base["list"]["events"][0]["subject"] == "Project kickoff"
    assert base["freebusy"]["busy"] and not any(s["start"].startswith("2026-10-14T10") for s in base["slots"]["slots"])
    _set_rules(**{kind: [EVENT_RULES[kind]]})
    out = _calendar_outputs(server, loaded)
    assert out["list"]["events"] == []
    assert "no calendar event" in out["get"]
    assert out["search"]["count"] == 0
    assert out["freebusy"]["busy"] == []
    assert any(s["start"].startswith("2026-10-14T10") for s in out["slots"]["slots"])
    assert "no calendar event" in out["prep"]
    text = _all_text(out)
    for word in ("kickoff", "room 4.12", "erin", "teams.microsoft", "agenda"):
        assert word not in text, word
    assert out["status"]["counts"]["events"] == 4
    assert out["status"]["privacy"]["hidden_events"] == 1


def test_modified_occurrences_are_hidden_with_their_series(loaded):
    assert privacy.count_hidden(loaded.conn, privacy.Rules(subject_keywords=["weekly team sync (moved)"]))["events"] == 2


# ---------------------------------------------------------------- import time

def test_import_time_exclusion(tmp_path, legacy_data):
    _set_rules(subject_keywords=["lunch", "kickoff"], folders=["deleteditems"])
    a = Archive(tmp_path / "fresh.db")
    res = sync(a, ["legacy"], source_paths={"legacy": legacy_data}, snapshot_base=tmp_path / "snaps")[0]
    assert res.status == "ok" and res.excluded == 1 and res.events_excluded == 2  # kickoff and "Cancelled lunch talk"
    assert res.details == {"excluded": 1, "events_excluded": 2}
    assert a.counts()["messages"] == 4 - 1 and a.counts()["events"] == 3
    assert a.conn.execute("SELECT COUNT(*) FROM messages WHERE subject LIKE 'Lunch%'").fetchone()[0] == 0
    assert a.conn.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'canteen'").fetchone()[0] == 0
    assert a.conn.execute("SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH 'kickoff'").fetchone()[0] == 0
    assert a.conn.execute("SELECT COUNT(*) FROM attendees WHERE addr = 'ada@example.org'").fetchone()[0] == 0
    run = a.last_runs()[0]
    assert json.loads(run["details_json"]) == {"excluded": 1, "events_excluded": 2}
    a.close()


def test_import_exclusion_applies_to_every_rule_type(tmp_path, legacy_data):
    for kind, value in {**{k: v[0] for k, v in CASES.items()}, "subject_keywords": "lunch"}.items():
        _set_rules(**{kind: [value]})
        a = Archive(tmp_path / f"{kind}.db")
        res = sync(a, ["legacy"], source_paths={"legacy": legacy_data}, snapshot_base=tmp_path / "snaps")[0]
        assert res.excluded >= 1, kind
        a.close()
    _set_rules(attachment_names=["*.csv"])
    a = Archive(tmp_path / "att.db")
    sync(a, ["legacy"], source_paths={"legacy": legacy_data}, snapshot_base=tmp_path / "snaps")
    assert a.conn.execute("SELECT COUNT(*) FROM attachments WHERE filename = 'budget.csv'").fetchone()[0] == 0
    a.close()


def test_removing_a_rule_brings_mail_back_on_next_sync(tmp_path, legacy_data):
    _set_rules(subject_keywords=["lunch"])
    a = Archive(tmp_path / "x.db")
    sync(a, ["legacy"], source_paths={"legacy": legacy_data}, snapshot_base=tmp_path / "snaps")
    assert a.counts()["messages"] == 3
    privacy.save_rules(privacy.Rules())
    res = sync(a, ["legacy"], source_paths={"legacy": legacy_data}, snapshot_base=tmp_path / "snaps")[0]
    assert res.excluded == 0 and a.counts()["messages"] == 4
    a.close()


class _OneMail(Importer):
    name = "legacy"

    def __init__(self, recs):
        super().__init__("-")
        self.recs = recs

    def available(self):
        return True

    def snapshot(self, dest):
        return dest

    def iter_records(self, snap, skip_keys=()):
        for r in self.recs:
            self.stats.seen += 1
            yield r


def test_unicode_keywords_and_subdomains(archive, tmp_path):
    recs = [
        MessageRecord(source="legacy", source_key="1", subject="Známky z matematiky", from_addr="x@y.cz"),
        MessageRecord(source="legacy", source_key="2", subject="hello", from_addr="p@mail.dept.uni.example"),
        MessageRecord(source="legacy", source_key="3", subject="hello again", from_addr="q@notuni.example"),
        MessageRecord(source="legacy", source_key="4", subject="Deleted thing", folder="Deleted Items"),
        MessageRecord(source="legacy", source_key="5", subject="Spam thing", folder="Junk Email"),
    ]
    rules = privacy.Rules()
    rules.add("subject_keywords", ["ZNÁMKY"])
    rules.add("domains", ["@Uni.example"])
    rules.add("folders", ["DeletedItems", "junk"])
    res = run_import(archive, _OneMail(recs), snapshot_base=tmp_path / "s", rules=rules)
    assert res.excluded == 4 and res.inserted == 1
    assert archive.conn.execute("SELECT subject FROM messages").fetchone()[0] == "hello again"
    # The same rules at query time agree with the import-time decision.
    for r in recs:
        hidden = privacy.excludes_message(r, rules)
        with archive.transaction():
            if hidden:
                archive.upsert(r)
        if hidden:
            sql, params = privacy.message_hidden(archive.conn, "m", rules)
            assert archive.conn.execute(
                f"SELECT COUNT(*) FROM messages m WHERE m.subject = ? AND ({sql})", [r.subject, *params]).fetchone()[0] == 1


def test_bad_rules_fail_closed(tmp_path, legacy_data):
    cfg = config.load_config()
    cfg["exclude"] = {"folder": ["typo"]}
    config.save_config(cfg)
    a = Archive(tmp_path / "x.db")
    res = sync(a, ["legacy"], source_paths={"legacy": legacy_data}, snapshot_base=tmp_path / "snaps")[0]
    assert res.status == "error" and "unknown key" in res.message and a.counts()["messages"] == 0
    with pytest.raises(privacy.PrivacyConfigError):
        tools.search_emails(a, "x")
    a.close()


# ---------------------------------------------------------------------- purge

def test_purge_deletes_everything_for_matches(loaded):
    att = loaded.conn.execute("SELECT id FROM attachments WHERE filename = 'budget.csv'").fetchone()[0]
    path = tools.get_attachment(loaded, att, mode="path")["path"]
    assert os.path.exists(path)
    _set_rules(attachment_names=["*.csv"], subject_keywords=["kickoff"])

    dry = privacy.purge(loaded, dry_run=True)
    assert dry["messages"] == 1 and dry["events"] == 1 and dry["cached_files"] == 1
    assert loaded.counts()["messages"] == 4 and os.path.exists(path)

    out = privacy.purge(loaded)
    assert out["messages"] == 1 and out["attachments"] == 1 and out["events"] == 1 and out["cached_files"] == 1
    assert not os.path.exists(path)
    q = lambda sql: loaded.conn.execute(sql).fetchone()[0]  # noqa: E731
    assert q("SELECT COUNT(*) FROM messages") == 3 and q("SELECT COUNT(*) FROM events") == 4
    assert q("SELECT COUNT(*) FROM attachments WHERE filename = 'budget.csv'") == 0
    assert q("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'zebra'") == 0
    assert q("SELECT COUNT(*) FROM message_sources WHERE message_pk NOT IN (SELECT id FROM messages)") == 0
    assert q("SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH 'kickoff'") == 0
    assert q("SELECT COUNT(*) FROM attendees WHERE event_pk NOT IN (SELECT id FROM events)") == 0
    assert q("SELECT COUNT(*) FROM event_instances WHERE event_pk NOT IN (SELECT id FROM events)") == 0
    assert q("SELECT COUNT(*) FROM attendees WHERE addr = 'ada@example.org'") == 0
    assert tools.archive_status(loaded)["privacy"]["hidden_messages"] == 0
    raw = loaded.path.read_bytes()
    assert b"zebra" not in raw and b"Project kickoff" not in raw


def test_purge_removes_excluded_folder_and_account_rows(loaded):
    _set_rules(folders=["Projects"], accounts=["me@uni.example.edu"])
    out = privacy.purge(loaded)
    assert out["accounts"] == 1 and out["folders"] == 3
    assert loaded.counts()["messages"] == 0 and loaded.counts()["accounts"] == 0


def test_purge_cli(loaded, capsys):
    db = str(loaded.path)
    assert cli.main(["--db", db, "purge-excluded"]) == 0
    assert "no privacy rules" in capsys.readouterr().out
    _set_rules(senders=["ada@example.org"])
    assert cli.main(["--db", db, "purge-excluded", "--dry-run"]) == 0
    assert "would delete: messages=1" in capsys.readouterr().out
    assert cli.main(["--db", db, "purge-excluded"]) == 0
    out = capsys.readouterr().out
    assert "deleted: messages=1" in out and "ada@example.org" not in out
    assert loaded.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3


# --------------------------------------------------------------------- config

def test_saving_rules_keeps_feeds_addresses_and_mode(monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(URL + "\n"))
    assert cli.main(["calendar", "add-feed", "work"]) == 0
    assert cli.main(["calendar", "set-my-addresses", "Me@Uni.example"]) == 0
    assert cli.main(["privacy", "add", "--folder", "Grades", "--sender", "hr@corp.example",
                     "--subject-keyword", 'say "hi" \\ Známky', "--domain", "@corp.example"]) == 0
    p = config.config_path()
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert [f.url for f in feeds.load_feeds()] == [URL]
    assert feeds.my_addresses() == {"me@uni.example"}
    rules = privacy.load_rules()
    assert rules.folders == ["grades"] and rules.domains == ["corp.example"]
    assert rules.subject_keywords == ['say "hi" \\ známky']

    # Feed edits afterwards keep the rules.
    assert cli.main(["calendar", "remove-feed", "work"]) == 0
    assert feeds.load_feeds() == [] and privacy.load_rules().as_dict() == rules.as_dict()
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    os.chmod(p, 0o644)
    privacy.save_rules(privacy.load_rules())
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    # Removing the last rule drops the table.
    privacy.save_rules(privacy.Rules())
    assert "exclude" not in config.load_config() and feeds.my_addresses() == {"me@uni.example"}


def test_privacy_cli(loaded, capsys):
    db = str(loaded.path)
    assert cli.main(["--db", db, "privacy", "show"]) == 0
    assert "no rules" in capsys.readouterr().out
    assert cli.main(["--db", db, "privacy", "add"]) == 2
    assert cli.main(["--db", db, "privacy", "add", "--folder", "Projects", "--folder", "Junk",
                     "--attachment-name", "*.csv"]) == 0
    out = capsys.readouterr().out
    assert "added 3 rule(s)" in out and "folders (2): projects, junk" in out
    assert "hidden in the archive: 1 messages, 0 events" in out
    assert cli.main(["--db", db, "privacy", "remove", "--folder", "JUNK"]) == 0
    assert "removed 1 rule(s)" in capsys.readouterr().out
    assert privacy.load_rules().folders == ["projects"]
    assert cli.main(["--db", db, "status"]) == 0
    assert "privacy: 2 rules, 1 messages and 0 events hidden" in capsys.readouterr().out


def test_config_roundtrip_keeps_unknown_sections():
    config.save_config({"my_addresses": ["a@b.c"], "other": {"x": 1, "y": ["p", "q"], "nested": {"z": True}},
                        "ics_feed": [{"name": "w", "url": "https://x.invalid/a"}]})
    cfg = config.load_config()
    privacy.save_rules(privacy.Rules(senders=["s@t.u"]))
    after = config.load_config()
    assert after["other"] == cfg["other"] and after["ics_feed"] == cfg["ics_feed"]
    assert after["exclude"] == {"senders": ["s@t.u"]}


def test_status_without_rules_reports_inactive(loaded):
    pv = tools.archive_status(loaded)["privacy"]
    assert pv == {"active": False, "rules": dict.fromkeys(privacy.RULE_TYPES, 0),
                  "hidden_messages": 0, "hidden_events": 0}


def test_excluded_account_hidden_from_account_overview(loaded):
    from new_outlook_mcp import tools as t

    assert [a["account"] for a in t.archive_status(loaded)["accounts"]] == ["me@uni.example.edu"]
    privacy.save_rules(privacy.Rules(accounts=["me@uni.example.edu"]))
    try:
        st = t.archive_status(loaded)
        assert st["accounts"] == [] and st["counts"]["accounts"] == 0
    finally:
        privacy.save_rules(privacy.Rules())
