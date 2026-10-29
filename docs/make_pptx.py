"""docs/presentation.md as a PowerPoint deck in Aiven's brand template, docs/presentation.pptx.

The text (titles, bullets, the table, the speaker's notes, the animations' captions) comes from the Markdown;
the diagrams are drawn here as native shapes, in the brand palette, and the two "step by step" animations
become one slide each, their steps appearing on click. Run with python-pptx:

    uv run --with python-pptx docs/make_pptx.py [TEMPLATE.pptx]
"""

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

DOCS = Path(__file__).resolve().parent
TEMPLATE = Path.home() / "Documents/MASTER Aiven Brand 2.0 Slides Template.pptx"
OUT = DOCS / "presentation.pptx"

# The brand palette (the template's theme).
DARK, WHITE, GREY, LINE = "05080F", "FFFFFF", "919294", "444547"
GREEN, TEAL, PURPLE, PINK, ORANGE = "5FFA74", "2ED0CD", "6F64FF", "DF56F2", "FF965E"
TEXT, MONO = "Inter", "Geist Mono Medium"

COVER, CONTENT, CLOSING = "TITLE_2_2_1_1", "TITLE_AND_BODY_1_4_1_1_1", "BLANK_1_1_1"
EYEBROWS = [
    "", "THE PROBLEM", "NEVIA", "NEVIA", "THE PATH", "THE DATA · M2P", "THE CODE · ACC", "VALIDATE, CUT OVER",
    "HOW IT RUNS", "WHAT NEXT",
]  # fmt: skip


# ---------------------------------------------------------------------- the Markdown


@dataclass
class Slide:
    title: str
    subtitle: str
    bullets: list[str]
    table: list[list[str]]
    notes: str
    animation: tuple[str, int] | None  # (m2p or acc, frame)
    caption: str
    after: str  # text after the diagram (a frame's caption, or a sentence under a table)


def read(md: Path) -> list[Slide]:
    text = md.read_text()
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)  # the front matter
    text = re.sub(r"\A\s*<!--.*?-->\s*", "", text, flags=re.S)  # the deck's own comment
    slides = []
    for chunk in re.split(r"\n---\n", text):
        anim = re.search(r"<!-- animation: (\w+), frame (\d+)/\d+ -->", chunk)
        notes = " ".join(n.strip() for n in re.findall(r"<!--(.*?)-->", chunk, re.S) if "animation:" not in n)
        body = re.sub(r"<!--.*?-->", "", chunk, flags=re.S)
        body = re.sub(r"```.*?```", "", body, flags=re.S)
        title = (re.search(r"^# (.+)$", body, re.M) or [""])[0].removeprefix("# ")
        subtitle = (re.search(r"^### (.+)$", body, re.M) or [""])[0].removeprefix("### ")
        bullets, table, after, item = [], [], [], None
        for line in body.splitlines():
            if line.startswith("- "):
                item = line[2:]
                bullets.append(item)
            elif line.startswith("  ") and line.strip() and bullets:
                bullets[-1] += " " + line.strip()
            elif line.startswith("|"):
                cells = [c.strip() for c in line.strip("|").split("|")]
                if not all(re.fullmatch(r"-+", c) for c in cells):
                    table.append(cells)
            elif line.strip() and not line.startswith("#"):
                after.append(line.strip())
        caption = " ".join(after) if anim else ""
        slides.append(Slide(title, subtitle, bullets, table, " ".join(notes.split()),
                            (anim[1], int(anim[2])) if anim else None, caption, " ".join(after)))  # fmt: skip
    return slides


# ---------------------------------------------------------------------- text


def rgb(hex_: str) -> RGBColor:
    return RGBColor.from_string(hex_)


