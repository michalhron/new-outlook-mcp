"""End-to-end: call the MCP tools through the SDK's in-process client."""

from __future__ import annotations

import json

import anyio
import pytest
from mcp import Client

from new_outlook_mcp.server import build_server

EXPECTED_TOOLS = {
    "search_emails", "get_email", "get_thread", "list_recent", "list_folders", "archive_status",
    "sync_now", "create_draft", "list_attachments", "get_attachment",
    "list_calendar_events", "get_calendar_event", "search_calendar", "calendar_freebusy", "find_free_slots",
    "meeting_prep", "create_event_draft",
}
READ_ONLY_TOOLS = EXPECTED_TOOLS - {"sync_now", "create_draft", "create_event_draft"}


def _payload(result) -> dict:
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


@pytest.fixture
def server(loaded):
    return build_server(loaded.path)


def test_tools_listed_with_read_only_hints(server):
    async def go():
        async with Client(server) as c:
            return (await c.list_tools()).tools

    listed = anyio.run(go)
    names = {t.name for t in listed}
    assert EXPECTED_TOOLS <= names
    ann = {t.name: t.annotations for t in listed}
    for name in READ_ONLY_TOOLS:
        assert ann[name].read_only_hint is True, name
    schema = next(t for t in listed if t.name == "search_emails").input_schema
    assert "sender" in schema["properties"] and "attachment_name" in schema["properties"]


def test_search_then_get(server):
    async def go():
        async with Client(server) as c:
            found = _payload(await c.call_tool("search_emails", {"query": "pelican", "sender": "erin"}))
            mail = _payload(await c.call_tool("get_email", {"email_id": str(found["results"][0]["id"])}))
            thread = _payload(await c.call_tool("get_thread", {"email_id": "alpha-1@example.org"}))
            status = _payload(await c.call_tool("archive_status", {}))
            atts = _payload(await c.call_tool("list_attachments", {"email_id": "beta-1@example.com"}))
            return found, mail, thread, status, atts

    found, mail, thread, status, atts = anyio.run(go)
    assert found["total"] == 1
    assert mail["subject"] == "Field trip logistics"
    assert thread["thread_size"] == 2
    assert status["counts"]["messages"] == 4
    assert atts["attachments"][0]["filename"] == "report.pdf"


def test_calendar_tools_over_mcp(server):
    async def go():
        async with Client(server) as c:
            evs = _payload(await c.call_tool("list_calendar_events", {
                "start": "2026-10-14", "end": "2026-10-14", "timezone": "Europe/Prague"}))
            prep = _payload(await c.call_tool("meeting_prep", {"event_id": str(evs["events"][0]["event_id"])}))
            return evs, prep

    evs, prep = anyio.run(go)
    assert evs["events"][0]["subject"] == "Project kickoff"
    assert prep["event"]["subject"] == "Project kickoff"


def test_bad_input_is_a_tool_error(server):
    async def go():
        async with Client(server) as c:
            return await c.call_tool("get_email", {"email_id": "no-such-message"})

    res = anyio.run(go)
    assert res.is_error and "no email" in res.content[0].text


def test_sync_now_reports_unavailable_sources(server):
    async def go():
        async with Client(server) as c:
            return _payload(await c.call_tool("sync_now", {"source": "hxstore"}))

    res = anyio.run(go)
    assert res["results"][0]["status"] == "unavailable"
