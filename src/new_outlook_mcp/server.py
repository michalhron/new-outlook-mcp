"""MCP server (stdio). Read-only over Outlook data: it queries only our archive DB."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from . import __version__, caltools, paths, realms, tools
from .db import Archive
from .sync import sync

TzParam = Annotated[str | None, Field(description="IANA timezone for input and output times (default: this Mac's zone)")]
RealmParam = Annotated[Literal["work", "private", "all"] | None, Field(
    description="Which mail to cover: work, private or all. Default: the user's default realm (work once "
                "realms are set up). Use private or all only when the user asks about private mail")]

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

INSTRUCTIONS = """\
Local, read-only archive of the user's Outlook for Mac mail and calendar: the
frozen legacy Outlook archive, the New Outlook cache, and optional published
ICS feeds. Search with search_emails, then open a message with get_email (use
its `id`). Bodies are plain text and may be truncated: pass `offset` =
`next_offset` to continue. search_files finds attachment files and bodies in Outlook's
cache that no archived message owns. Calendar tools work on the user's own calendar only.
When the user has assigned accounts to realms, results carry a `realm` (work,
private or unassigned) and searches cover work mail by default. Pass
realm="private" or realm="all" only when the user asks about private mail.
Nothing here can send mail, answer invitations, or change calendar events.
create_draft and create_event_draft only open a draft that the user reviews
and sends or saves themselves.
"""


def build_server(db_path: Path | None = None, *, realm: str | None = None) -> MCPServer:
    """`realm` ("work", "private", "all") sets this process's realm fence. None leaves it as it is."""
    if realm is not None:
        realms.set_fence(realm)
    db_path = Path(db_path or paths.db_path())
    server = MCPServer("new-outlook", instructions=INSTRUCTIONS, version=__version__)
    state: dict[str, Archive] = {}

    def archive() -> Archive:
        if "a" not in state:
            state["a"] = Archive(db_path)
        return state["a"]

    def call(fn, *args, **kwargs):
        try:
            return fn(archive(), *args, **kwargs)
        except tools.ToolInputError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=READ_ONLY)
    def search_emails(
        query: Annotated[str | None, Field(description="Full-text query (SQLite FTS5 syntax: words, \"phrases\", OR, NOT, prefix*). Searches subject, sender, recipients and body. Omit to filter only.")] = None,
        sender: Annotated[str | None, Field(description="From: sender name or address contains this text")] = None,
        recipient: Annotated[str | None, Field(description="To/Cc/Bcc contains this text")] = None,
        folder: Annotated[str | None, Field(description="Folder name contains this text")] = None,
        account: Annotated[str | None, Field(description="Account name contains this text")] = None,
        date_from: Annotated[str | None, Field(description="Earliest date, YYYY-MM-DD or ISO 8601 (UTC)")] = None,
        date_to: Annotated[str | None, Field(description="Latest date, inclusive, YYYY-MM-DD or ISO 8601 (UTC)")] = None,
        has_attachment: bool | None = None,
        attachment_name: Annotated[str | None, Field(description="An attachment file name contains this text")] = None,
        sort: Literal["relevance", "date_desc", "date_asc"] = "relevance",
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
        mode: Annotated[Literal["keyword", "semantic", "hybrid"], Field(
            description="keyword: exact words (default). semantic: by meaning. hybrid: both, fused. "
                        "semantic and hybrid need `new-outlook embed` to have been run")] = "keyword",
        realm: RealmParam = None,
    ) -> dict:
        """Search archived emails. Returns summaries with an `id` for get_email/get_thread."""
        return call(tools.search_emails, query, from_=sender, to=recipient, folder=folder, account=account,
                    date_from=date_from, date_to=date_to, has_attachment=has_attachment,
                    attachment_name=attachment_name, sort=sort, limit=limit, offset=offset, mode=mode,
                    realm=realm)

    @server.tool(annotations=READ_ONLY)
    def semantic_search(
        query: Annotated[str, Field(description="What you remember, in your own words, in any of about 100 languages. No exact keywords needed")],
        mode: Annotated[Literal["hybrid", "semantic"], Field(
            description="hybrid: meaning plus keywords, fused (default). semantic: meaning only")] = "hybrid",
        sender: Annotated[str | None, Field(description="From: sender name or address contains this text")] = None,
        recipient: Annotated[str | None, Field(description="To/Cc/Bcc contains this text")] = None,
        folder: Annotated[str | None, Field(description="Folder name contains this text")] = None,
        account: Annotated[str | None, Field(description="Account name contains this text")] = None,
        date_from: Annotated[str | None, Field(description="Earliest date, YYYY-MM-DD or ISO 8601 (UTC)")] = None,
        date_to: Annotated[str | None, Field(description="Latest date, inclusive, YYYY-MM-DD or ISO 8601 (UTC)")] = None,
        has_attachment: bool | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 10,
        realm: RealmParam = None,
    ) -> dict:
        """Find emails and attachments by meaning. Each result is one email with the best matching passage as
        its `snippet`, `match` says whether that passage is the subject, the body or an attachment, and
        `matched` says whether meaning, keywords or both found it."""
        return call(tools.semantic_search, query, mode=mode, limit=limit, from_=sender, to=recipient,
                    folder=folder, account=account, date_from=date_from, date_to=date_to,
                    has_attachment=has_attachment, realm=realm)

    @server.tool(annotations=READ_ONLY)
    def find_similar(
        email_id: Annotated[str | None, Field(description="Archive id or Message-ID of an email")] = None,
        attachment_id: Annotated[int | None, Field(description="attachment_id from list_attachments")] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 10,
        realm: RealmParam = None,
    ) -> dict:
        """Find emails similar in content to an email or to one attachment. Give exactly one of the two ids."""
        return call(tools.find_similar, email_id, attachment_id=attachment_id, limit=limit, realm=realm)

    @server.tool(annotations=READ_ONLY)
    def get_email(
        email_id: Annotated[str, Field(description="Archive id from search results, or an Internet Message-ID")],
        offset: Annotated[int, Field(ge=0, description="Body character offset, for paging long bodies")] = 0,
        max_chars: Annotated[int, Field(ge=100, le=100000)] = tools.DEFAULT_BODY_CHARS,
        include_headers: bool = False,
    ) -> dict:
        """Get one email: metadata, attachment list and plain-text body (paged with offset)."""
        return call(tools.get_email, email_id, offset=offset, max_chars=max_chars, include_headers=include_headers)

    @server.tool(annotations=READ_ONLY)
    def get_thread(
        email_id: Annotated[str, Field(description="Archive id or Internet Message-ID of any message in the thread")],
        max_messages: Annotated[int, Field(ge=1, le=200)] = 50,
        body_chars: Annotated[int, Field(ge=0, le=20000, description="Body characters per message")] = 1500,
        subject_fallback: bool = True,
    ) -> dict:
        """Get the conversation containing a message, oldest first. Uses Message-ID/References/In-Reply-To,
        the Outlook conversation id, and falls back to the normalized subject."""
        return call(tools.get_thread, email_id, max_messages=max_messages, body_chars=body_chars,
                    subject_fallback=subject_fallback)

    @server.tool(annotations=READ_ONLY)
    def list_recent(
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
        folder: str | None = None,
        account: str | None = None,
        days: Annotated[int | None, Field(ge=1, description="Only messages from the last N days")] = None,
        realm: RealmParam = None,
    ) -> dict:
        """List the newest archived emails."""
        return call(tools.list_recent, limit=limit, folder=folder, account=account, days=days, realm=realm)

    @server.tool(annotations=READ_ONLY)
    def list_attachments(
        email_id: Annotated[str, Field(description="Archive id or Internet Message-ID of the email")],
        include_inline: Annotated[bool, Field(description="Also list small inline images such as signature logos")] = False,
    ) -> dict:
        """List an email's attachments: name, type, size, and whether the file is on this Mac."""
        return call(tools.list_attachments, email_id, include_inline=include_inline)

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                             idempotent_hint=True, open_world_hint=False))
    def get_attachment(
        attachment_id: Annotated[int | str, Field(
            description="attachment_id from list_attachments, or an id like 'orphan:12' from search_files")],
        mode: Annotated[Literal["text", "path", "open"], Field(
            description="text: extract readable text (PDF, Word, Excel, plain text, CSV, calendar); "
                        "path: return a local file path; open: open the file in its default Mac app")] = "text",
        offset: Annotated[int, Field(ge=0, description="Text character offset, for paging long text")] = 0,
        max_chars: Annotated[int, Field(ge=100, le=100000)] = tools.DEFAULT_BODY_CHARS,
    ) -> dict:
        """Read an attachment from local storage. Never downloads from a server: if the file is not
        on this Mac, the result says so. Ids like 'orphan:12' are files that no archived message owns:
        their text stays available even after Outlook deletes the file."""
        return call(tools.get_attachment, attachment_id, mode=mode, offset=offset, max_chars=max_chars)

    @server.tool(annotations=READ_ONLY)
    def search_files(
        query: Annotated[str | None, Field(description="Full-text query (FTS5 syntax) over file name and text. "
                                                      "Omit to list the newest files.")] = None,
        kind: Annotated[Literal["attachment", "body"] | None, Field(
            description="attachment: attachment files; body: cached message bodies")] = None,
        date_from: Annotated[str | None, Field(description="Earliest file date, YYYY-MM-DD or ISO 8601 (UTC)")] = None,
        date_to: Annotated[str | None, Field(description="Latest file date, inclusive")] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict:
        """Search files in Outlook's Files/ cache that belong to no archived message (attachments and bodies
        of mail that has left the cache). Read one with get_attachment using its `attachment_id` ('orphan:<n>').
        Dates are file dates, not send dates. Use search_emails for mail itself."""
        return call(tools.search_files, query, kind=kind, date_from=date_from, date_to=date_to, limit=limit,
                    offset=offset)


    @server.tool(annotations=READ_ONLY)
    def list_calendar_events(
        start: Annotated[str, Field(description="Start, YYYY-MM-DD or ISO 8601 (local time if no offset)")],
        end: Annotated[str | None, Field(description="End, inclusive day or ISO 8601. Default: start + 7 days")] = None,
        calendar: Annotated[str | None, Field(description="Calendar name contains this text")] = None,
        account: str | None = None,
        timezone: TzParam = None,
        include_cancelled: bool = False,
        limit: Annotated[int, Field(ge=1, le=500)] = 200,
    ) -> dict:
        """List calendar event occurrences in a date range (recurring events are expanded)."""
        return call(caltools.list_calendar_events, start, end, calendar=calendar, account=account,
                    timezone_name=timezone, include_cancelled=include_cancelled, limit=limit)

    @server.tool(annotations=READ_ONLY)
    def get_calendar_event(
        event_id: Annotated[str, Field(description="event_id from list_calendar_events or search_calendar")],
        timezone: TzParam = None,
    ) -> dict:
        """Full details of one event: times, attendees and responses, body, meeting link, recurrence."""
        return call(caltools.get_calendar_event, event_id, timezone_name=timezone)

    @server.tool(annotations=READ_ONLY)
    def search_calendar(
        query: Annotated[str, Field(description="Full-text query over subject, location, people and body")],
        date_from: str | None = None,
        date_to: str | None = None,
        timezone: TzParam = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict:
        """Search calendar events."""
        return call(caltools.search_calendar, query, date_from=date_from, date_to=date_to,
                    timezone_name=timezone, limit=limit)

    @server.tool(annotations=READ_ONLY)
    def calendar_freebusy(
        start: str,
        end: str | None = None,
        working_hours: Annotated[str, Field(description="e.g. 09:00-17:00")] = "09:00-17:00",
        weekdays: Annotated[str, Field(description="e.g. MO-FR or MO,TU,TH")] = "MO-FR",
        timezone: TzParam = None,
        include_tentative: bool = True,
    ) -> dict:
        """My own busy blocks and free time within working hours. Other people's calendars are not available."""
        return call(caltools.calendar_freebusy, start, end, working_hours=working_hours, weekdays=weekdays,
                    timezone_name=timezone, include_tentative=include_tentative)

    @server.tool(annotations=READ_ONLY)
    def find_free_slots(
        duration_minutes: Annotated[int, Field(ge=5, le=1440)],
        start: str,
        end: str | None = None,
        working_hours: str = "09:00-17:00",
        weekdays: str = "MO-FR",
        timezone: TzParam = None,
        step_minutes: Annotated[int, Field(ge=5, le=240)] = 30,
        max_results: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Find free slots of a given length in my own calendar."""
        return call(caltools.find_free_slots, duration_minutes, start, end, working_hours=working_hours,
                    weekdays=weekdays, timezone_name=timezone, step_minutes=step_minutes, max_results=max_results)

    @server.tool(annotations=READ_ONLY)
    def meeting_prep(
        event_id: str,
        days_back: Annotated[int, Field(ge=1, le=730)] = 90,
        max_threads: Annotated[int, Field(ge=1, le=50)] = 10,
        timezone: TzParam = None,
    ) -> dict:
        """Prepare for a meeting: event details, attendees, and recent email threads with them or on the topic."""
        return call(caltools.meeting_prep, event_id, days_back=days_back, max_threads=max_threads,
                    timezone_name=timezone)

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                             idempotent_hint=False, open_world_hint=False))
    def create_event_draft(
        subject: str,
        start: Annotated[str, Field(description="YYYY-MM-DDTHH:MM (local) or YYYY-MM-DD for all-day")],
        end: Annotated[str | None, Field(description="End time; for all-day events the last day. Default: 1 hour")] = None,
        all_day: bool = False,
        location: str | None = None,
        body: str | None = None,
        attendees: list[str] | None = None,
        timezone: TzParam = None,
    ) -> dict:
        """EXPERIMENTAL. Open a new-event draft (.ics) in Outlook for the user to review and save.
        It does not add, change or delete anything in the calendar and sends no invitations."""
        try:
            return caltools.create_event_draft(subject=subject, start=start, end=end, timezone_name=timezone,
                                               all_day=all_day, location=location, body=body, attendees=attendees)
        except tools.ToolInputError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=READ_ONLY)
    def list_folders() -> dict:
        """List folders with message counts and date ranges."""
        return call(tools.list_folders)

    @server.tool(annotations=READ_ONLY)
    def archive_status() -> dict:
        """Archive size, date coverage per source (legacy, hxstore) and the last sync per source."""
        return call(tools.archive_status)

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                             idempotent_hint=True, open_world_hint=False))
    def sync_now(source: Literal["legacy", "hxstore", "ics", "all"] = "all") -> dict:
        """Import new mail and calendar data from local Outlook files into the archive. Reads copies of
        Outlook's files only. The one network access is the user's own published ICS feeds, if configured."""
        names = ["legacy", "hxstore", "ics"] if source == "all" else [source]
        results = sync(archive(), names)
        return {"results": [r.as_dict() for r in results]}

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                             idempotent_hint=False, open_world_hint=True))
    def create_draft(
        to: Annotated[list[str], Field(description="Recipient addresses")],
        subject: str = "",
        body: Annotated[str, Field(description="Plain-text body")] = "",
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
    ) -> dict:
        """Open a prefilled draft in the default mail app (New Outlook) via a mailto: link.
        It never sends: the user reviews and sends the draft manually."""
        try:
            return tools.create_draft(to, cc=cc, bcc=bcc, subject=subject, body=body)
        except tools.ToolInputError as exc:
            raise ToolError(str(exc)) from exc

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="new-outlook-mcp", description="Run the MCP server on stdio.")
    parser.add_argument("--db", type=Path, help=f"archive database (default: {paths.db_path()})")
    parser.add_argument("--realm", choices=realms.FENCES,
                        help="hard limit: every tool returns only this realm, whatever a call asks for. "
                             "Default: $NEW_OUTLOOK_REALM, else all (searches still default to work)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        fence = realms.default_fence(args.realm)
    except realms.RealmConfigError as exc:
        parser.error(str(exc))
    build_server(args.db, realm=fence).run("stdio")


if __name__ == "__main__":
    main()