def runs(paragraph, text: str, size: float, color: str = WHITE, *, bold: bool = False) -> None:
    """`text` into `paragraph`, its **bold** and `code` as such (code in the mono font, green)."""
    for part in re.split(r"(\*\*.+?\*\*|`[^`]+`)", text):
        if not part:
            continue
        r = paragraph.add_run()
        code = part.startswith("`")
        r.text = part.strip("*`") if part.startswith(("**", "`")) else part
        f = r.font
        f.size, f.color.rgb = Pt(size), rgb(GREEN if code else color)
        f.bold = bold or part.startswith("**")
        f.name = MONO if code else TEXT


def fill_text(frame, lines: list[str], size: float, color: str = WHITE, *, space: float = 6, bullet: bool = False):  # noqa: PLR0913
    frame.clear()
    frame.word_wrap = True
    for i, line in enumerate(lines):
        p = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
        if bullet:
            r = p.add_run()
            r.text = "▸  "
            r.font.size, r.font.color.rgb, r.font.name = Pt(size), rgb(GREEN), TEXT
        runs(p, line, size, color)
        p.space_after = Pt(space)


def textbox(  # noqa: PLR0913, PLR0917 - where it goes, and what
    slide, x, y, w, h, lines, size, color=WHITE, *, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, fill=None
):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.margin_left = tf.margin_right = Inches(0.05)
    tf.margin_top = tf.margin_bottom = Inches(0.02)
    tf.vertical_anchor = anchor
    fill_text(tf, lines, size, color, space=2)
    for p in tf.paragraphs:
        p.alignment = align
    if fill:
        tb.fill.solid()
        tb.fill.fore_color.rgb = rgb(fill)
    return tb


# ---------------------------------------------------------------------- diagrams

# Columns and rows of the diagrams' grid, in inches.
COLS = [0.35, 2.25, 4.15, 6.05, 7.95]
ROWS = [1.28, 2.5, 3.72]
W, H = 1.45, 0.72

KIND = {  # shape, outline
    "you": (MSO_SHAPE.ROUNDED_RECTANGLE, WHITE),
    "computer": (MSO_SHAPE.ROUNDED_RECTANGLE, TEAL),
    "db": (MSO_SHAPE.CAN, GREEN),
    "ai": (MSO_SHAPE.ROUNDED_RECTANGLE, PURPLE),
    "check": (MSO_SHAPE.DIAMOND, PINK),
    "result": (MSO_SHAPE.ROUNDED_RECTANGLE, ORANGE),
}


@dataclass
class Node:
    id: str
    name: str
    detail: str
    kind: str
    col: int
    row: int
    frame: int = 0

    @property
    def box(self) -> tuple[float, float, float, float]:
        return COLS[self.col], ROWS[self.row], W, H


@dataclass
class Edge:
    a: str
    b: str
    label: str = ""
    frame: int = 0
    dashed: bool = False


M2P = (
    [
        Node("you", "Your machine", "agent m2p", "you", 0, 1, 1),
        Node("agent", "m2p-agent", "the AI agent, mongo2pg", "computer", 1, 1, 1),
        Node("mongo", "m2p-mongo", "MongoDB and its data", "db", 2, 0, 2),
        Node("pg", "m2p-pg", "the target", "db", 2, 2, 2),
        Node("infer", "mongo2pg infer", "a model, warnings", "ai", 3, 0, 3),
        Node("branch", "m2p-tryN", "a branch of m2p-pg", "db", 3, 2, 4),
        Node("report", "report", "post-import", "check", 4, 1, 5),
        Node("fix", "fix the data", "config, normalize.sql", "ai", 4, 2, 5),
        Node("finish", "DONE.md, final.sql", "in build/m2p-runs/", "result", 4, 0, 6),
    ],
    [
        Edge("you", "agent", "computer, key, code", 1),
        Edge("agent", "mongo", "makes", 2),
        Edge("agent", "pg", "makes", 2),
        Edge("mongo", "infer", "samples", 3),
        Edge("pg", "branch", "branch.sh N", 4, dashed=True),
        Edge("infer", "branch", "export, import", 4),
        Edge("branch", "report", "", 5),
        Edge("report", "fix", "fails", 5),
        Edge("fix", "branch", "next try", 5),
        Edge("report", "finish", "passes", 6),
    ],
)

