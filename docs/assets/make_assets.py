"""Generate the README and docs figures (docs/assets/*.svg). Run: python3 docs/assets/make_assets.py docs/assets"""
import sys
from pathlib import Path

OUT = Path(sys.argv[1])
PAPER, LIFT, BAND = "#F3EFE3", "#FAF7EF", "#EEE4CF"
NAVY, RUST, STEEL, TAUPE, TEXT = "#1F3D57", "#C15A3D", "#98AAB9", "#8C7B5C", "#2A2A2A"
SERIF = "'Source Serif 4','Source Serif Pro',Georgia,serif"
SANS = "'IBM Plex Sans','Helvetica Neue',Helvetica,Arial,sans-serif"


def svg(w, h, body, title, bg=PAPER):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'role="img" aria-label="{title}"><title>{title}</title>'
            f'<rect width="{w}" height="{h}" fill="{bg}"/>{body}</svg>\n')


def t(x, y, s, size, *, color=TEXT, font=SANS, weight=400, anchor="start", spacing=0, italic=False):
    st = ' font-style="italic"' if italic else ""
    return (f'<text x="{x}" y="{y}" font-family="{font}" font-size="{size}" font-weight="{weight}" fill="{color}" '
            f'text-anchor="{anchor}" letter-spacing="{spacing}"{st}>{s}</text>')


def kicker(x, y, s, color=TAUPE):
    return f'<rect x="{x}" y="{y - 13}" width="14" height="14" fill="{RUST}"/>' + t(x + 26, y, s, 17, color=color, weight=600, spacing=3.5)


def dot(x, y, r=9, fill=NAVY):
    return f'<circle cx="{x}" cy="{y}" r="{r}" fill="{fill}"/>'


def ring(x, y, r=8, color=TAUPE, w=2.2):
    return f'<circle cx="{x}" cy="{y}" r="{r}" fill="{LIFT}" stroke="{color}" stroke-width="{w}"/>'


def line(x1, y1, x2, y2, color=NAVY, w=5, dash=None, head=True):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    out = f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{w}"{d}/>'
    if head:
        import math
        a = math.atan2(y2 - y1, x2 - x1)
        L, W = 4 * w, 2.4 * w
        bx, by = x2 - L * math.cos(a), y2 - L * math.sin(a)
        p1 = (bx + W * math.sin(a), by - W * math.cos(a))
        p2 = (bx - W * math.sin(a), by + W * math.cos(a))
        out += f'<path d="M{x2} {y2}L{p1[0]:.1f} {p1[1]:.1f}L{p2[0]:.1f} {p2[1]:.1f}z" fill="{color}"/>'
    return out


