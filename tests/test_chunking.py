from __future__ import annotations

import pytest

from new_outlook_mcp import chunking
from new_outlook_mcp.chunking import MAX_CHARS, chunk_body, chunk_plain, strip_quotes_and_signature


def test_strips_gt_quotes_and_wrote_header():
    text = ("Sounds good, let's meet on Friday.\n\nOn Tue, Mar 5, 2024 at 8:15 AM Ada Example <ada@example.org> wrote:\n"
            "> Can we meet this week?\n> Ada\n")
    out = strip_quotes_and_signature(text)
    assert out == "Sounds good, let's meet on Friday."


def test_strips_wrapped_wrote_header():
    text = "Yes.\n\nOn Tuesday, March 5, 2024 at 8:15 AM, Ada Example\n<ada@example.org> wrote:\nOld text here\nmore old"
    assert strip_quotes_and_signature(text) == "Yes."


def test_strips_czech_reply_header():
    text = ("Díky za info, pošlu to zítra.\n\nDne 5. 3. 2024 v 8:15 Ada Example <ada@example.org> napsal(a):\n"
            "> Původní text\n> další řádek\n")
    out = strip_quotes_and_signature(text)
    assert "pošlu to zítra" in out and "Původní" not in out and "napsal" not in out


def test_strips_original_message_and_outlook_header_blocks():
    a = "Approved.\n\n-----Original Message-----\nFrom: Bob\nSent: Monday\nSubject: Budget\n\nPlease approve."
    assert strip_quotes_and_signature(a) == "Approved."
    b = ("Approved.\n\n________________________________\nFrom: Bob Sample <bob@example.net>\n"
         "Sent: Monday, March 4, 2024 9:00 AM\nTo: Ada\nSubject: Budget\n\nPlease approve.")
    assert strip_quotes_and_signature(b) == "Approved."
    cz = "Schváleno.\n\nOd: Bob <bob@example.net>\nOdesláno: pondělí 4. března 2024 9:00\nKomu: Ada\nPředmět: Rozpočet\n\nProsím schvalte."
    assert strip_quotes_and_signature(cz) == "Schváleno."
    r = strip_quotes_and_signature("Původní zpráva\n\n-----Původní zpráva-----\nOd: Bob\nOdesláno: x\nKomu: y\n")
    assert r.startswith("Původní zpráva")


def test_strips_signature_delimiter_and_closers():
    assert strip_quotes_and_signature("Meeting is at 10.\n\n-- \nAda Example\nProfessor\nTel: 123") == "Meeting is at 10."
    out = strip_quotes_and_signature("The data look fine to me.\n\nBest regards,\nAda Example\nUniversity\nada@example.org")
    assert out == "The data look fine to me."
    cz = strip_quotes_and_signature("Přijdu v pátek.\n\nS pozdravem\nAda Příklad\nUniverzita")
    assert cz == "Přijdu v pátek."
    assert "iPhone" not in strip_quotes_and_signature("See you there.\n\nSent from my iPhone")


def test_closer_inside_long_text_is_kept():
    body = "Thanks, that helps.\n" + "\n".join(f"Detail line number {i} with some more words in it." for i in range(15))
    assert "Detail line number 14" in strip_quotes_and_signature(body)


def test_inline_replies_keep_answers():
    text = ("On Mon, Ada wrote:\n> Q1: when?\nFriday.\n> Q2: where?\nIn the canteen.\n")
    out = strip_quotes_and_signature(text)
    assert "Friday." in out and "canteen" in out and "Q1" not in out


def test_never_strips_to_nothing():
    text = "> only a quote\n> and another"
    assert "only a quote" in strip_quotes_and_signature(text) or strip_quotes_and_signature(text) == ""
    top = "-----Original Message-----\nFrom: Bob\nSent: x\nTo: y\n\nForwarded content that matters."
    assert chunk_body(top)


def test_chunk_sizes_overlap_and_offsets():
    paras = [f"Paragraph {i}. " + ("word " * 25).strip() for i in range(30)]
    text = "\n\n".join(paras)
    chunks = chunk_body(text)
    assert len(chunks) > 2
    assert all(len(c.text) <= MAX_CHARS + 300 for c in chunks)  # includes at most one overlap paragraph
    assert all(500 <= len(c.text) or c is chunks[-1] for c in chunks)
    for c in chunks:
        assert text[c.start:c.end].replace("\n", " ").split()[0] == c.text.split()[0]
    # overlap: the first paragraph of a chunk repeats the last one of the previous chunk
    first = chunks[1].text.split("\n\n")[0]
    assert first in chunks[0].text


def test_long_paragraph_without_breaks_is_split():
    text = "x" * 3500
    chunks = chunk_plain(text)
    assert len(chunks) >= 4 and all(len(c.text) <= MAX_CHARS for c in chunks)
    assert chunk_plain("") == [] and chunk_body(None) == []


def test_chunk_body_drops_quote_content():
    text = "My new idea is about reframing.\n\n> old quote about zebras\n"
    joined = " ".join(c.text for c in chunk_body(text))
    assert "reframing" in joined and "zebras" not in joined


@pytest.mark.parametrize("reply, quote_header, closer, kept, dropped", [
    # Danish
    ("Tak for mødet, jeg sender rapporten i morgen.",
     "Den 5. mar. 2026 kl. 09.15 skrev Ada Example <ada@example.org>:", "Med venlig hilsen\nAda",
     "sender rapporten", "skrev"),
    # Dutch
    ("Bedankt voor het overleg, ik stuur het verslag morgen.",
     "Op 5 mrt. 2026 om 09:15 schreef Ada Example <ada@example.org>:", "Met vriendelijke groet,\nAda",
     "stuur het verslag", "schreef"),
    # Finnish
    ("Kiitos kokouksesta, lähetän raportin huomenna.",
     "Ti 5.3.2026 klo 9.15 Ada Example <ada@example.org> kirjoitti:", "Ystävällisin terveisin\nAda",
     "lähetän raportin", "kirjoitti"),
])
def test_quote_and_closer_stripping_nordic_and_dutch(reply, quote_header, closer, kept, dropped):
    text = f"{reply}\n\n{closer}\n\n{quote_header}\n> The original message text.\n"
    out = chunking.strip_quotes_and_signature(text)
    assert kept in out and dropped not in out and "original message" not in out
    assert closer.splitlines()[0] not in out


@pytest.mark.parametrize("headers, marker", [
    ("Fra: Ada Example <ada@example.org>\nSendt: 5. marts 2026 09:15\nTil: Bob\nEmne: Rapport", "Emne"),
    ("Van: Ada Example <ada@example.org>\nVerzonden: 5 maart 2026 09:15\nAan: Bob\nOnderwerp: Verslag", "Onderwerp"),
    ("Lähettäjä: Ada Example <ada@example.org>\nLähetetty: 5. maaliskuuta 2026 9.15\nVastaanottaja: Bob\nAihe: Raportti",
     "Aihe"),
])
def test_outlook_reply_headers_nordic_and_dutch(headers, marker):
    text = f"Short answer here.\n\n{headers}\n\nOld quoted body that should go."
    out = chunking.strip_quotes_and_signature(text)
    assert "Short answer here." in out and marker not in out and "Old quoted body" not in out