ACC = (
    [
        Node("you", "Your machine", "agent acc", "you", 0, 1, 1),
        Node("agent", "acc-agent", "runs the run", "computer", 1, 1, 1),
        Node("gh", "GitHub", "the app's repository", "db", 1, 0, 2),
        Node("app", "acc-app", "the app, from main", "computer", 2, 1, 2),
        Node("mongo", "acc-mongo", "its MongoDB, seeded", "db", 2, 0, 2),
        Node("infer", "mongo2pg infer", "without the values", "ai", 3, 0, 3),
        Node("ai", "the AI, as acc-ai", "code and tests", "ai", 3, 1, 4),
        Node("dev", "acc-app-db-RUN", "its own Postgres", "db", 4, 0, 4),
        Node("build", "mvn package", "with the tests", "computer", 4, 1, 5),
        Node("sb", "acc-app-RUN", "sandbox, a checkpoint", "computer", 3, 2, 6),
        Node("sbdb", "acc-pg-RUN", "with the seed data", "db", 2, 2, 6),
        Node("pr", "Pull request", "on GitHub, never merged", "result", 4, 2, 7),
    ],
    [
        Edge("you", "agent", "key, code", 1),
        Edge("gh", "agent", "clone", 2),
        Edge("agent", "app", "recreates", 2),
        Edge("app", "mongo", "chisel", 2, dashed=True),
        Edge("mongo", "infer", "samples", 3),
        Edge("infer", "ai", "schemas", 4),
        Edge("ai", "dev", "checks", 4, dashed=True),
        Edge("ai", "build", "the change", 5),
        Edge("app", "sb", "checkpoint", 6),
        Edge("build", "sb", "the jar", 6),
        Edge("sb", "sbdb", "", 6),
        Edge("sb", "pr", "it answers", 7),
    ],
)

PATH = (
    [
        Node("a", "1. Assess", "mongo2pg infer", "ai", 0, 0),
        Node("d", "2. Move the data", "m2p", "computer", 1, 0),
        Node("c", "3. Move the code", "acc", "computer", 2, 0),
        Node("v", "4. Validate", "reports, sandbox", "check", 3, 0),
        Node("x", "5. Cut over", "your decision", "result", 4, 0),
    ],
    [Edge("a", "d"), Edge("d", "c", "side by side", dashed=True), Edge("c", "v"), Edge("v", "x")],
)

RUNS = (
    [
        Node("you", "Your machine", "agent m2p | acc", "you", 0, 1),
        Node("agent", "Agent computer", "this code, an AI agent", "computer", 2, 1),
        Node("rest", "Everything else", "MongoDB, Postgres, branches, sandboxes, PRs", "db", 4, 1),
    ],
    [Edge("you", "agent", "computer, key, secrets"), Edge("agent", "rest", "makes and deletes")],
)


def node_shape(slide, n: Node):
    kind, outline = KIND[n.kind]
    x, y, w, h = n.box
    s = slide.shapes.add_shape(kind, Inches(x), Inches(y), Inches(w), Inches(h))
    s.fill.solid()
    s.fill.fore_color.rgb = rgb(DARK)
    s.line.color.rgb = rgb(outline)
    s.line.width = Pt(1.5)
    s.shadow.inherit = False
    tf = s.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = Inches(0.04)
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf.clear()
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    runs(p, n.name, 9.5 if n.kind != "check" else 9, bold=True)
    q = tf.add_paragraph()
    q.alignment = PP_ALIGN.CENTER
    runs(q, n.detail, 7.5, GREY)
    return s


def anchors(a: Node, b: Node) -> tuple[float, float, float, float]:
    """Where an edge leaves `a` and reaches `b`: their facing sides' middles."""
    ax, ay, aw, ah = a.box
    bx, by, bw, bh = b.box
    acx, acy, bcx, bcy = ax + aw / 2, ay + ah / 2, bx + bw / 2, by + bh / 2
    if a.row == b.row or abs(bcx - acx) > abs(bcy - acy) * 1.6:
        x1 = ax + aw if bcx > acx else ax
        x2 = bx if bcx > acx else bx + bw
        return x1, acy, x2, bcy
    y1 = ay + ah if bcy > acy else ay
    y2 = by if bcy > acy else by + bh
    return acx, y1, bcx, y2


