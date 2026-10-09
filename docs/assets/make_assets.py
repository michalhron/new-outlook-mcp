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
