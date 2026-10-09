"""Build a synthetic legacy Outlook 'Data' folder. All content is made up.

The schema mirrors what pyolk and olk15-export query (see README). Only the
columns this project reads are created, plus a few extras to make sure unknown
columns are ignored.
"""

from __future__ import annotations

import sqlite3
import struct
import uuid
from datetime import datetime, timezone
from pathlib import Path

from new_outlook_mcp.importers import olk15

MAGIC = olk15.MAGIC


def block_file(fourcc: str, payload: bytes) -> bytes:
    header = MAGIC + b"\x01\x01\x01\x00" + struct.pack("<i", olk15.KIND_BLOCK)
    header += b"\x03\x00\x00\x00" + uuid.uuid4().bytes  # 20-byte BlockID
    header += fourcc.encode("latin-1")[::-1] + b"\x00\x00\x00\x00"
    assert len(header) == olk15.HEADER_SIZE
    return header + payload


def collection(props: dict[tuple[int, int], bytes]) -> bytes:
    items = list(props.items())
    head = struct.pack("<3i", len(items), 12 + 8 * len(items), sum(len(v) for _, v in items))
    for (vtype, idx), value in items:
        head += olk15.encode_key(vtype, idx) + struct.pack("<i", len(value))
    return head + b"".join(v for _, v in items)


def collection_list(items: list[dict[tuple[int, int], bytes]]) -> bytes:
    blobs = [collection(i) for i in items]
    return struct.pack("<i", len(blobs)) + b"".join(struct.pack("<h", len(b)) for b in blobs) + b"".join(blobs)


def user_record(addr: str, name: str) -> bytes:
    a, n = addr.encode(), name.encode("utf-16-le")
    return b"\x03\x00\x02\x03\x00\x00" + b"\x00" * 22 + struct.pack("<i", len(a)) + a + struct.pack("<i", len(n)) + n


def entity_file(record_id: int, props: dict[tuple[int, int], bytes], class_id: int = 3) -> bytes:
    header = MAGIC + b"\x01\x01\x01\x00" + struct.pack("<i", olk15.KIND_ENTITY)
    header += struct.pack("<ii", record_id, class_id) + b"\x00" * 12 + b"gsMM" + b"\x00" * 4
    assert len(header) == olk15.HEADER_SIZE
    return header + collection(props)


def utf16z(s: str) -> bytes:
    return s.encode("utf-16-le") + b"\x00\x00"


def unix(y, mo, d, h=12, mi=0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp())


MIME_PLAIN = (
    "Message-ID: <alpha-1@example.org>\r\n"
    "Date: Tue, 05 Mar 2024 09:15:00 +0100\r\n"
    "From: Ada Example <ada@example.org>\r\n"
    "To: Bob Sample <bob@example.net>, carol@example.com\r\n"
    "Cc: Dan Test <dan@example.org>\r\n"
    "Subject: Quarterly budget review\r\n"
    "MIME-Version: 1.0\r\n"
    "Content-Type: multipart/mixed; boundary=\"XYZ\"\r\n"
    "\r\n"
    "--XYZ\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "\r\n"
    "Hi Bob,\r\nplease find the budget spreadsheet attached. The zebra numbers look fine.\r\n"
    "--XYZ\r\n"
    "Content-Type: text/csv; name=\"budget.csv\"\r\n"
    "Content-Disposition: attachment; filename=\"budget.csv\"\r\n"
    "\r\n"
    "item,amount\r\nzebra,42\r\n"
    "--XYZ--\r\n"
)

# Reply, stored with bare CR line endings as Outlook for Mac sometimes does.
MIME_REPLY = (
    "Message-ID: <alpha-2@example.net>\r"
    "In-Reply-To: <alpha-1@example.org>\r"
    "References: <alpha-1@example.org>\r"
    "Date: Wed, 06 Mar 2024 10:00:00 +0000\r"
    "From: bob@example.net\r"
    "To: Ada Example <ada@example.org>\r"
    "Subject: RE: Quarterly budget review\r"
    "Content-Type: text/html; charset=utf-8\r"
    "\r"
    "<html><body><p>Thanks Ada,</p><p>looks <b>good</b> to me.</p><style>p{}</style></body></html>\r"
)

ATTACHMENT_PART = (
    "Content-Type: application/pdf; name=\"report.pdf\"\r"
    "Content-Disposition: attachment; filename=\"report.pdf\"\r"
    "Content-Transfer-Encoding: base64\r"
    "\r"
    "JVBERi0xLjQKJcOkw7zDtsOfCjIgMCBvYmoKPDwvTGVuZ3RoIDMgMCBSPj4Kc3RyZWFtCg==\r"
)


ACCOUNT_UID = 60129542146