def edge_shapes(slide, e: Edge, nodes: dict[str, Node]) -> list:
    x1, y1, x2, y2 = anchors(nodes[e.a], nodes[e.b])
    c = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
    c.line.color.rgb = rgb(GREY)
    c.line.width = Pt(1.25)
    ln = c.line._get_or_add_ln()
    if e.dashed:
        ln.append(
            etree.fromstring('<a:prstDash xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" val="dash"/>')
        )
    ln.append(etree.fromstring(f'<a:tailEnd xmlns:a="{A}" type="triangle" w="med" len="med"/>'))
    out = [c]
    width = 0.065 * len(e.label) + 0.12  # at 7 pt
    if e.label and (y1 != y2 or width < abs(x2 - x1) - 0.04):  # only where it fits: the captions say the rest
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2 - (0.17 if y1 == y2 else 0)
        out.append(textbox(slide, mx - width / 2, my - 0.11, width, 0.22, [e.label], 7, GREY, align=PP_ALIGN.CENTER,
                           anchor=MSO_ANCHOR.MIDDLE, fill=DARK))  # fmt: skip
    return out


def diagram(slide, spec) -> dict[int, list]:
    """The diagram's shapes, by the frame in which they appear (0: at once)."""
    nodes, edges = spec
    by_id = {n.id: n for n in nodes}
    frames: dict[int, list] = {}
    for e in edges:  # under the nodes
        frames.setdefault(e.frame, []).extend(edge_shapes(slide, e, by_id))
    for n in nodes:
        frames.setdefault(n.frame, []).append(node_shape(slide, n))
    return frames


# ---------------------------------------------------------------------- animations

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _effect(ids: list[int], spid: int, *, click: bool, enter: bool, grp: int) -> str:
    a, b, c = ids.pop(0), ids.pop(0), ids.pop(0)
    node = "clickEffect" if click else "withEffect"
    cls = "entr" if enter else "exit"
    tgt = f'<p:tgtEl><p:spTgt spid="{spid}"/></p:tgtEl>'
    vis = (f'<p:set><p:cBhvr><p:cTn id="{b}" dur="1" fill="hold"><p:stCondLst><p:cond delay="{0 if enter else 399}"/>'
           f'</p:stCondLst></p:cTn>{tgt}<p:attrNameLst><p:attrName>style.visibility</p:attrName></p:attrNameLst>'
           f'</p:cBhvr><p:to><p:strVal val="{"visible" if enter else "hidden"}"/></p:to></p:set>')  # fmt: skip
    fade = (f'<p:animEffect transition="{"in" if enter else "out"}" filter="fade"><p:cBhvr><p:cTn id="{c}" dur="400"/>'
            f"{tgt}</p:cBhvr></p:animEffect>")  # fmt: skip
    inner = vis + fade if enter else fade + vis
    return (f'<p:par><p:cTn id="{a}" presetID="10" presetClass="{cls}" presetSubtype="0" fill="hold" grpId="{grp}" '
            f'nodeType="{node}"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>{inner}</p:childTnLst>'
            "</p:cTn></p:par>")  # fmt: skip


