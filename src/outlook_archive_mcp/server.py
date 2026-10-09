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

from . import __version__, paths, tools
from .db import Archive
from .sync import sync

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

INSTRUCTIONS = """\
Local, read-only archive of the user's Outlook for Mac mail. It contains the
frozen legacy Outlook archive and, once supported, the New Outlook cache.
Search with search_emails, then open a message with get_email (use its `id`).
Bodies are plain text and may be truncated: pass `offset` = `next_offset` to
continue. The archive cannot send mail. create_draft only opens a prefilled
draft window that the user must review and send themselves.
"""


def build_server(db_path: Path | None = None) -> MCPServer:
    db_path = Path(db_path or paths.db_path())
    server = MCPServer("outlook-archive", instructions=INSTRUCTIONS, version=__version__)
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
    ) -> dict:
        """Search archived emails. Returns summaries with an `id` for get_email/get_thread."""
        return call(tools.search_emails, query, from_=sender, to=recipient, folder=folder, account=account,
                    date_from=date_from, date_to=date_to, has_attachment=has_attachment,
                    attachment_name=attachment_name, sort=sort, limit=limit, offset=offset)

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
    ) -> dict:
        """List the newest archived emails."""
        return call(tools.list_recent, limit=limit, folder=folder, account=account, days=days)

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
        attachment_id: Annotated[int, Field(description="attachment_id from list_attachments")],
        mode: Annotated[Literal["text", "path", "open"], Field(
            description="text: extract readable text (PDF, Word, Excel, plain text, CSV, calendar); "
                        "path: return a local file path; open: open the file in its default Mac app")] = "text",
        offset: Annotated[int, Field(ge=0, description="Text character offset, for paging long text")] = 0,
        max_chars: Annotated[int, Field(ge=100, le=100000)] = tools.DEFAULT_BODY_CHARS,
    ) -> dict:
        """Read an attachment from local storage. Never downloads from a server: if the file is not
        on this Mac, the result says so."""
        return call(tools.get_attachment, attachment_id, mode=mode, offset=offset, max_chars=max_chars)

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
    def sync_now(source: Literal["legacy", "hxstore", "all"] = "all") -> dict:
        """Import new mail from local Outlook files into the archive. Reads copies of Outlook's
        files only and never contacts a server."""
        names = ["legacy", "hxstore"] if source == "all" else [source]
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
    parser = argparse.ArgumentParser(prog="outlook-archive-mcp", description="Run the MCP server on stdio.")
    parser.add_argument("--db", type=Path, help=f"archive database (default: {paths.db_path()})")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    build_server(args.db).run("stdio")


if __name__ == "__main__":
    main()