def box(x, y, w, h, *, fill="none", stroke=NAVY, sw=2.5, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>'


# ---------------------------------------------------------------- icons (96 x 96)
def icon(body, name):
    (OUT / f"icon-{name}.svg").write_text(svg(96, 96, body, name, bg=LIFT))


icon(  # one archive: three sources merge into one
    line(22, 26, 62, 46, w=4, head=False) + line(22, 48, 62, 48, w=4, head=False) + line(22, 70, 62, 50, w=4, head=False)
    + ring(20, 26, 7) + ring(20, 48, 7) + ring(20, 70, 7) + dot(68, 48, 14)
    + f'<rect x="62" y="42" width="12" height="12" fill="{RUST}"/>', "archive")
icon(  # keyword: lens over a row of words
    f'<circle cx="40" cy="40" r="20" fill="none" stroke="{NAVY}" stroke-width="6"/>'
    + line(54, 54, 78, 78, w=8, head=False)
    + f'<rect x="31" y="37" width="18" height="6" fill="{RUST}"/>', "keyword")
icon(  # meaning: nearest points within a dashed radius
    f'<circle cx="48" cy="48" r="30" fill="none" stroke="{NAVY}" stroke-width="2.5" stroke-dasharray="6 5"/>'
    + dot(34, 38, 6) + dot(60, 34, 6) + dot(58, 62, 6) + dot(36, 62, 6)
    + ring(10, 14, 5) + ring(86, 84, 5) + ring(88, 20, 5)
    + f'<rect x="41" y="41" width="14" height="14" fill="{RUST}"/>', "meaning")
icon(  # attachment: a page with its text extracted
    f'<path d="M22 10h36l18 18v58H22z" fill="none" stroke="{NAVY}" stroke-width="5" stroke-linejoin="miter"/>'
    + f'<path d="M58 10v18h18" fill="none" stroke="{NAVY}" stroke-width="5"/>'
    + f'<rect x="32" y="44" width="34" height="5" fill="{NAVY}"/><rect x="32" y="56" width="34" height="5" fill="{NAVY}"/>'
    + f'<rect x="32" y="68" width="20" height="5" fill="{RUST}"/>', "attachment")
icon(  # calendar: a grid of days, one marked
    "".join(dot(22 + c * 26, 22 + r * 26, 7) if (r, c) != (1, 2) else
            f'<rect x="{22 + c * 26 - 8}" y="{22 + r * 26 - 8}" width="16" height="16" fill="{RUST}"/>'
            for r in range(3) for c in range(3)), "calendar")
icon(  # privacy: a boundary that keeps one item out
    box(14, 14, 54, 68, dash="6 5") + dot(30, 34, 7) + dot(52, 48, 7) + dot(30, 64, 7)
    + ring(84, 30, 7) + f'<line x1="77" y1="23" x2="91" y2="37" stroke="{RUST}" stroke-width="4"/>', "privacy")
icon(  # near-live: a loop with a fresh point
    f'<path d="M78 48a30 30 0 1 1-9-21" fill="none" stroke="{NAVY}" stroke-width="6"/>'
    + f'<path d="M80 12v24H56z" fill="{NAVY}"/>' + dot(48, 48, 8, RUST), "sync")
icon(  # drafts: a path that stops for you
    line(12, 48, 54, 48, w=4, dash="6 5", head=False)
    + f'<path d="M54 26l32 22-32 22z" fill="none" stroke="{NAVY}" stroke-width="5" stroke-linejoin="miter"/>'
    + f'<rect x="8" y="41" width="14" height="14" fill="{RUST}"/>', "draft")

# ---------------------------------------------------------------- banner (1200 x 340)
b = kicker(64, 78, "OUTLOOK FOR MAC  ·  MCP SERVER")
b += t(62, 150, "new-outlook-mcp", 64, color=NAVY, font=SERIF, weight=700)
b += t(64, 200, "Claude reads your Outlook mail and calendar", 26)
b += t(64, 234, "from the files already on your Mac.", 26)
b += t(64, 290, "Local  ·  read-only  ·  no online API", 18, color=TAUPE)
# right: your Mac as a dashed boundary, sources flow into one archive, nothing goes out to the cloud
b += box(720, 60, 410, 230, fill=LIFT, dash="9 7")
b += t(736, 86, "YOUR MAC", 14, color=TAUPE, weight=600, spacing=3)
for y in (130, 180, 230):
    b += line(778, y, 900, 180, w=4, head=False)
for y in (130, 180, 230):
    b += dot(770, y, 10)
b += line(900, 180, 1030, 180, w=5)
b += f'<rect x="1040" y="164" width="32" height="32" fill="{RUST}"/>'
b += dot(900, 180, 13)
b += t(1056, 226, "archive", 15, color=TAUPE, anchor="middle")
b += ring(1160, 40, 9) + ring(1172, 312, 9) + ring(690, 318, 9)
(OUT / "banner.svg").write_text(svg(1200, 340, b, "new-outlook-mcp"))

# ---------------------------------------------------------------- flow (1200 x 380)
f = kicker(48, 52, "HOW IT WORKS")
f += t(46, 100, "Copies, reads, answers. Nothing is written back.", 32, color=NAVY, font=SERIF, weight=700)
f += box(36, 140, 940, 190, fill=LIFT, dash="9 7")
f += t(52, 166, "YOUR MAC", 14, color=TAUPE, weight=600, spacing=3)
nodes = [(130, "Outlook's files", "never written"), (305, "Snapshot", "a private copy"),
         (480, "Importers", "legacy · HxStore · ICS"), (660, "archive.db", "full text + vectors"),
         (840, "MCP tools", "read-only")]
for (x1, *_), (x2, *_) in zip(nodes, nodes[1:]):
    f += line(x1 + 16, 236, x2 - 18, 236)
for x, a, s in nodes:
    f += (f'<rect x="{x - 14}" y="{222}" width="28" height="28" fill="{RUST}"/>' if a == "archive.db" else dot(x, 236, 14))
    f += t(x, 204, a, 19, weight=600, anchor="middle") + t(x, 284, s, 15, color=TAUPE, anchor="middle")
f += line(856, 236, 1060, 236, color=NAVY)
f += dot(1090, 236, 16) + t(1090, 204, "Claude", 19, weight=600, anchor="middle")
f += t(1090, 284, "asks and reads", 15, color=TAUPE, anchor="middle")
f += ring(1090, 350, 9) + t(1076, 356, "Microsoft servers: not contacted", 15, color=TAUPE, anchor="end", italic=True)
(OUT / "flow.svg").write_text(svg(1200, 380, f, "How it works"))

# ---------------------------------------------------------------- semantic (1200 x 470)
s = kicker(48, 52, "SEARCH BY MEANING")
s += t(46, 100, "Describe the message. Search finds it by meaning.", 32, color=NAVY, font=SERIF, weight=700)
panels = [(36, "1", "Nearest passages", "Your question lands near passages", "with the same meaning."),
          (428, "2", "Two rankings", "Meaning and keywords rank", "messages on their own."),
          (820, "3", "One list", "Messages both agree on rise.", "Each shows its best passage.")]
for x, n, head, l1, l2 in panels:
    s += box(x, 130, 344, 250, fill=LIFT, stroke="none")
    s += t(x + 18, 160, f"STEP {n} OF 3", 13, color=TAUPE, weight=600, spacing=3)
    s += t(x + 18, 190, head, 22, color=NAVY, font=SERIF, weight=700)
    s += t(x + 18, 410, l1, 16) + t(x + 18, 432, l2, 16)
# panel 1: dashed radius around the question
cx, cy = 208, 290
s += f'<circle cx="{cx}" cy="{cy}" r="62" fill="none" stroke="{NAVY}" stroke-width="2.5" stroke-dasharray="7 6"/>'
for dx, dy in ((-34, -26), (30, -36), (38, 22), (-28, 34), (4, 46)):
    s += dot(cx + dx, cy + dy, 8)
for dx, dy in ((-130, -60), (120, -70), (140, 40), (-140, 50), (-90, 72), (100, 74)):
    s += ring(cx + dx, cy + dy, 7)
s += f'<rect x="{cx - 9}" y="{cy - 9}" width="18" height="18" fill="{RUST}"/>'
# panel 2: two ranked columns, lines join the same message
lx, rx, top = 520, 700, 230
s += t(lx, 218, "meaning", 14, color=TAUPE, anchor="middle") + t(rx, 218, "keywords", 14, color=TAUPE, anchor="middle")
left, right = ["a", "b", "c", "d", "e"], ["c", "f", "a", "g", "h"]
for i in range(5):
    s += dot(lx, top + 18 + i * 28, 8)
    s += f'<rect x="{rx - 8}" y="{top + 10 + i * 28}" width="16" height="16" fill="{NAVY}"/>'
for i, m in enumerate(left):
    if m in right:
        j = right.index(m)
        s += f'<line x1="{lx + 12}" y1="{top + 18 + i * 28}" x2="{rx - 12}" y2="{top + 18 + j * 28}" stroke="{STEEL}" stroke-width="3"/>'
# panel 3: fused list, the two shared messages on top
fx = 860
for i in range(5):
    y = top + 18 + i * 28
    shared = i < 2
    s += (f'<rect x="{fx - 8}" y="{y - 8}" width="16" height="16" fill="{RUST}"/>' if shared else dot(fx, y, 8))
    s += f'<rect x="{fx + 22}" y="{y - 4}" width="{220 - i * 30}" height="8" fill="{NAVY if shared else STEEL}"/>'
s += line(380, 290, 420, 290, w=4) + line(772, 290, 812, 290, w=4)
(OUT / "semantic.svg").write_text(svg(1200, 470, s, "How search by meaning works"))
print("ok")


# ================================================================ doc figures
MONO = "'IBM Plex Mono',Menlo,Consolas,monospace"


def header(kick, title):
    return kicker(48, 52, kick) + t(46, 100, title, 32, color=NAVY, font=SERIF, weight=700)


def node(x, y, name, sub=None, shape="dot", size=13, font=SANS, nsize=17):
    if shape == "square":
        g = f'<rect x="{x - size}" y="{y - size}" width="{2 * size}" height="{2 * size}" fill="{RUST}"/>'
    elif shape == "ring":
        g = ring(x, y, size - 3)
    else:
        g = dot(x, y, size)
    g += t(x, y - size - 12, name, nsize, weight=600, anchor="middle", font=font)
    if sub:
        g += t(x, y + size + 22, sub, 14, color=TAUPE, anchor="middle")
    return g


def chain(points, y=None, w=4, color=NAVY, gap=18):
    out = ""
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        import math
        a = math.atan2(y2 - y1, x2 - x1)
        out += line(x1 + gap * math.cos(a), y1 + gap * math.sin(a), x2 - (gap + 2) * math.cos(a),
                    y2 - (gap + 2) * math.sin(a), w=w, color=color)
    return out


def band(x, y, w, h, label):
    return box(x, y, w, h, fill=LIFT, stroke="none") + t(x + 16, y + 26, label, 13, color=TAUPE, weight=600, spacing=3)


def save(name, w, h, body, title):
    (OUT / f"{name}.svg").write_text(svg(w, h, body, title))


# ---------------------------------------------------------------- semantic pipeline
g = header("HOW IT WORKS", "Index once. Then every question is a short lookup.")
g += band(36, 130, 1128, 170, "INDEXING  ·  ONCE, THEN AFTER EACH SYNC")
ix = [(110, "Message", None, "dot"), (300, "Parts", "subject · body · attachments", "dot"),
      (490, "Clean", "quotes, signatures out", "dot"), (680, "Chunks", "about 700 characters", "dot"),
      (870, "Model", "passage: …", "dot"), (1060, "Vectors", "in archive.db", "square")]
g += chain([(x, 220) for x, *_ in ix])
for x, n, s_, sh in ix:
    g += node(x, 220, n, s_, sh)
g += band(36, 320, 1128, 260, "QUERYING  ·  EVERY SEARCH")
y1, y2 = 400, 510
g += node(110, 455, "Question")
qa = [(300, "Model", "query: …"), (490, "Nearest chunks", "cosine similarity"),
      (680, "Best per message", "its passage is the snippet"), (870, "Filters", "and privacy rules")]
qb = [(300, "Keyword query", "OR of the words"), (490, "bm25 ranking", "full-text index"),
      (870, "Filters", "and privacy rules")]
g += chain([(110, 455), (300, y1)]) + chain([(x, y1) for x, *_ in qa])
g += chain([(110, 455), (300, y2)]) + chain([(300, y2), (490, y2), (870, y2)])
for x, n, s_ in qa:
    g += node(x, y1, n, s_)
for x, n, s_ in qb:
    g += node(x, y2, n, s_)
g += chain([(870, y1), (1010, 455)]) + chain([(870, y2), (1010, 455)])
g += node(1010, 455, "Fusion", "RRF", "square") + chain([(1010, 455), (1110, 455)]) + node(1110, 455, "Results")
save("semantic-pipeline", 1200, 610, g, "Indexing and querying for search by meaning")

# ---------------------------------------------------------------- chunking
g = header("CHUNKING", "Keep what the author wrote. Cut it into passages.")
g += band(36, 130, 570, 330, "ONE MAIL BODY")
yy = 172
def bars(x, y, widths, color):
    return "".join(f'<rect x="{x}" y="{y + i * 16}" width="{w_}" height="8" fill="{color}"/>' for i, w_ in enumerate(widths))
g += bars(60, yy, [380, 360, 340], NAVY) + bars(60, yy + 58, [370, 240], NAVY)
g += t(60, yy + 118, "On Monday, a colleague wrote:", 15, color=TAUPE, italic=True)
g += bars(76, yy + 132, [350, 330, 280], STEEL) + t(60, yy + 141, "&gt;", 15, color=TAUPE)
g += t(60, yy + 202, "Best regards", 15, color=TAUPE, italic=True) + bars(60, yy + 214, [160, 220], STEEL)
for y0, y1_, lab, keep in ((yy - 4, yy + 84, "kept", True), (yy + 102, yy + 176, "quote, dropped", False),
                          (yy + 188, yy + 238, "signature, dropped", False)):
    c = RUST if keep else TAUPE
    g += f'<rect x="456" y="{y0}" width="4" height="{y1_ - y0}" fill="{c}"/>'
    g += t(468, (y0 + y1_) / 2 + 5, lab, 13, color=c, weight=600)
g += line(616, 295, 664, 295)
g += band(680, 130, 484, 330, "CHUNKS THAT GET EMBEDDED")
cy0 = 170
g += f'<rect x="688" y="{cy0}" width="456" height="40" fill="none" stroke="{NAVY}" stroke-width="2"/>'
g += t(704, cy0 + 26, "subject  ·  its own chunk", 15, color=NAVY, weight=600)
g += f'<rect x="688" y="{cy0 + 56}" width="456" height="62" fill="none" stroke="{NAVY}" stroke-width="2"/>'
g += bars(704, cy0 + 70, [420, 400], NAVY) + f'<rect x="704" y="{cy0 + 102}" width="300" height="8" fill="{RUST}"/>'
g += f'<rect x="688" y="{cy0 + 134}" width="456" height="62" fill="none" stroke="{NAVY}" stroke-width="2"/>'
g += f'<rect x="704" y="{cy0 + 148}" width="300" height="8" fill="{RUST}"/>' + bars(704, cy0 + 164, [410, 260], NAVY)
g += t(1016, cy0 + 110, "overlap", 13, color=RUST, weight=600)
g += f'<rect x="688" y="{cy0 + 212}" width="456" height="62" fill="none" stroke="{NAVY}" stroke-width="2" stroke-dasharray="6 5"/>'
g += t(704, cy0 + 236, "report.pdf", 14, color=TAUPE, weight=600, font=MONO) + bars(704, cy0 + 248, [400, 340], NAVY)
g += t(46, 498, "Body chunks hold about 700 and at most 1,000 characters. A short last paragraph repeats at the start of the next chunk.", 16)
g += t(46, 522, "Attachment chunks carry the file name. Patterns cover English, Czech, German, French, Spanish, Italian, Danish, Dutch and Finnish.", 16)
save("chunking", 1200, 550, g, "How a message becomes chunks")

# ---------------------------------------------------------------- RRF
g = header("RECIPROCAL RANK FUSION", "Messages both rankings agree on rise to the top.")
g += band(36, 130, 470, 330, "TWO RANKINGS")
meaning, keywords = list("ABCDE"), list("CFAGB")
g += t(170, 180, "meaning", 15, color=TAUPE, anchor="middle") + t(370, 180, "keywords", 15, color=TAUPE, anchor="middle")
for i in range(5):
    y = 214 + i * 46
    g += t(70, y + 5, str(i + 1), 15, color=TAUPE, anchor="middle")
    g += dot(170, y, 15) + t(170, y + 6, meaning[i], 15, color=LIFT, weight=700, anchor="middle")
    g += f'<rect x="355" y="{y - 15}" width="30" height="30" fill="{NAVY}"/>' + t(370, y + 6, keywords[i], 15, color=LIFT, weight=700, anchor="middle")
for i, m in enumerate(meaning):
    if m in keywords:
        j = keywords.index(m)
        g += f'<line x1="188" y1="{214 + i * 46}" x2="352" y2="{214 + j * 46}" stroke="{STEEL}" stroke-width="3"/>'
scores = {}
for lst in (meaning, keywords):
    for r, m in enumerate(lst, 1):
        scores[m] = scores.get(m, 0) + 1 / (60 + r)
order = sorted(scores, key=lambda m: (-scores[m], (meaning + keywords).index(m)))
g += band(536, 130, 628, 330, "FUSED SCORE")
mx = max(scores.values())
for i, m in enumerate(order):
    y = 176 + i * 40
    both = m in meaning and m in keywords
    w_ = 380 * scores[m] / mx
    g += t(570, y + 6, m, 16, weight=700, anchor="middle", color=NAVY)
    g += f'<rect x="596" y="{y - 9}" width="{w_:.0f}" height="18" fill="{RUST if both else STEEL}"/>'
    g += t(606 + w_, y + 6, f"{scores[m]:.4f}", 14, color=TAUPE, font=MONO)
g += t(46, 500, "score(message) = sum over rankings of 1 / (60 + rank)", 18, font=MONO, color=NAVY)
g += t(46, 528, "A: 1/61 + 1/63 = 0.0323.  F, found by keywords only at rank 2: 1/62 = 0.0161.  Rust bars: found by both.", 16)
save("rrf", 1200, 556, g, "Reciprocal Rank Fusion with a worked example")

# ---------------------------------------------------------------- architecture
g = header("DATA FLOW", "From Outlook's files to a tool answer.")
top = [(75, "sources", "Outlook files, ICS"), (230, "snapshot", "private copy"), (395, "importer", "one per source"),
       (575, "sync.run_import", None), (755, "archive.db", "SQLite + FTS5"), (920, "tools", "caltools · semantic"),
       (1080, "server.py · cli.py", "MCP and command line")]
g += chain([(x, 200) for x, *_ in top])
for x, n, s_ in top:
    g += node(x, 200, n, s_, "square" if n == "archive.db" else "dot", font=MONO if "." in n or n == "tools" else SANS, nsize=16)
g += t(395, 262, "yields MessageRecord, EventRecord", 13, color=TAUPE, anchor="middle", font=MONO)
steps = [("privacy filter", "drop excluded records"), ("Archive.upsert", "dedup and merge"),
         ("finish_files, finish_events", "orphan files, feed pruning"), ("rebuild_instances", "recurrence expansion"),
         ("embed new mail", "if search by meaning is set up")]
g += f'<rect x="573" y="232" width="4" height="{len(steps) * 48 - 8}" fill="{NAVY}"/>'
for i, (n, s_) in enumerate(steps):
    y = 262 + i * 48
    g += f'<rect x="577" y="{y - 2}" width="34" height="4" fill="{NAVY}"/>' + dot(619, y, 7, RUST if i == 0 else NAVY)
    g += t(637, y + 5, n, 15, font=MONO, weight=600) + t(637 + 9.2 * len(n) + 14, y + 5, s_, 14, color=TAUPE)
g += t(46, 530, "Triggers: new-outlook sync, the sync_now tool, the watcher, and the 36-hour LaunchAgent.", 16)
save("architecture", 1200, 560, g, "Data flow through the code")

# ---------------------------------------------------------------- coverage
g = header("WHAT EACH SOURCE COVERS", "The archive keeps everything any source has seen.")
x0, xs, x2m, xn = 260, 760, 1010, 1120  # axis start, legacy stop, two months ago, today
rows = [("Legacy archive", 180), ("New Outlook cache", 240), ("Cached files", 300), ("ICS feed", 360), ("Archive", 430)]
for name, y in rows:
    g += t(240, y + 5, name, 16, weight=600, anchor="end", color=RUST if name == "Archive" else TEXT)
    g += f'<line x1="{x0}" y1="{y}" x2="{xn}" y2="{y}" stroke="{BAND}" stroke-width="2"/>'
g += f'<rect x="{x0}" y="171" width="{xs - x0}" height="18" fill="{NAVY}"/>'
for x in (300, 380, 430, 520, 560, 610, 650, 700, 735, 790, 840, 880, 930, 970):
    g += ring(x, 240, 6)
g += f'<rect x="{x2m}" y="231" width="{xn - x2m}" height="18" fill="{NAVY}"/>'
for x in (280, 340, 410, 470, 500, 590, 640, 690, 760, 800, 860, 900, 950, 990, 1040, 1080):
    g += ring(x, 300, 5, STEEL)
g += f'<rect x="{x2m - 260}" y="352" width="{xn - x2m + 260}" height="16" fill="{STEEL}"/>'
g += f'<rect x="{x0}" y="421" width="{xn - x0}" height="18" fill="{RUST}"/>'
for x, lab in ((x0, "years back"), (xs, "legacy client stops"), (x2m, "2 months ago"), (xn, "today")):
    g += f'<line x1="{x}" y1="150" x2="{x}" y2="460" stroke="{TAUPE}" stroke-width="1.5" stroke-dasharray="4 5"/>'
    g += t(x, 484, lab, 14, color=TAUPE, anchor="middle")
g += t(46, 528, "Dense bars: full coverage. Circles: items you opened, searched for or that Outlook kept. Illustrative, not to scale.", 15, color=TAUPE, italic=True)
save("coverage", 1200, 556, g, "Time coverage of each source")

# ---------------------------------------------------------------- watcher
g = header("NEAR-LIVE SYNC", "Wait for Outlook to go quiet, then sync.")
ax0, scale = 80, 6.0  # seconds to pixels
def sx(s_):
    return ax0 + s_ * scale
g += t(sx(0), 168, "OUTLOOK WRITES", 13, color=TAUPE, weight=600, spacing=3)
for s_ in (2, 4, 7, 9, 60, 63, 66, 124, 127):
    g += f'<rect x="{sx(s_) - 2}" y="182" width="4" height="34" fill="{TAUPE}"/>'
g += t(sx(0), 268, "SYNCS", 13, color=TAUPE, weight=600, spacing=3)
for s_, d in ((29, 12), (89, 12)):
    g += f'<rect x="{sx(s_)}" y="282" width="{d * scale}" height="34" fill="{NAVY}"/>'
g += f'<rect x="{sx(149)}" y="282" width="{6 * scale}" height="34" fill="{RUST}"/>'
g += t(sx(152), 340, "torn copy", 14, color=RUST, anchor="middle", weight=600)
def span(a, b, y, lab, color=NAVY):
    return (f'<line x1="{sx(a)}" y1="{y}" x2="{sx(b)}" y2="{y}" stroke="{color}" stroke-width="2"/>'
            f'<line x1="{sx(a)}" y1="{y - 7}" x2="{sx(a)}" y2="{y + 7}" stroke="{color}" stroke-width="2"/>'
            f'<line x1="{sx(b)}" y1="{y - 7}" x2="{sx(b)}" y2="{y + 7}" stroke="{color}" stroke-width="2"/>'
            + t((sx(a) + sx(b)) / 2, y - 10, lab, 14, anchor="middle", color=color))
g += span(9, 29, 240, "20 s quiet")
g += span(66, 86, 240, "20 s quiet")
g += span(29, 89, 372, "at least 60 s between sync starts")
g += f'<line x1="{sx(155)}" y1="299" x2="{sx(170)}" y2="299" stroke="{RUST}" stroke-width="3" stroke-dasharray="6 5"/>'
g += t(sx(156), 276, "then back off 5 min", 14, color=RUST)
for s_ in range(0, 171, 30):
    g += f'<rect x="{sx(s_) - 1}" y="410" width="2" height="10" fill="{TAUPE}"/>' + t(sx(s_), 440, f"{s_} s", 13, color=TAUPE, anchor="middle")
g += f'<rect x="{sx(0)}" y="408" width="{170 * scale}" height="2" fill="{TAUPE}"/>'
g += t(46, 486, "Each sync runs as a low-priority child process and stops after 10 minutes. A 36-hour LaunchAgent is the fallback.", 16)
save("watcher", 1200, 512, g, "Watcher timing")

# ---------------------------------------------------------------- privacy
g = header("PRIVACY SCOPES", "Excluded mail is filtered twice and can be purged.")
g += band(36, 130, 1128, 300, "")
pts = [(110, "Outlook", "mail and events"), (330, "Import filter", "rules at sync time"), (560, "archive.db", None),
       (790, "Query filter", "rules at every call"), (1010, "Tools", "what Claude can see")]
g += chain([(x, 240) for x, *_ in pts])
for x, n, s_ in pts:
    if "filter" in n:
        g += f'<rect x="{x - 4}" y="196" width="8" height="88" fill="none"/>'
        g += f'<line x1="{x}" y1="196" x2="{x}" y2="288" stroke="{NAVY}" stroke-width="3" stroke-dasharray="7 6"/>'
        g += t(x, 160, n, 17, weight=600, anchor="middle") + t(x, 180, s_, 14, color=TAUPE, anchor="middle")
    else:
        g += node(x, 240, n, s_, "square" if n == "archive.db" else "dot", font=MONO if "." in n else SANS)
g += ring(370, 330, 8) + f'<line x1="362" y1="322" x2="378" y2="338" stroke="{RUST}" stroke-width="3"/>'
g += line(340, 262, 366, 318, w=2.5, color=RUST, head=False) + t(394, 336, "dropped, never stored", 14, color=RUST)
g += ring(830, 330, 8) + f'<line x1="822" y1="322" x2="838" y2="338" stroke="{RUST}" stroke-width="3"/>'
g += line(800, 262, 826, 318, w=2.5, color=RUST, head=False) + t(854, 336, "hidden at once, for rules added later", 14, color=RUST)
g += f'<path d="M560 268 V392 H640" fill="none" stroke="{NAVY}" stroke-width="3" stroke-dasharray="7 6"/>'
g += t(652, 398, "purge-excluded deletes stored matches, with their chunks and vectors", 14, color=NAVY)
save("privacy", 1200, 460, g, "How privacy rules apply")

# ---------------------------------------------------------------- HxStore block
g = header("HXSTORE BLOCK", "A CRC-checked header, a key and an LZ4 payload.")
px = 14
fields = [("CRC", 4, RUST), ("CRC", 4, RUST), ("magic", 8, NAVY), ("key len", 4, STEEL), ("comp", 4, STEEL),
          ("infl", 4, STEEL), ("codec", 4, STEEL), ("key", 8, NAVY)]
x = 60
y = 190
starts = []
for name, n, c in fields:
    starts.append(x)
    g += f'<rect x="{x}" y="{y}" width="{n * px - 3}" height="56" fill="{c}"/>'
    g += t(x + (n * px - 3) / 2, y + 34, name, 13 if n == 4 else 15, color=LIFT, weight=600, anchor="middle")
    x += n * px
starts.append(x)
g += f'<rect x="{x}" y="{y}" width="{1140 - x}" height="56" fill="none" stroke="{NAVY}" stroke-width="2.5" stroke-dasharray="7 6"/>'
g += t((x + 1140) / 2, y + 34, "LZ4 payload  ·  compressed length bytes", 15, color=NAVY, weight=600, anchor="middle")
for off, xx in (("+0x00", starts[0]), ("+0x04", starts[1]), ("+0x08", starts[2]), ("+0x10", starts[3]),
                ("+0x20", starts[7])):
    g += t(xx, y - 12, off, 13, color=TAUPE, font=MONO)
def brace(a, b, yb, lab, color):
    return (f'<path d="M{a} {yb - 10} V{yb} H{b} V{yb - 10}" fill="none" stroke="{color}" stroke-width="2.5"/>'
            + t(a, yb + 24, lab, 14, color=color))
g += brace(starts[1], starts[7] - 3, 280, "first CRC-32 covers bytes 0x04 to 0x20", RUST)
g += brace(starts[2], 1140, 340, "second CRC-32 covers the magic, the key and the payload", NAVY)
g += t(46, 416, "Header length is 0x20 plus the key length (8 or 16 bytes). Blocks start on 512-byte boundaries. Codec 4 is raw LZ4.", 16)
save("hxstore-block", 1200, 444, g, "Layout of one HxStore block")

# ---------------------------------------------------------------- calendar merge
g = header("CALENDAR", "One event per UID, expanded into occurrences.")
src = [("New Outlook", "priority 1", 190), ("ICS feed", "priority 2", 260), ("Legacy archive", "priority 3", 330)]
for n, p_, y in src:
    g += dot(210, y, 11) + t(186, y + 5, n, 16, weight=600, anchor="end") + t(186, y + 24, p_, 13, color=TAUPE, anchor="end")
    g += line(226, y, 404, 260, w=3, color=NAVY if p_ == "priority 1" else STEEL, head=False)
g += f'<rect x="404" y="244" width="32" height="32" fill="{RUST}"/>'
g += t(420, 228, "merged by UID", 15, weight=600, anchor="middle") + t(420, 300, "higher priority wins", 13, color=TAUPE, anchor="middle")
g += line(452, 260, 560, 260)
g += band(580, 140, 584, 250, "WEEKLY SERIES, EXPANDED IN ITS OWN TIMEZONE")
for i in range(7):
    x = 620 + i * 76
    g += t(x, 196, f"week {i + 1}", 13, color=TAUPE, anchor="middle")
    if i == 3:
        g += ring(x, 250, 8) + f'<rect x="{x + 6}" y="{276}" width="22" height="22" fill="{RUST}"/>'
        g += line(x + 8, 256, x + 14, 274, w=2, color=RUST, head=False)
    elif i == 5:
        g += ring(x, 250, 8) + f'<line x1="{x - 8}" y1="242" x2="{x + 8}" y2="258" stroke="{RUST}" stroke-width="3"/>'
    else:
        g += dot(x, 250, 10)
g += t(848, 330, "moved occurrence", 13, color=RUST, anchor="middle") + t(1000, 330, "cancelled date", 13, color=RUST, anchor="middle")
g += t(46, 440, "A modified occurrence is stored as its own event and replaces the regular one on that day. Times hold across daylight saving changes.", 16)
save("calendar", 1200, 468, g, "Calendar merge and recurrence expansion")
print("figures ok")

# ---------------------------------------------------------------- realms
g = header("WORK AND PRIVATE", "One archive. Work by default, private on request.")
g += band(36, 130, 1128, 270, "")
g += dot(330, 205, 14) + t(300, 200, "Outlook", 17, weight=600, anchor="end") + t(300, 222, "work", 14, color=TAUPE, anchor="end")
g += ring(330, 315, 11) + t(300, 310, "HEY, via mcp-hey", 17, weight=600, anchor="end")
g += t(300, 332, "private  ·  only what you read", 14, color=TAUPE, anchor="end")
g += chain([(330, 205), (540, 260)], gap=24) + chain([(330, 315), (540, 260)], gap=24)
g += f'<rect x="526" y="246" width="28" height="28" fill="{RUST}"/>' + t(540, 230, "archive.db", 16, weight=600,
                                                                             anchor="middle", font=MONO)
g += t(540, 302, "one index", 14, color=TAUPE, anchor="middle")
rows = [(185, "default", "work", [1, 1, 1, 1, 1]), (260, 'realm="private"', "private", [0, 0, 0]),
        (335, 'realm="all"', "both", [1, 0, 1, 1, 0, 1, 0])]
for y, lab, sub_, marks in rows:
    g += line(570, 260, 700, y, w=3, color=NAVY if lab == "default" else STEEL, head=False)
    g += t(720, y + 5, lab, 15, font=MONO, weight=600) + t(720, y + 25, sub_, 13, color=TAUPE)
    for i, m in enumerate(marks):
        x = 910 + i * 34
        g += dot(x, y, 9) if m else ring(x, y, 8)
g += t(46, 440, "A server started with --realm work returns work mail only, whatever a call asks for.", 16)
g += t(46, 466, "Accounts without a realm are hidden behind a fence and shown only when a search covers all mail.", 16)
save("realms", 1200, 494, g, "Work and private realms")
print("realms ok")