def animate(slide, clicks: list[tuple[list, list]]) -> None:
    """On each click, the shapes of the first list fade in and those of the second fade out."""
    ids = list(range(3, 10_000))
    builds: set[tuple[int, int]] = set()
    pars = []
    for enter, leave in clicks:
        effects = []
        for i, s in enumerate(enter):
            effects.append(_effect(ids, s.shape_id, click=i == 0, enter=True, grp=0))
            if s.has_text_frame:  # as PowerPoint writes them: only for shapes with text
                builds.add((s.shape_id, 0))
        for s in leave:
            effects.append(_effect(ids, s.shape_id, click=False, enter=False, grp=1))
            if s.has_text_frame:
                builds.add((s.shape_id, 1))
        outer, inner = ids.pop(0), ids.pop(0)
        pars.append(f'<p:par><p:cTn id="{outer}" fill="hold"><p:stCondLst><p:cond delay="indefinite"/></p:stCondLst>'
                    f'<p:childTnLst><p:par><p:cTn id="{inner}" fill="hold"><p:stCondLst><p:cond delay="0"/>'
                    f"</p:stCondLst><p:childTnLst>{''.join(effects)}</p:childTnLst></p:cTn></p:par>"
                    "</p:childTnLst></p:cTn></p:par>")  # fmt: skip
    bld = "".join(f'<p:bldP spid="{s}" grpId="{g}" animBg="1"/>' for s, g in sorted(builds))
    xml = (f'<p:timing xmlns:p="{P}"><p:tnLst><p:par><p:cTn id="1" dur="indefinite" restart="never" nodeType="tmRoot">'
           '<p:childTnLst><p:seq concurrent="1" nextAc="seek"><p:cTn id="2" dur="indefinite" nodeType="mainSeq">'
           f"<p:childTnLst>{''.join(pars)}</p:childTnLst></p:cTn>"
           '<p:prevCondLst><p:cond evt="onPrev" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:prevCondLst>'
           '<p:nextCondLst><p:cond evt="onNext" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:nextCondLst>'
           f"</p:seq></p:childTnLst></p:cTn></p:par></p:tnLst><p:bldLst>{bld}</p:bldLst></p:timing>")  # fmt: skip
    sld = slide._element
    sld.insert(list(sld).index(sld.find(qn("p:clrMapOvr"))) + 1, etree.fromstring(xml))


# ---------------------------------------------------------------------- slides


def layout(prs, name: str):
    return next(lay for m in prs.slide_masters for lay in m.slide_layouts if lay.name == name)


def ph(slide, idx: int):
    return next(s for s in slide.placeholders if s.placeholder_format.idx == idx)


def drop(slide, idx: int) -> None:
    s = ph(slide, idx)
    s._element.getparent().remove(s._element)


def place(shape, top: float, height: float) -> None:
    """A placeholder lower on its slide: all four, or the ones not set fall back to 0."""
    shape.left, shape.top, shape.width, shape.height = Inches(0.6), Inches(top), Inches(8.81), Inches(height)


def notes(slide, text: str) -> None:
    if text:
        slide.notes_slide.notes_text_frame.text = text


def content(prs, eyebrow: str, title: str, footer: str = "From MongoDB to PostgreSQL, with AI agents on Nevia"):
    s = prs.slides.add_slide(layout(prs, CONTENT))
    ph(s, 3).text_frame.text = eyebrow
    ph(s, 0).text_frame.text = title
    ph(s, 2).text_frame.text = footer
    return s


def bullets_into(placeholder, items: list[str], size: float = 13) -> None:
    fill_text(placeholder.text_frame, items, size, space=9, bullet=True)
    for p in placeholder.text_frame.paragraphs:
        pPr = p._p.get_or_add_pPr()
        pPr.set("marL", "0")
        pPr.set("indent", "0")
        pPr.insert(0, etree.fromstring('<a:buNone xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"/>'))


def table_into(slide, rows: list[list[str]], y: float) -> None:
    n, m = len(rows), len(rows[0])
    t = slide.shapes.add_table(n, m, Inches(0.59), Inches(y), Inches(8.8), Inches(0.42 * n)).table
    t.columns[0].width = Inches(2.0)
    t.columns[1].width = Inches(6.8)
    tbl = t._tbl
    tbl.tblPr.set("firstRow", "1")
    tbl.tblPr.set("bandRow", "0")
    style = tbl.tblPr.find(qn("a:tableStyleId"))
    if style is not None:
        tbl.tblPr.remove(style)
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            cell = t.cell(r, c)
            cell.fill.solid()
            cell.fill.fore_color.rgb = rgb(DARK)
            cell.margin_left = cell.margin_right = Inches(0.08)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            tf = cell.text_frame
            tf.clear()
            runs(tf.paragraphs[0], value, 10 if r else 9, GREEN if r == 0 else WHITE, bold=r == 0 or c == 0)
            tcPr = cell._tc.get_or_add_tcPr()
            border = etree.SubElement(tcPr, qn("a:lnB"), w="9525")
            solid = etree.SubElement(border, qn("a:solidFill"))
            etree.SubElement(solid, qn("a:srgbClr"), val=LINE)