def build_legacy_data(root: Path) -> Path:
    """Create root/Data with 5 messages. Returns the Data path."""
    data = root / "Data"
    for sub in ("Messages/S0", "Message Sources/S0", "Message Attachments/S0"):
        (data / sub).mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(data / "Outlook.sqlite")
    conn.executescript(
        """
        CREATE TABLE Mail (
            Record_RecordID INTEGER PRIMARY KEY, PathToDataFile TEXT, Record_FolderID INTEGER,
            Record_AccountUID INTEGER, Record_ModDate INTEGER, Message_NormalizedSubject TEXT,
            Message_SenderList TEXT, Message_SenderAddressList TEXT, Message_DisplayTo TEXT,
            Message_ToRecipientAddressList TEXT, Message_CCRecipientAddressList TEXT,
            Message_TimeReceived INTEGER, Message_TimeSent INTEGER, Message_MessageID TEXT,
            Message_ReadFlag INTEGER, Message_HasAttachment INTEGER, Message_Preview TEXT,
            Message_Size INTEGER, Conversation_ConversationID INTEGER, Message_Unknown BLOB);
        CREATE TABLE Folders (
            Record_RecordID INTEGER PRIMARY KEY, Folder_Name TEXT, Folder_ParentID INTEGER,
            Record_AccountUID INTEGER, Folder_SpecialFolderType INTEGER);
        CREATE TABLE AccountsExchange (
            Record_RecordID INTEGER PRIMARY KEY, Account_Name TEXT, Account_EmailAddress TEXT,
            Account_MailAccountUID INTEGER);
        CREATE TABLE Blocks (BlockID BLOB, BlockTag INTEGER, PathToDataFile TEXT);
        CREATE TABLE Mail_OwnedBlocks (Record_RecordID INTEGER, BlockID BLOB, BlockTag INTEGER);
        CREATE TABLE Main (Record_RecordID INTEGER PRIMARY KEY);
        """
    )
    # Like real profiles: the mailbox root folder has no name, and Mail.Record_AccountUID
    # holds AccountsExchange.Account_MailAccountUID (a large number), not its RecordID.
    conn.executemany("INSERT INTO Folders VALUES (?, ?, ?, ?, ?)", [
        (122, None, 0, ACCOUNT_UID, 0),
        (1, "Inbox", 122, ACCOUNT_UID, 1),
        (2, "Projects", 1, ACCOUNT_UID, 0),
        (3, "Sent Items", 122, ACCOUNT_UID, 2),
    ])
    conn.execute("INSERT INTO AccountsExchange VALUES (1, 'University', 'me@uni.example.edu', ?)", (ACCOUNT_UID,))

    def add_block(rid: int, tag: str, rel: str, content: bytes) -> None:
        (data / rel).write_bytes(content)
        bid = uuid.uuid4().bytes
        tag_int = olk15.fourcc_int(tag)
        conn.execute("INSERT INTO Blocks VALUES (?, ?, ?)", (bid, tag_int, rel.replace(" ", "%20")))
        conn.execute("INSERT INTO Mail_OwnedBlocks VALUES (?, ?, ?)", (rid, bid, tag_int))

    def add_entity(rid: int, props) -> str:
        rel = f"Messages/S0/{uuid.uuid4()}.olk15Message"
        (data / rel).write_bytes(entity_file(rid, props))
        return rel

    mail = []
    # 101: full source available (MSrc) with a MIME attachment.
    rel = add_entity(101, {olk15.PROP_SUBJECT: utf16z("Quarterly budget review")})
    add_block(101, "MSrc", "Message Sources/S0/src-101.olk15MsgSource", block_file("MSrc", MIME_PLAIN.encode()))
    mail.append((101, rel, 2, ACCOUNT_UID, "Quarterly budget review", "Ada Example", "ada@example.org", "Bob Sample",
                 "bob@example.net", "dan@example.org", unix(2024, 3, 5, 8, 15), None, "<alpha-1@example.org>",
                 1, 1, "Hi Bob, please find", 2048, 7001))
    # 102: reply, source with CR line endings.
    rel = add_entity(102, {})
    add_block(102, "MSrc", "Message Sources/S0/src-102.olk15MsgSource", block_file("MSrc", MIME_REPLY.encode()))
    mail.append((102, rel, 1, ACCOUNT_UID, "Quarterly budget review", "bob@example.net", None, "Ada Example", None, None,
                 unix(2024, 3, 6, 10), None, "alpha-2@example.net", 0, 0, "Thanks Ada", 900, 7001))
    # 103: no source; .olk15Message holds subject, HTML body and headers; plus an Attc block.
    headers = (
        "Message-ID: <beta-1@example.com>\r\nFrom: Erin Demo <erin@example.com>\r\n"
        "To: me@uni.example.edu\r\nDate: Fri, 10 May 2024 16:30:00 +0000\r\nSubject: Field trip logistics\r\n"
    )
    rel = add_entity(103, {
        olk15.PROP_SUBJECT: utf16z("Field trip logistics"),
        olk15.PROP_BODY: utf16z("<html><body><div>The bus leaves at 8.</div><div>Bring a pelican sketchbook.</div></body></html>"),
        olk15.PROP_HEADERS: headers.encode() + b"\x00",
    })
    add_block(103, "Attc", "Message Attachments/S0/att-103.olk15MsgAttachment",
              block_file("Attc", ATTACHMENT_PART.encode()))
    mail.append((103, rel, 1, ACCOUNT_UID, "Field trip logistics", "Erin Demo", None, "me@uni.example.edu", None, None,
                 unix(2024, 5, 10, 16, 30), None, None, 1, 1, "The bus leaves", 5000, None))
    # 104: nothing but database columns (data file missing). Times in Cocoa seconds.
    mail.append((104, "Messages/S0/missing.olk15Message", 3, ACCOUNT_UID, "Lunch?", "Me", "me@uni.example.edu",
                 "frank@example.org", "frank@example.org", None, unix(2025, 1, 2) - 978307200, None, None,
                 1, 0, "Lunch on Thursday at the canteen?", 300, None))
    # 105: duplicate of 101 (same Message-ID) in another folder: must be deduplicated.
    rel = add_entity(105, {olk15.PROP_SUBJECT: utf16z("Quarterly budget review")})
    mail.append((105, rel, 1, ACCOUNT_UID, "Quarterly budget review", "Ada Example", "ada@example.org", None, None, None,
                 unix(2024, 3, 5, 8, 15), None, "alpha-1@example.org", 1, 0, "Hi Bob", 2048, 7001))

    _add_legacy_calendar(conn, data)
    conn.executemany(
        """INSERT INTO Mail (Record_RecordID, PathToDataFile, Record_FolderID, Record_AccountUID,
               Message_NormalizedSubject, Message_SenderList, Message_SenderAddressList, Message_DisplayTo,
               Message_ToRecipientAddressList, Message_CCRecipientAddressList, Message_TimeReceived,
               Message_TimeSent, Message_MessageID, Message_ReadFlag, Message_HasAttachment, Message_Preview,
               Message_Size, Conversation_ConversationID) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        mail,
    )
    conn.commit()
    conn.close()
    return data


WIN_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def wm(dt: datetime) -> int:
    """Minutes since 1601-01-01 UTC, as Outlook for Mac stores event times."""
    return int((dt - WIN_EPOCH).total_seconds() // 60)


def utc(y, mo, d, h=0, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def i32(v: int) -> bytes:
    return struct.pack("<i", v)


def tz_collection(tzid: str) -> bytes:
    return collection({(0x4643, 0x7A74): tzid.encode()})


def _add_legacy_calendar(conn: sqlite3.Connection, data: Path) -> None:
    """Five synthetic events in a 'Calendar' folder. See test_calendar.py for what each one checks."""
    from new_outlook_mcp.importers import legacy_calendar as lc

    (data / "Events/S0").mkdir(parents=True, exist_ok=True)
    conn.executescript(
        """
        CREATE TABLE CalendarEvents (
            Record_RecordID INTEGER PRIMARY KEY, PathToDataFile TEXT, Record_FolderID INTEGER,
            Record_AccountUID INTEGER, Calendar_StartDateUTC INTEGER, Calendar_EndDateUTC INTEGER,
            Calendar_IsRecurring INTEGER, Calendar_RecurrenceID INTEGER, Calendar_MasterRecordID INTEGER);
        INSERT INTO Folders VALUES (4, 'Calendar', 122, 60129542146, 0);
        """
    )

    def add(rid, start, end, props, *, recurring=0, recurrence_id=None, master=None):
        rel = f"Events/S0/{uuid.uuid4()}.olk15Event"
        (data / rel).write_bytes(entity_file(rid, props, class_id=8))
        conn.execute("INSERT INTO CalendarEvents VALUES (?, ?, 4, 1, ?, ?, ?, ?, ?)",
                     (rid, rel, wm(start), wm(end), recurring, recurrence_id, master))

    prague = tz_collection("Europe/Prague")
    # 201: single meeting with organizer, attendees and a Teams link.
    add(201, utc(2026, 10, 14, 8), utc(2026, 10, 14, 9), {
        lc.EV_SUBJECT: utf16z("Project kickoff"),
        lc.EV_LOCATION: utf16z("Room 4.12"),
        lc.EV_BODY: utf16z("<p>Agenda: scope, budget, timeline.</p>"),
        lc.EV_UID: b"UID-KICKOFF-1\x00",
        lc.EV_HTTP_JOIN_LINK: utf16z("https://teams.microsoft.com/l/meetup-join/19%3ameeting_synthetic"),
        lc.EV_RESPONSE: i32(1),
        lc.EV_BUSY: i32(0),
        lc.EV_ORGANIZER: user_record("erin@example.com", "Erin Demo"),
        lc.EV_ATTENDEES: collection_list([
            {lc.ATT_NAME: utf16z("Ada Example"), lc.ATT_ADDR: b"ada@example.org\x00", lc.ATT_TYPE: i32(0),
             lc.EV_RESPONSE: i32(1)},
            {lc.ATT_NAME: utf16z("Bob Sample"), lc.ATT_ADDR: b"bob@example.net\x00", lc.ATT_TYPE: i32(1),
             lc.EV_RESPONSE: i32(2)},
        ]),
        lc.EV_TIMEZONE: prague,
    })
    # 202: weekly on Monday 09:00 Prague time across the 29 Mar 2026 DST switch, 16 Mar skipped.
    add(202, utc(2026, 3, 2, 8), utc(2026, 3, 2, 9), {
        lc.EV_SUBJECT: utf16z("Weekly team sync"),
        lc.EV_UID: b"UID-WEEKLY-1\x00",
        lc.EV_TIMEZONE: prague,
        lc.EV_RRULE: collection({
            lc.RR_TYPE: i32(1), lc.RR_INTERVAL: i32(1), lc.RR_WEEKDAYS: i32(0b0000010),
            lc.RR_END_TYPE: i32(8225), lc.RR_UNTIL: i32(wm(utc(2026, 4, 13))),
            lc.RR_EXCEPTIONS: i32(wm(utc(2026, 3, 16))),
        }),
    }, recurring=1)
    # 203: the 23 Mar occurrence of 202, moved to Tuesday 24 Mar 14:00 Prague time.
    add(203, utc(2026, 3, 24, 13), utc(2026, 3, 24, 14), {
        lc.EV_SUBJECT: utf16z("Weekly team sync (moved)"),
        lc.EV_TIMEZONE: prague,
    }, recurrence_id=wm(utc(2026, 3, 23, 8)), master=202)
    # 204: all-day, out of office.
    add(204, utc(2026, 10, 20), utc(2026, 10, 21), {
        lc.EV_SUBJECT: utf16z("Conference travel"),
        lc.EV_UID: b"UID-TRAVEL-1\x00",
        lc.EV_ALL_DAY: b"\x01",
        lc.EV_BUSY: i32(3),
    })
    # 205: cancelled.
    add(205, utc(2026, 10, 15, 12), utc(2026, 10, 15, 13), {
        lc.EV_SUBJECT: utf16z("Cancelled lunch talk"),
        lc.EV_UID: b"UID-CANCELLED-1\x00",
        lc.EV_CANCELLED: b"\x01",
    })


FEED_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Synthetic//Test//EN
X-WR-CALNAME:Work
BEGIN:VTIMEZONE
TZID:W. Europe Standard Time
BEGIN:STANDARD
DTSTART:16010101T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:16010101T020000
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
UID:UID-KICKOFF-1
SUMMARY:Project kickoff
DTSTART;TZID=W. Europe Standard Time:20261014T100000
DTEND;TZID=W. Europe Standard Time:20261014T110000
LOCATION:Room 5.01 (changed)
STATUS:CONFIRMED
END:VEVENT
BEGIN:VEVENT
UID:UID-SEMINAR-1
SUMMARY:Reading seminar
DESCRIPTION:Join: https://zoom.us/j/123456789 (synthetic)
DTSTART;TZID=W. Europe Standard Time:20261005T140000
DTEND;TZID=W. Europe Standard Time:20261005T153000
RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=6
EXDATE;TZID=W. Europe Standard Time:20261019T140000
ORGANIZER;CN=Me:mailto:me@uni.example.edu
ATTENDEE;CN=Frank;PARTSTAT=ACCEPTED;ROLE=REQ-PARTICIPANT:mailto:frank@example.org
END:VEVENT
BEGIN:VEVENT
UID:UID-SEMINAR-1
RECURRENCE-ID;TZID=W. Europe Standard Time:20261026T140000
SUMMARY:Reading seminar (room change)
DTSTART;TZID=W. Europe Standard Time:20261026T160000
DTEND;TZID=W. Europe Standard Time:20261026T173000
END:VEVENT
END:VCALENDAR
"""


def add_legacy_message(data: Path, rid: int, subject: str, when: int) -> None:
    """Append one database-only message (for incremental-sync tests)."""
    conn = sqlite3.connect(data / "Outlook.sqlite")
    conn.execute(
        "INSERT INTO Mail (Record_RecordID, Record_FolderID, Record_AccountUID, Message_NormalizedSubject,"
        " Message_SenderAddressList, Message_TimeReceived, Message_Preview) VALUES (?, 1, 1, ?, ?, ?, ?)",
        (rid, subject, "gina@example.org", when, "added later"),
    )
    conn.commit()
    conn.close()
