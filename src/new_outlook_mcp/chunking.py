"""Turn mail bodies and attachment text into chunks worth embedding.

Mail bodies lose quoted replies and signatures first, so a reply is embedded for what
its author wrote and not for the thread below it. The rules are conservative: when
stripping would leave nothing, the text is kept. Offsets (`start`, `end`) point into
the original text that was passed in.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

TARGET_CHARS = 700
MAX_CHARS = 1000
OVERLAP_CHARS = 200
MIN_CHARS = 12


@dataclass
class Chunk:
    text: str
    start: int
    end: int


# ------------------------------------------------------------- quote stripping

_ORIGINAL_MSG = re.compile(
    r"^\s*[-_ ]{2,}\s*(original message|původní zpráva|ursprüngliche nachricht|message d'origine|"
    r"forwarded message|přeposlaná zpráva|"
    r"oprindelig meddelelse|videresendt meddelelse|"  # Danish
    r"oorspronkelijk bericht|doorgestuurd bericht|"  # Dutch
    r"alkuperäinen viesti|välitetty viesti)\s*[-_ ]{2,}\s*$", re.IGNORECASE)  # Finnish
_WROTE = [
    re.compile(r"^On\b.{0,300}?\bwrote:\s*$", re.DOTALL),
    re.compile(r"^Dne\b.{0,300}?\bnapsal(?:a|i|\(a\))?\s*:\s*$", re.DOTALL | re.IGNORECASE),
    re.compile(r"^(Am|Le|El|Il)\b.{0,300}?\b(schrieb|a écrit|escribió|ha scritto)\b.{0,40}:\s*$",
               re.DOTALL | re.IGNORECASE),
    re.compile(r"^(Den|D\.)\s.{0,300}?\bskrev\b.{0,40}:\s*$", re.DOTALL | re.IGNORECASE),  # Danish
    re.compile(r"^Op\s.{0,300}?\bschreef\b.{0,40}:\s*$", re.DOTALL | re.IGNORECASE),  # Dutch
    re.compile(r"^.{0,300}?\bkirjoitti\s*:\s*$", re.DOTALL | re.IGNORECASE),  # Finnish: date and name first
]
# Outlook reply headers. da: Fra/Sendt/Til/Emne, nl: Van/Verzonden/Aan/Onderwerp,
# fi: Lähettäjä/Lähetetty/Vastaanottaja/Aihe/Kopio.
_HDR_FROM = re.compile(r"^\s*(From|Od|Von|De|Fra|Van|Lähettäjä|Lahettaja)\s*:\s*\S", re.IGNORECASE)
_HDR_SENT = re.compile(r"^\s*(Sent|Date|Odesláno|Odeslano|Datum|Gesendet|Envoyé|Sendt|Dato|Verzonden|"
                       r"Verstuurd|Lähetetty|Lahetetty|Päivämäärä)\s*:", re.IGNORECASE)
_HDR_OTHER = re.compile(r"^\s*(To|Komu|An|À|Subject|Předmět|Predmet|Betreff|Cc|Kopie|Til|Emne|Aan|Onderwerp|"
                        r"Vastaanottaja|Aihe|Kopio)\s*:", re.IGNORECASE)
_SIG_DELIM = re.compile(r"^--\s?$")
_RULE = re.compile(r"^\s*[_\-=*~]{5,}\s*$")
_DEVICE_SIG = re.compile(
    r"^\s*(sent from my \w+|sent from outlook for \w+|get outlook for \w+|odesláno z \w+|odeslano z \w+|"
    r"sent from mail for windows|sendt fra min \w+|verzonden (vanaf|met) (mijn )?\w+|"
    r"lähetetty \w+-?(laitteesta|puhelimesta)?|lähetetty (iphonesta|androidista|outlookista)\w*)\b.{0,40}$",
    re.IGNORECASE)
_CLOSER = re.compile(
    r"^(best regards|kind regards|warm regards|regards|best wishes|sincerely|yours sincerely|cheers|"
    r"thanks|thank you|many thanks|best|s pozdravem|s pratelskym pozdravem|srdecne zdravi|zdravi|zdravim|"
    r"hezky den|diky|dekuji|mit freundlichen gruessen|mit freundlichen grussen|viele gruesse|"
    r"med venlig hilsen|venlig hilsen|mange hilsner|hilsen|mvh|kærlig hilsen|kaerlig hilsen|tak|"  # Danish
    r"met vriendelijke groet|met vriendelijke groeten|vriendelijke groet|hartelijke groet|groeten|mvg|"  # Dutch
    r"bedankt|dank je|dank u|"
    r"ystavallisin terveisin|parhain terveisin|terveisin|kiitos|kiitoksin)"  # Finnish (folded)
    r"\s*[,.!]?\s*(\S+)?\s*$")


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c)).replace("ß", "ss").replace("ü", "ue")


def _wrote_marker_span(lines: list[str], i: int) -> int:
    """Number of lines (1 to 3) of a "On ... wrote:" style header starting at line i, else 0."""
    first = lines[i].strip()
    if not first or len(first) > 400:
        return 0
    for span in (1, 2, 3):
        joined = " ".join(s.strip() for s in lines[i:i + span])
        if any(p.match(joined) for p in _WROTE):
            return span
    return 0


def _is_header_block(lines: list[str], i: int) -> bool:
    if not _HDR_FROM.match(lines[i]):
        return False
    window = lines[i + 1:i + 6]
    return any(_HDR_SENT.match(x) for x in window) and any(_HDR_OTHER.match(x) for x in window)


def _next_nonblank(lines: list[str], i: int) -> str | None:
    for x in lines[i:]:
        if x.strip():
            return x
    return None


def _find_cut(lines: list[str]) -> tuple[int, set[int]]:
    """Index of the first line that starts quoted history or a signature (else len(lines)),
    plus reply headers to drop that are followed by inline quotes."""
    seen_text = False
    drops: set[int] = set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if _ORIGINAL_MSG.match(line):
            return i, drops
        span = _wrote_marker_span(lines, i)
        if span:
            nxt = _next_nonblank(lines, i + span)
            # Inline-reply style: the quoted lines are dropped one by one and the answers stay.
            if nxt and nxt.lstrip().startswith(">"):
                drops.update(range(i, i + span))
                continue
            if seen_text:
                return i, drops
            continue
        if seen_text and _is_header_block(lines, i):
            return i, drops
        if _SIG_DELIM.match(line) and seen_text:
            return i, drops
        if seen_text and stripped and len(stripped) <= 45 and _CLOSER.match(_fold(stripped)):
            rest = [x for x in lines[i + 1:] if x.strip()]
            quoted_from = next((j for j, x in enumerate(rest) if x.lstrip().startswith(">")), len(rest))
            tail = rest[:quoted_from]
            if len(tail) <= 8 and all(len(x) <= 120 for x in tail):
                return i, drops
        if stripped and not stripped.startswith(">") and not _RULE.match(line) and not _DEVICE_SIG.match(line):
            seen_text = True
    return len(lines), drops


def _line_spans(text: str) -> list[tuple[int, int, str]]:
    out = []
    pos = 0
    for raw in text.split("\n"):
        out.append((pos, pos + len(raw), raw.rstrip("\r")))
        pos += len(raw) + 1
    return out


def _paragraphs(spans: list[tuple[int, int, str]], keep) -> list[tuple[int, int]]:
    """Group kept lines into paragraphs split at blank or dropped lines. Returns (start, end) offsets."""
    paras: list[tuple[int, int]] = []
    cur: tuple[int, int] | None = None
    for idx, (s, e, line) in enumerate(spans):
        if keep(idx, line) and line.strip():
            cur = (cur[0], e) if cur else (s, e)
        elif cur:
            paras.append(cur)
            cur = None
    if cur:
        paras.append(cur)
    return paras


_INVISIBLE = re.compile(r"[\u00ad\u034f\u180e\u200b-\u200f\u2060-\u2064\ufeff]")


def line_key(line: str) -> str:
    """A line folded for boilerplate counting: case, spacing, invisible padding and edge punctuation ignored."""
    s = _INVISIBLE.sub("", unicodedata.normalize("NFKC", line)).casefold()
    s = re.sub(r"\s+", " ", s)
    return s.strip(" \t.,;:!?-–—_*|•·()[]\"'")


def mostly_symbols(text: str, min_alnum_share: float = 0.3) -> bool:
    """True for text that is mostly punctuation or padding, like a row of commas."""
    stripped = re.sub(r"\s+", "", text)
    if not stripped:
        return True
    return sum(ch.isalnum() for ch in stripped) / len(stripped) < min_alnum_share


def kept_lines(text: str) -> list[str]:
    """The lines of a mail body that survive quote and signature stripping (before boilerplate removal)."""
    return [line for s, e in body_paragraphs(text) for line in text[s:e].split("\n")]


def body_paragraphs(text: str, boilerplate: frozenset[str] | set[str] = frozenset()) -> list[tuple[int, int]]:
    """Paragraph offsets of a mail body without quoted replies, signature and device footers.

    Lines whose `line_key` is in `boilerplate` (footers repeated across many messages) are dropped too.
    """
    spans = _line_spans(text)
    lines = [x[2] for x in spans]
    cut, drops = _find_cut(lines)

    def keep(i: int, line: str) -> bool:
        return (i < cut and i not in drops and not line.lstrip().startswith(">") and not _RULE.match(line)
                and not _DEVICE_SIG.match(line) and not mostly_symbols(line)
                and not (boilerplate and line_key(line) in boilerplate))

    paras = _paragraphs(spans, keep)
    if not paras and boilerplate:  # a message that is all boilerplate keeps its usual paragraphs
        return body_paragraphs(text)
    if not paras:  # never strip a message down to nothing
        paras = _paragraphs(spans, lambda i, line: not line.lstrip().startswith(">")) \
            or _paragraphs(spans, lambda i, line: True)
    return paras


def strip_quotes_and_signature(text: str) -> str:
    """The cleaned body as plain text, paragraphs separated by a blank line."""
    return "\n\n".join(_tidy(text[s:e]) for s, e in body_paragraphs(text))


# ------------------------------------------------------------------- chunking

def _tidy(s: str) -> str:
    s = s.replace("\r", "")
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n[ \t]+", "\n", s).strip()


def _split_long(text: str, start: int, end: int, target: int, max_chars: int) -> list[tuple[int, int]]:
    """Cut an over-long paragraph into pieces of at most max_chars at natural breaks."""
    out = []
    pos = start
    while end - pos > max_chars:
        window = text[pos + target // 2: pos + max_chars]
        base = pos + target // 2
        cut = None
        for pat in (r"\n", r"[.!?…]\s", r"[;:,]\s", r"\s"):
            ms = list(re.finditer(pat, window))
            if ms:
                cut = base + ms[-1].end()
                break
        if cut is None or cut <= pos:
            cut = pos + max_chars
        out.append((pos, cut))
        pos = cut
    if pos < end:
        out.append((pos, end))
    return out


def chunk_paragraphs(text: str, paras: list[tuple[int, int]], *, target: int = TARGET_CHARS,
                     max_chars: int = MAX_CHARS, overlap: int = OVERLAP_CHARS) -> list[Chunk]:
    """Pack paragraphs into chunks of about `target` and at most `max_chars` characters.

    A chunk starts with the last paragraph of the previous one when that is short, as overlap.
    """
    units: list[tuple[int, int]] = []
    for s, e in paras:
        units.extend(_split_long(text, s, e, target, max_chars) if e - s > max_chars else [(s, e)])
    chunks: list[Chunk] = []
    cur: list[tuple[int, int]] = []
    new = 0
    cur_len = 0

    def flush() -> None:
        nonlocal cur, new, cur_len
        if new:
            body = "\n\n".join(_tidy(text[s:e]) for s, e in cur)
            chunks.append(Chunk(body, cur[0][0], cur[-1][1]))
            last = cur[-1]
            keep = [last] if last[1] - last[0] <= overlap else []
            cur, new, cur_len = keep, 0, sum(e - s for s, e in keep)

    for u in units:
        ulen = u[1] - u[0]
        if new and (cur_len + ulen + 2 > max_chars or cur_len >= target):
            flush()
            if cur and cur_len + ulen + 2 > max_chars:
                cur, cur_len = [], 0
        cur.append(u)
        new += 1
        cur_len += ulen + 2
    flush()
    if len(chunks) > 1 and len(chunks[-1].text) < MIN_CHARS:
        chunks.pop()
    return [c for c in chunks if len(c.text) >= MIN_CHARS or len(chunks) == 1]


def chunk_body(text: str | None, boilerplate: frozenset[str] | set[str] = frozenset(), **kw) -> list[Chunk]:
    """Chunks of a mail body: quotes, signature and boilerplate lines removed, split by paragraph."""
    if not text or not text.strip():
        return []
    chunks = chunk_paragraphs(text, body_paragraphs(text, boilerplate), **kw)
    return [c for c in chunks if not mostly_symbols(c.text)]  # left when a body is nothing but symbols


def chunk_plain(text: str | None, **kw) -> list[Chunk]:
    """Chunks of attachment text: no stripping, split by paragraph."""
    if not text or not text.strip():
        return []
    spans = _line_spans(text)
    return chunk_paragraphs(text, _paragraphs(spans, lambda i, line: not mostly_symbols(line)), **kw)