def build(template: Path) -> None:
    prs = Presentation(str(template))
    ids = prs.slides._sldIdLst
    for sid in list(ids):  # the template's samples go; its masters and layouts stay
        prs.part.drop_rel(sid.rId)
        ids.remove(sid)

    md = read(DOCS / "presentation.md")
    main = [s for s in md if not s.animation]
    frames = {k: [s for s in md if s.animation and s.animation[0] == k] for k in ("m2p", "acc")}

    # 1. The cover.
    c = main[0]
    s = prs.slides.add_slide(layout(prs, COVER))
    ph(s, 0).text_frame.text = c.title.upper()
    ph(s, 1).text_frame.text = c.subtitle
    ph(s, 2).text_frame.text = "NEVIA · TWO AI AGENTS · M2P AND ACC"
    notes(s, " ".join(c.bullets) + " " + c.notes)

    for i, sl in enumerate(main[1:], 1):
        s = content(prs, EYEBROWS[i], sl.title)
        body = ph(s, 1)
        if sl.table:  # Nevia's concepts
            drop(s, 1)
            table_into(s, sl.table, 1.35)
            textbox(s, 0.59, 4.7, 8.8, 0.3, [sl.after], 11, GREY)
        elif sl.title.startswith("The path"):
            diagram(s, PATH)
            place(body, 2.4, 2.6)
            bullets_into(body, sl.bullets, 12)
        elif sl.title.startswith("How it runs"):
            diagram(s, RUNS)
            for shape in s.shapes:  # this diagram on the grid's first row
                if not shape.is_placeholder:
                    shape.top -= Inches(1.0)
            place(body, 2.75, 2.3)
            bullets_into(body, sl.bullets, 12)
        else:
            bullets_into(body, sl.bullets)
        notes(s, sl.notes)
        for agent, spec in (("m2p", M2P), ("acc", ACC)):
            if sl.title.endswith(f": {agent}"):
                animated(prs, agent, spec, frames[agent])

    s = prs.slides.add_slide(layout(prs, CLOSING))
    ph(s, 0).text_frame.text = "THANK YOU"
    notes(s, "agent m2p setup && agent m2p run; agent acc setup && agent acc run migrate")
    prs.save(str(OUT))
    print(f"{OUT}: {len(prs.slides)} slides")  # noqa: T201 - a script


def animated(prs, agent: str, spec, frames: list[Slide]) -> None:
    """The agent's steps on one slide: on each click, the step's shapes and caption appear, the caption before
    goes."""
    s = content(prs, f"{agent.upper()} · STEP BY STEP", f"How {agent} works, step by step")
    drop(s, 1)
    shapes = diagram(s, spec)
    captions = []
    for n, f in enumerate(frames, 1):
        title, _, rest = f.caption.partition("** ")
        tb = textbox(s, 0.59, 4.6, 8.8, 0.62, [f"**{n}/{len(frames)} · {title.strip('*')}** {rest}"], 11)
        captions.append(tb)
    animate(s, [([*shapes.get(n, []), captions[n - 1]], [captions[n - 2]] if n > 1 else [])
                for n in range(1, len(frames) + 1)])  # fmt: skip
    steps = " ".join(f"{n}. {f.caption}" for n, f in enumerate(frames, 1)).replace("**", "")
    notes(s, f"Click {len(frames)} times: one step each. {steps}")


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else TEMPLATE)
