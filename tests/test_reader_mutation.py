"""Fixed-seed mutation tests: synthetic fixtures of xls (clear, RC4, XOR), xlsb, ppt (clear, encrypted), rtf, pdf (plain,
xref and object streams, R4 AES), the OOXML docx, pptx and xlsx packages, mobi (MOBI 6, KF8 combo, HUFF/CDIC), wpd
(WordPerfect 6 and 5), iWork (IWA Pages, Numbers and Keynote, '09 Pages and Numbers), djvu (a bundled TXTz book, a
TXTa page), Access (Jet 3, Jet 4), Publisher (2002+, 2000, 97/98) and Visio binary (v11, v5) are damaged at random and
read; every outcome must be a Doc, another parser's name for a compound file, or a ReadError (no other exception, no
hang). Five diagram seeds (a pptx slide with a glued and a loose connector, a vsdx page with a style-chain arrow, a ppt
slide with a solver, an hwpx connectLine, an hwp $col) are damaged the same way, and every Mermaid block a damaged
case writes must pass `diagram_fix.mermaid_ok`.

About 100 cases a seed, each from `random.Random(SEED + k)`, so a failure names the case and repeats. No damaged
case is ever written to disk: compound files and zips are built in memory and read through the formats' in-memory
entries (`xls.parse_compound`, `ppt.parse_compound`, `xlsb.parse_package`, `ooxml.parse_package`, `rtf.parse_bytes`, `pdf.parse_bytes`,
`mobi.parse_bytes`, `wpd.parse_bytes`, `iwork.parse_bytes`, `djvu.parse_bytes`, `jet.parse_bytes`, `pub.parse_compound`,
`vsd.parse_compound`, `vsdx.parse` and `hwpx.parse` on bytes, hwp's reader on a compound file's bytes), and rendering
never writes an image. Only the clean xlsb, ppt, docx and pptx seeds and the clean pptx, ppt, hwpx and hwp diagram
seeds touch a temporary directory, once each (the xlsx, mobi, wpd, iWork, djvu, Access, Publisher, Visio and vsdx
diagram seeds are built in memory). An OOXML case damages one or two of its seed's XML members. An IWA case damages the archive bytes of one or two
`.iwa` members after Snappy is undone (every other case only inside one object's payload, the framing kept), then
stores them as type-1 chunks; a '09 case damages `index.xml`. A Publisher case damages its Contents or Quill stream
or both, a Visio case its VisioDocument stream (stored and LZ77-coded pages), before `cfb_bytes`. The RTF seed
holds none of the control words of a shape's property groups, and the test builds them with `cat` so no .pyc holds
them either.

Run: python -I -X utf8 tests/test_reader_mutation.py
"""
import dataclasses
import io
import random
import re
import struct
import sys
import tempfile
import time
import zipfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import metafile, pict, svg_write, vector, wpg  # noqa: E402
from reader import markdown  # noqa: E402
from reader.containers import flate, pbwire, snappy  # noqa: E402
from reader.containers.cfb import Compound  # noqa: E402
from reader.containers.officecrypt import PPT_DEFAULT  # noqa: E402
from reader.formats import (djvu, hwp, hwpx, iwork, iwork_iwa, jet, mobi, ooxml, pdf, ppt, pub,  # noqa: E402
                            pub_contents, rtf, vsd, vsdx, wpd, xls, xlsb)
from reader.limits import ReadError  # noqa: E402
from reader.model import Code, Context, Doc  # noqa: E402
from diagram_fix import mermaid_ok  # noqa: E402
from pdf_fix import (EncryptSpec, Name, Ref, acroform, font_type0, image_xobject, link_annot,  # noqa: E402
                     outline_items, rule_ops, simple_pdf, text_annot, text_ops, widget)
from reader_fix import (XLSX_SML, blip_png, cfb_bytes, encrypt_xls, hwp_para, make_hwp, make_ppt,  # noqa: E402
                        make_xlsb, make_xlsx, make_zip, oa_fbse, oa_solver, pub_2k_file, pub_cells, pub_escher,
                        pub_file, pub_sp, pub_table, xlsx_cell, xlsx_row)
import djvu_fix as djf  # noqa: E402
import metafile_fix as mf  # noqa: E402
import iwork_fix as iwf  # noqa: E402
import jet_fix as jf  # noqa: E402
import test_reader_docm as dm  # noqa: E402  (the seed builders and fixtures of the owning test files)
import test_reader_hwp as hw  # noqa: E402
import test_reader_hwpx as hx  # noqa: E402
import test_reader_iwork as iw  # noqa: E402
import test_reader_mobi as mb  # noqa: E402
import test_reader_omml as om  # noqa: E402
import test_reader_ppt_shapes as ps  # noqa: E402
import test_reader_pub as pb  # noqa: E402
import test_reader_slides as sl  # noqa: E402
import test_reader_smartart as sa  # noqa: E402
import test_reader_vsd as vs  # noqa: E402
import test_reader_vsdx as vx  # noqa: E402
import test_reader_wpd as wp  # noqa: E402
import test_reader_xlsb as xb  # noqa: E402

SEED = 20261114
CASES = 100
SLOW = 5.0  # seconds one case may take: a case past it is a hang

SHEETS = [{"name": "First", "cells": {(0, 0): "alpha", (0, 1): 42.5, (1, 0): "한글 text", (2, 2): 7}},
          {"name": "Second", "cells": {(0, 0): "beta " * 30, (1, 1): "gamma"}}]
SLIDES = [{"title": "Quarterly plan", "body": ["first point", "second point"]},
          {"title": "Second slide", "body": ["more text"]}]
PNG = b"\x89PNG\r\n\x1a\n" + b"P" * 24


def cat(*parts):
    """The parts joined at call time: a concatenation of constants is folded by the compiler and lands in the .pyc."""
    return b"".join(parts)


# ---- mutations
def plan(rng, size):
    """A list of edits for data of `size` bytes, chosen only from rng and size so that the same plan can damage two
    streams of one length (a clear stream and its encryption) at the same places."""
    edits = []
    for _ in range(rng.choice((1, 1, 2, 3, 6))):
        op = rng.choice(("flip", "flip", "set", "run", "cut", "drop", "dup", "word"))
        at = rng.randrange(max(size, 1))
        if op == "flip":
            edits.append((op, at, rng.randrange(1, 256)))
        elif op == "set":
            edits.append((op, at, rng.choice((0x00, 0xFF, 0x7F, 0x80))))
        elif op == "run":
            edits.append((op, at, rng.choice((0x00, 0xFF)), rng.randrange(2, 40)))
        elif op == "cut":
            edits.append((op, at))
        elif op == "drop":
            edits.append((op, at, rng.randrange(1, 64)))
        elif op == "dup":
            edits.append((op, at, rng.randrange(1, 64)))
        else:
            edits.append((op, at, rng.choice((0xFFFF, 0x7FFF, 0x8000, 0x0001, 0))))
    return edits


def apply(edits, data):
    out = bytearray(data)
    for e in edits:
        op, at = e[0], min(e[1], max(len(out) - 1, 0))
        if not out:
            break
        if op == "flip":
            out[at] ^= e[2]
        elif op == "set":
            out[at] = e[2]
        elif op == "run":
            out[at:at + e[3]] = bytes([e[2]]) * min(e[3], len(out) - at)
        elif op == "cut":
            del out[at:]
        elif op == "drop":
            del out[at:at + e[2]]
        elif op == "dup":
            out[at:at] = out[at:at + e[2]]
        else:
            out[at:at + 2] = e[2].to_bytes(2, "little")
    return bytes(out)


# ---- the harness
class MemAssets:
    """Stands in for images.Assets while a case is rendered: no image is ever written out."""

    def __init__(self, dir):
        self.lines, self.figures, self.figures_left, self.warnings = {}, 0, 0, set()

    def add(self, data, alt="", codepage=""):
        return "assets/mem.png", True


def render(doc, skipped):
    real, markdown.Assets = markdown.Assets, MemAssets
    try:
        return markdown.render(doc, Path("assets-never-written"), False, skipped)
    finally:
        markdown.Assets = real


def outcome(read, label, routed=True):
    """"doc", "routed" (another parser's compound file; only when `routed`) or the ReadError kind; any other
    exception fails with the case's label, a case past SLOW is a hang."""
    t0 = time.monotonic()
    try:
        got = read()
        kind = "doc" if isinstance(got, Doc) else "routed" if routed and isinstance(got, str) else None
        assert kind, f"{label}: returned {type(got).__name__}, not a Doc"
    except ReadError as e:
        kind = e.kind
        assert kind in ("damaged", "encrypted", "unsupported", "too_large", "drm"), f"{label}: ReadError kind {kind!r}"
    except AssertionError:
        raise
    except BaseException as e:
        raise AssertionError(f"{label}: {type(e).__name__}: {e}") from e
    took = time.monotonic() - t0
    assert took < SLOW, f"{label}: {took:.1f}s"
    return kind


def run_cases(label, make_case, cases=CASES, seed=SEED, routed=True):
    """Run `cases` cases; make_case(rng, k) returns a callable that reads the damaged input from memory. Returns the
    outcome counts. Nothing damaged is written to disk. `routed=False`: a returned str fails the case."""
    counts = {}
    for k in range(cases):
        rng = random.Random(seed + k)
        kind = outcome(make_case(rng, k), f"{label} case {k} (seed {seed + k})", routed)
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def read_doc(parse, data):
    ctx = Context()
    doc = parse(data, ctx)
    if isinstance(doc, Doc):
        render(doc, ctx.skipped)
    return doc


# ---- xls
def encrypt_stream(kind):
    from reader_fix import biff_workbook
    if kind is None:
        return biff_workbook(SHEETS)
    return encrypt_xls(SHEETS, "VelvetSweatshop", kind)


def xls_streams():
    return {"clear": encrypt_stream(None), "rc4": encrypt_stream("rc4"), "xor": encrypt_stream("xor")}


def xls_case(streams, kind):
    """Cases that damage `kind`'s Workbook stream. A case's plan is drawn from its seed and the clear stream's length
    alone, so the three kinds are damaged at the same places (the encrypted streams are a FILEPASS record longer)."""
    def make(rng, k):
        edits = plan(rng, len(streams["clear"]))
        blob = cfb_bytes({"Workbook": apply(edits, streams[kind])})[0]
        return lambda: read_doc(xls.parse_compound, blob)
    return make


def test_xls_mutations():
    streams = xls_streams()
    for kind in ("clear", "rc4", "xor"):
        counts = run_cases(f"xls {kind}", xls_case(streams, kind))
        assert sum(counts.values()) == CASES, counts
    # The same plans on the clear stream and on its RC4 and XOR encryptions (record headers stay clear):
    # the shares of framing refusals ("damaged") agree. The streams differ only in what a damaged body decrypts to, so
    # a body edit rarely changes whether the records still frame; the margin is 0.15, about 3 standard errors of a
    # difference of two shares of 100 near one half (sqrt(2 * .25 / 100) = .07), not a tuned value.
    shares = {}
    for kind in ("clear", "rc4", "xor"):
        counts = run_cases(f"xls {kind} paired", xls_case(streams, kind), seed=SEED + 1000)
        shares[kind] = counts.get("damaged", 0) / CASES
    for kind in ("rc4", "xor"):
        assert abs(shares["clear"] - shares[kind]) <= 0.15, shares


# ---- xlsb (the clean seed is built once in a temporary directory and read back; no damaged case is)
def xlsb_members():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "seed.xlsb"
        make_xlsb(path, SHEETS)
        with zipfile.ZipFile(path) as z:
            return {i.filename: z.read(i.filename) for i in z.infolist()}


def test_xlsb_mutations():
    members = xlsb_members()
    binary = sorted(n for n in members if n.endswith(".bin"))
    assert binary, members.keys()

    def make(rng, k):
        parts = dict(members)
        for name in rng.sample(binary, rng.choice((1, 1, 2))):
            parts[name] = apply(plan(rng, len(parts[name])), parts[name])
        blob = make_zip(None, parts, stored=())
        return lambda: read_doc(xlsb.parse_package, blob)

    counts = run_cases("xlsb", make)
    assert sum(counts.values()) == CASES, counts


# ---- OOXML: docx, pptx and xlsx packages; one or two XML members damaged, the zip rebuilt and read in memory
def zip_members(src) -> dict:
    """{name: bytes} of a clean zip, a path or its bytes."""
    with zipfile.ZipFile(io.BytesIO(src) if isinstance(src, bytes) else src) as z:
        return {i.filename: z.read(i.filename) for i in z.infolist()}


def xlsx_seed() -> dict:
    """Two sheets, shared strings (one rich), a legacy comment, an external hyperlink, a merge, a hidden row, and a
    chart and a picture on the second sheet; built in memory."""
    first = (xlsx_row(1, xlsx_cell("A1", 0, t="s"), xlsx_cell("B1", "42.5", s=1), xlsx_cell("C1", f="SUM(B1)"))
             + xlsx_row(2, xlsx_cell("A2", 1, t="s"), attrs='hidden="1"')
             + xlsx_row(3, xlsx_cell("A3", "true", t="b"), xlsx_cell("B3", inline="<t>inline</t>")))
    after = ('<mergeCells count="1"><mergeCell ref="A3:B3"/></mergeCells>'
             '<hyperlinks><hyperlink ref="A1" r:id="rL"/></hyperlinks>')
    comments = (f'<comments xmlns="{XLSX_SML}"><authors><author>Ann</author></authors><commentList>'
                '<comment ref="A1" authorId="0"><text><r><t>check this</t></r></text></comment>'
                "</commentList></comments>")
    sheets = [{"name": "First", "rows": first, "after": after,
               "rels": [("rL", "hyperlink", "https://example.test/a", True),
                        ("rC", "comments", "../comments1.xml", False)]},
              {"name": "Second", "rows": xlsx_row(1, xlsx_cell("A1", "한글", t="str")),
               "rels": [("rD", "drawing", "../drawings/drawing1.xml", False)]}]
    extra = {"xl/comments1.xml": comments,
             "xl/drawings/drawing1.xml": xb.drawing_xml(xb.anchor(0, xb.chart_frame("rId1")),
                                                        xb.anchor(2, xb.pic("rId2"))),
             "xl/drawings/_rels/drawing1.xml.rels": xb.rels_xml(("rId1", "chart", "../charts/chart1.xml"),
                                                                ("rId2", "image", "../media/image1.png")),
             "xl/charts/chart1.xml": xb.BAR, "xl/media/image1.png": PNG}
    return zip_members(make_xlsx(None, sheets, shared=["<t>alpha</t>", "<r><t>rich</t></r><r><t> run</t></r>"],
                                 numfmts={164: "0.00"}, xfs=[0, 164], extra_parts=extra))


def docx_seed() -> dict:
    """A docm package: paragraphs, an OMML paragraph and a SmartArt data part (`docm` writes it, clean, to a
    temporary directory)."""
    math = (f'<w:p><w:r><w:t>Area </w:t></w:r><m:oMath xmlns:m="{om.M}"><m:sSup>{om.e(om.r("a"))}'
            f'{om.e(om.r("2"), "sup")}</m:sSup></m:oMath></w:p>')
    art = ("<w:p><w:r><w:drawing><wp:inline><a:graphic><a:graphicData>" + sa.REF
           + "</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>")
    data = sa.data_xml([sa.DOC, ("A", None, "first"), ("B", None, "second"), ("A1", None, "inner")],
                       [("0", "A", 0, None), ("0", "B", 1, None), ("A", "A1", 0, None)])
    return zip_members(dm.docm(dm.P("Intro text") + math + art + dm.P("After"), {"word/diagrams/data1.xml": data},
                               sa.RELS_D))


def pptx_seed() -> dict:
    """A deck: a title, an `a14:m` equation, notes and a legacy comment with its author (`deck` writes it, clean, to
    a temporary directory)."""
    p = f'<a:p {om.PNS}><a:r><a:t>E = </a:t></a:r><a14:m><m:oMath>{om.sup("m", "2")}</m:oMath></a14:m></a:p>'
    members = {"ppt/comments/comment1.xml": sl._legacy((0, "first comment")),
               "ppt/commentAuthors.xml": sl._authors("Ann"),
               sl.PRELS: sl._pres_rels(1, [("rA", "commentAuthors", "commentAuthors.xml")])}
    path = sl.deck([sl.sp("Plan", "title") + sl.sp(paras=p)], notes={1: sl.sp("speaker", "body")},
                   srels={1: [("rC", "comments", "../comments/comment1.xml")]}, extra=members)
    return zip_members(path)


def ooxml_case(members):
    xml = sorted(n for n in members if n.endswith((".xml", ".rels")))

    def make(rng, k):
        parts = dict(members)
        for name in rng.sample(xml, rng.choice((1, 1, 2))):
            parts[name] = apply(plan(rng, len(parts[name])), parts[name])
        blob = make_zip(None, parts, stored=())
        return lambda: read_doc(ooxml.parse_package, blob)
    return make


def ooxml_mutations(label, members, probe):
    ctx = Context()
    out = render(ooxml.parse_package(make_zip(None, members, stored=()), ctx), ctx.skipped)
    assert probe in out and not ctx.skipped, (label, out, ctx.skipped)  # the clean seed reads whole
    counts = run_cases(label, ooxml_case(members))
    assert sum(counts.values()) == CASES, counts


def test_xlsx_mutations():
    ooxml_mutations("xlsx", xlsx_seed(), "Chart: bar")


def test_docx_mutations():
    ooxml_mutations("docx", docx_seed(), "$a^{2}$")


def test_pptx_mutations():
    ooxml_mutations("pptx", pptx_seed(), "$m^{2}$")


# ---- ppt
def ppt_streams(**kw):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "seed.ppt"
        make_ppt(path, SLIDES, notes={1: "speaker words"}, pictures=[blip_png(PNG)], **kw)
        with Compound(path) as cf:
            return {n: cf.read(n) for n in ("PowerPoint Document", "Current User", "Pictures")}


def ppt_case(streams):
    names = ("PowerPoint Document", "PowerPoint Document", "Pictures", "Current User")

    def make(rng, k):
        parts = dict(streams)
        for name in rng.sample(names, rng.choice((1, 1, 2))):
            parts[name] = apply(plan(rng, len(parts[name])), parts[name])
        blob = cfb_bytes(parts)[0]
        return lambda: read_doc(ppt.parse_compound, blob)
    return make


def test_ppt_mutations():
    for label, kw in (("clear", {}), ("encrypted", {"encrypt": PPT_DEFAULT})):
        counts = run_cases(f"ppt {label}", ppt_case(ppt_streams(**kw)))
        assert sum(counts.values()) == CASES, counts


# ---- rtf: the seed holds no shape control words, and nothing is written out
RTF_SEED = (
    b"{\\rtf1\\ansi\\ansicpg1252\\deff0{\\fonttbl{\\f0\\fcharset0 Arial;}{\\f1\\fcharset128 MS Mincho;}}"
    b"{\\colortbl;\\red0\\green0\\blue0;}{\\stylesheet{\\s1 heading 1;}{\\s2 heading 2;}{\\s3\\sbasedon2 sub;}}"
    b"{\\info{\\title Seed title}{\\author Someone}}{\\header\\pard Top line\\par}{\\footer\\pard Bottom\\par}"
    b"\\pard\\s1 Heading one\\par\\pard\\plain\\f0\\fs24 Plain {\\b bold \\i italic} \\'e9\\'e8 \\u8212? "
    b"\\u-10179?\\u-8704? {\\v hidden words}\\line next\\par"
    b"\\pard\\outlinelevel2 Outline two\\par\\pard\\ls1\\ilvl1 {\\listtext 1.\\tab}list item\\par"
    b"\\trowd\\trleft-108\\cellx1000\\cellx2000\\pard\\intbl a\\cell b\\cell\\row"
    b"\\trowd\\clmgf\\cellx1000\\clmrg\\cellx2000\\pard\\intbl c\\cell \\cell\\row\\pard after table\\par"
    b"\\pard note{\\footnote\\pard\\plain note text}{\\footnote\\ftnalt end}\\par"
    b"{\\field{\\*\\fldinst HYPERLINK \"http://example.test/a\"}{\\fldrslt link text}}\\par"
    b"{\\*\\shppict{\\pict\\pngblip\\picw1\\pich1 89504e470d0a1a0a0000000000000000000000000000000000000000}}"
    b"{\\object{\\*\\objdata 0102}{\\result object result}}"
    b"{\\pict\\dibitmap0\\bin16 0123456789abcdef}\\pard last\\par}"
)


def test_rtf_seed_has_no_shape_words():
    for word in (cat(b"\\s", b"p"), cat(b"\\s", b"n"), cat(b"\\s", b"v"), cat(b"\\sh", b"pinst"),
                 cat(b"pFra", b"gments")):
        # the control words: a letter or a delimiter must follow, `\sbasedon` and `\s1` are not them
        assert word not in RTF_SEED.replace(b"\\sbasedon", b"").replace(b"\\s1", b"").replace(b"\\s2", b"") \
            .replace(b"\\s3", b""), word


def test_rtf_mutations():
    doc = rtf.parse_bytes(RTF_SEED, Context())
    assert "Heading one" in render(doc, {})

    def make(rng, k):
        data = apply(plan(rng, len(RTF_SEED)), RTF_SEED)
        if rng.random() < 0.3:  # also brace and backslash noise: the bytes that steer the lexer
            at = rng.randrange(len(data))
            data = data[:at] + rng.choice((b"{", b"}", b"\\", b"\\bin9999 ", b"\\u", b"\\'", b"\\*")) + data[at:]

        return lambda: read_doc(rtf.parse_bytes, data)

    counts = run_cases("rtf", make)
    assert sum(counts.values()) == CASES, counts


# ---- pdf: built and damaged in memory, read with the native engine
def pdf_page1():
    grid = b" ".join([rule_ops(72, 500, 272, 500), rule_ops(72, 480, 272, 480), rule_ops(72, 460, 272, 460),
                      rule_ops(72, 500, 72, 460), rule_ops(172, 500, 172, 460), rule_ops(272, 500, 272, 460)])
    cells = text_ops("cell a", 76, 485, 10) + b" " + text_ops("cell b", 176, 485, 10)
    cid = b"BT /F2 12 Tf 72 600 Td <00010002> Tj ET"
    return (text_ops("Seed heading text", 72, 720) + b" " + text_ops("Second line of body", 72, 700) + b" " + cid + b" "
            + grid + b" " + cells + b" q 100 0 0 50 72 380 cm /Im1 Do Q")


def pdf_seed(**kw):
    """Two pages: Helvetica and CID text (ToUnicode), a ruled table, a gray Flate image, a link, a comment, a filled
    field (object 201, a widget on page 1) and an outline."""
    objects = {30: image_xobject(8, 8), 40: link_annot([72, 715, 172, 735], "https://example.test/a"),
               41: text_annot([300, 700, 320, 720], "a comment", author="Ann")}
    objects.update(font_type0(tounicode={1: "한", 2: "글"}))
    objects.update(acroform([widget([72, 300, 172, 320], Ref(4), T="name", FT=Name("Tx"), V="filled value")]))
    objects.update(outline_items([(1, "Part one", 0), (2, "Detail", 0), (1, "Part two", 1)]))
    return simple_pdf([pdf_page1(), text_ops("Page two text", 72, 700)],
                      resources={"Font": {"F1": Ref(3), "F2": Ref(100)}, "XObject": {"Im1": Ref(30)}},
                      page_extra=[{"Annots": [Ref(40), Ref(41), Ref(201)]}, {}],
                      catalog={"AcroForm": Ref(200), "Outlines": Ref(300)}, extra_objects=objects, **kw)


def pdf_seeds():
    """Three seeds, each built in memory: plain (xref table), an xref stream with object streams, and R4 AES."""
    return [pdf_seed(), pdf_seed(xref="stream", objstm=True), pdf_seed(encrypt=EncryptSpec(4))]


def read_pdf(data):
    return read_doc(lambda b, ctx: pdf.parse_bytes(b, ctx, engine="native"), data)


def test_pdf_mutations():
    seeds = pdf_seeds()
    for n, seed in enumerate(seeds):
        ctx = Context()
        out = render(pdf.parse_bytes(seed, ctx, engine="native"), ctx.skipped)
        assert "Seed heading text" in out and "Page two text" in out, (n, out)  # the clean seed reads

        def make(rng, k, seed=seed):
            data = apply(plan(rng, len(seed)), seed)
            return lambda: read_pdf(data)

        counts = run_cases(f"pdf seed {n}", make)
        assert sum(counts.values()) == CASES, counts
    # a non-Standard handler is "drm": the R4 seed with /Standard damaged to /Stbndard
    assert seeds[2].count(b"/Standard") == 1, seeds[2].count(b"/Standard")
    data = seeds[2].replace(b"/Standard", b"/Stbndard")
    assert outcome(lambda: read_pdf(data), "pdf non-standard handler") == "drm"


# ---- mobi: three books built in memory, each damaged whole and read from bytes
def mobi_seeds() -> dict:
    """A MOBI 6 book (PalmDOC) with an image and a footnote, a KF8 combo with an NCX, and a HUFF/CDIC book."""
    text = mb._fn_book(b"<a filepos=0000000000>1</a>", tail=b'<p><img recindex="00001"></p>')
    chunks = -(-len(text) // 4096)
    mobi6 = mb._mobi(text, compression=2, pack=mb._palmdoc_literal, first_image=1 + chunks, extra=[mb.PNG],
                     exth={503: b"Seed book"})
    files, _ = mb._two_files()
    ncx_at = len(mb._kf8_recs(files))
    cncx, rows = b"", []
    for label, fid, off, depth in (("Beta", 1, 3, 0), ("Gamma", 2, 3, 1)):
        rows.append((f"{len(rows):04d}", {1: [0], 3: [len(cncx)], 4: [depth], 6: [fid, off]}))
        cncx += mb._vwi(len(label)) + label.encode()
    ncx = mb._indx_records(mb.NCX6_TAGX, rows, cncx=cncx + bytes(4))
    combo = mb._combo(mb.OLD, mb._kf8_recs(files, ncx=ncx_at) + ncx)
    huff, cdics = mb._huff_tables([b"<html><body>", b"<p>he", b"llo</p>", b"</body></html>"])
    rec0 = mb._rec0(compression=17480, text_length=40, text_records=1, huff_record=2, huff_count=2)
    huffcdic = mb._pdb([rec0, bytes([0, 1, 2, 3]), huff] + cdics)
    return {"mobi6": (mobi6, "The note text"), "kf8 combo": (combo, "gamma"), "huffcdic": (huffcdic, "hello")}


# The fewest Doc outcomes a seed's CASES cases may give: about half the rate measured at 100 cases (and at least 1),
# so a regression that refuses every damaged case fails while the draw of 100 cases does not.
DOC_FLOOR = {"mobi mobi6": 25, "mobi kf8 combo": 25, "mobi huffcdic": 10, "wpd wp6": 35, "wpd wp5": 40,
             "iwork pages": CASES // 4, "iwork numbers": CASES // 4, "iwork key": CASES // 4,
             "iwork pages09": 25, "iwork numbers09": 25,
             # measured at SEED: 42, 28; 85, 82; 61, 63, 45; 40, 39
             "djvu book": 20, "djvu page": 14, "jet jet3": 40, "jet jet4": 40, "pub 2002": 30, "pub 2000": 30,
             "pub 97": 22, "vsd v11": 20, "vsd v5": 19}


def check_floor(label, counts):
    assert sum(counts.values()) == CASES, (label, counts)
    assert counts.get("doc", 0) >= DOC_FLOOR[label], (label, counts, DOC_FLOOR[label])


def test_mobi_mutations():
    for label, (seed, probe) in mobi_seeds().items():
        ctx = Context()
        out = render(mobi.parse_bytes(seed, ctx), ctx.skipped)
        assert probe in out and not ctx.skipped, (label, out, ctx.skipped)  # the clean seed reads whole

        def make(rng, k, seed=seed):
            data = apply(plan(rng, len(seed)), seed)
            return lambda: read_doc(mobi.parse_bytes, data)

        check_floor(f"mobi {label}", run_cases(f"mobi {label}", make, routed=False))


# ---- wpd: a WP6 and a WP5 document built in memory, damaged whole and read from bytes (no LibreOffice switch)
def wpd_seeds() -> dict:
    """WP6: a footnote, a comment, an undo span, a graphics box (WPG picture) and a table. WP5: a header, notes, bold,
    a comment and a table."""
    comment = struct.pack("<HHB", 1, 2, 0)  # the comment's one prefix id: text packet 2
    packets = [(0x08, wp._text(b"Note\x80", b"text")), (0x08, wp._text(b"A\x80remark")), (0x1B, comment),
               (0x6F, wp._WPG), (0x40, struct.pack("<HH", 1, 4), 1), (0x41, wp._style(3))]
    body = (b"Intro" + wp._note(1) + b"\x80said" + wp._grp(0xD4, 0x1D, [3]) + b"\xcc"
            + b"Keep\x80" + wp._undo(0) + b"Gone" + wp._undo(1) + b"\x80Also\xcc"
            + b"See" + wp._grp(0xDF, 0, [6, 5]) + b"this\xcc"
            + wp._grp(0xD4, 0x2A) + b"\xc5a\xc6b\xc5c\xc6d\xbfafter")
    wp6 = wp._wp6(body, packets)
    cell = lambda span=1, rows=1: wp._var5(0xDC, 0, bytes((0, 0, span, rows)) + b"\x00" * 7)  # noqa: E731
    body5 = (wp._hf5(0, b"Running\x0dhead") + b"Body " + wp._fixed5(0xC3, b"\x0c") + b"bold"
             + wp._fixed5(0xC4, b"\x0c") + wp._fn5(b"Foot\x0dnote") + b" text\x0aNext"
             + wp._var5(0xD9, 2, b"\x00\x00\x01" + b"A\x0dremark") + b"\x0a"
             + wp._var5(0xD2, 0x0B, b"\x00" * 8) + wp._var5(0xDC, 1) + cell(rows=2) + b"A" + cell() + b"B"
             + wp._var5(0xDC, 1) + cell(0x81) + b"A2" + cell() + b"C" + wp._var5(0xDC, 2) + b"After"
             + wp._fn5(b"End", endnote=True) + b"Last")
    return {"wp6": (wp6, "Note text"), "wp5": (wp._wp5(body5), "Foot note")}


def test_wpd_mutations():
    for label, (seed, probe) in wpd_seeds().items():
        ctx = Context()
        out = render(wpd.parse_bytes(seed, ctx), ctx.skipped)
        assert probe in out and not ctx.skipped, (label, out, ctx.skipped)  # the clean seed reads whole

        def make(rng, k, seed=seed):
            data = apply(plan(rng, len(seed)), seed)
            return lambda: read_doc(wpd.parse_bytes, data)

        check_floor(f"wpd {label}", run_cases(f"wpd {label}", make, routed=False))


# ---- iWork: IWA members damaged after Snappy (re-chunked as stored chunks); '09 index.xml damaged; zips in memory
def iwa_pages() -> bytes:
    text = "Heading\nLook here link B\ufffc end\nKeep gone stay\nPic\ufffc after"
    at = text.index
    note, pic = at("\ufffc"), text.rindex("\ufffc")
    archives = iwf.pages_doc(10, headers=[130]) + [
        iwf.archive(10, 2001, iwf.storage(
            text, para_styles=[(0, 20), (at("Look"), None)], links=[(at("link"), 80), (at("link") + 4, None)],
            footnotes=[(note, 90)], comments=[(at("here"), 95), (at("here") + 4, None)],
            deletions=[(at("gone"), 100), (at("gone") + 5, None)], attachments=[(pic, 110)],
            sections=[(0, iwf.SECTION)])),
        iwf.style(20, "Heading 1"), iwf.link(80, "https://example.test/a"), iwf.footnote(90, 91),
        iwf.archive(91, 2001, iwf.storage("The note.")), iwf.highlight(95, 96),
        iwf.comment(96, "Fix this", author=97), iwf.author(97, "Ann"), iwf.change(100, 2),
        iwf.attachment(110, 111), iwf.image(111, 7), iwf.archive(130, 2001, iwf.storage("Head"))]
    return iwf.iwork_zip({"Index/Document.iwa": iwf.iwa_member(*archives),
                          "Index/Metadata.iwa": iwf.metadata_iwa([(7, "pic.png")]), "Data/pic.png": PNG})


def iwa_numbers() -> bytes:
    def text(key):
        return iwf.cell_v5(3, string_id=key)
    tiles = {0: iwf.tile({0: [text(1), iwf.cell_v5(2, value=42.5), iwf.cell_v5(6, value=1.0)],
                          3: [iwf.cell_v5(5, value=86400.0), None, text(2)]}),
             1: iwf.tile({2: [None, text(3), iwf.cell_v5(10, value=7, format_id=1)]})}
    t = iwf.table("T", 8, 3, tiles=tiles, strings=[(1, "alpha"), (2, "beta"), (3, "past tile")], tile_size=4,
                  merges=[(0, 0, 1, 2)], formats=[(1, iwf.fmt(10, decimals=2, currency="EUR"))])
    archives = iwf.numbers_doc([("S", [600, 500])]) + t + iwf.chart(1, ["r"], ["A"], [[2]], "C")
    return iwf.iwork_zip({"Index/Document.iwa": iwf.iwa_member(*archives)})


def iwa_keynote() -> bytes:
    slides = [{"title": "T", "body": "Body text", "notes": "speaker words", "drawables": [900], "z_order": [900]},
              {"title": "U", "body": "Second", "skipped": True, "template": "Template title"}]
    archives = iwf.keynote_doc(slides) + [iwf.shape(900, 901), iwf.archive(901, 2001, iwf.storage("Drawn box"))]
    return iwf.iwork_zip({"Index/Document.iwa": iwf.iwa_member(*archives)})


def pages09_seed() -> bytes:
    notes = '<sf:p><sf:span><sf:footnote-mark sf:mark="1"/></sf:span> The note.<sf:footnotebr/></sf:p>'
    styles = iw.BOLD_STYLE + '<sf:paragraphstyle sf:ident="t" sf:name="Title"/>'
    body = (iw._p("Doc Title", "t") + iw._p('Body text<sf:footnote sf:autonumber="1" sf:index="0"/> more.')
            + iw._p('See <sf:link href="https://example.test/x"><sf:span sf:style="b">bold link</sf:span></sf:link>'
                    '<sf:lnbr/>end'))
    return iwf.pages09(body, header=iw._p("Running head"), footnotes=notes, styles=styles)


def numbers09_seed() -> bytes:
    rows = [[iw._t("Name"), iw._n("4650"), iw._n("3.25")],
            ['<sf:d sf:cell-date="0"/>', '<sf:t sf:col-span="2"><sf:ct sfa:s="Wide"/></sf:t>'],
            ['<sf:f><sf:fo sf:fs="=SUM(B1)"/><sf:r><sf:rn sf:v="7"/></sf:r></sf:f>', '<sf:b sf:v="1"/>', "<sf:s/>"]]
    sheets = iw._ws("A", iw._table09("Ta", rows, numcols=3)) + iw._ws("B", iw._table09("Tb", [[iw._t("beta")]]))
    return iwf.numbers09(sheets)


def iwork_seeds() -> dict:
    """label: (zip bytes, app, probe)."""
    return {"pages": (iwa_pages(), "pages", "The note."), "numbers": (iwa_numbers(), "numbers", "past tile"),
            "key": (iwa_keynote(), "key", "Drawn box"), "pages09": (pages09_seed(), "pages", "The note."),
            "numbers09": (numbers09_seed(), "numbers", "beta")}


def rechunk(rng, raw: bytes) -> bytes:
    """`raw` as one to three stored (type 1) IWA chunks, cut at places drawn from rng."""
    cuts = sorted(rng.randrange(len(raw) + 1) for _ in range(rng.choice((0, 0, 1, 2))))
    bounds = [0, *cuts, len(raw)]
    return b"".join(iwf.chunk(raw[a:b], type=1) for a, b in zip(bounds, bounds[1:]))


def payload_spans(stream: bytes) -> list[tuple[int, int]]:
    """(start, end) of every message payload in an unpacked .iwa stream, found by walking its framing ([varint n]
    [ArchiveInfo] then the payloads its MessageInfo lengths give) with the pbwire helpers `iwork_iwa._archives`
    reads it with."""
    spans, pos = [], 0
    while pos < len(stream):
        n, pos = pbwire.varint(stream, pos)
        info = pbwire.fields(stream[pos:pos + n])
        pos += n
        for mi in info.get(2, []):
            length = pbwire.one(pbwire.fields(mi), 3, 0)
            spans.append((pos, pos + length))
            pos += length
    return spans


KEEP_LENGTH = ("flip", "set", "run", "word")


def damage_payload(rng, stream: bytes, spans) -> bytes:
    """`stream` with bytes inside one payload changed by length-preserving edits only, so every ArchiveInfo,
    MessageInfo and length varint stays intact and the object walks read the damaged fields."""
    a, b = rng.choice([s for s in spans if s[1] > s[0]])
    body = stream[a:b]
    edits = [e for e in plan(rng, len(body)) if e[0] in KEEP_LENGTH] or [("flip", rng.randrange(len(body)), 1)]
    body = apply(edits, body)[:b - a]  # a word edit on the last byte would add one
    return stream[:a] + body + stream[b:]


HOSTILE_VALUES = (b"", b"-1", b"0", b"99999999", b"4294967296", b"1e309", b"nan", b"x", b"-0.5")


def damage_values(rng, xml: bytes) -> bytes:
    """`xml` with one to four attribute values swapped for a hostile value or another value of the same file: the
    file stays well formed, so the '09 walk reads the counts, spans, dates and ids it is given. Namespace
    declarations and version attributes are left alone."""
    spans = [x.span(2) for x in re.finditer(rb'([\w:.-]+)="([^"<&]*)"', xml)
             if not x[1].startswith(b"xmlns") and not x[1].endswith(b"version")]
    values = [xml[a:b] for a, b in spans]
    out = xml
    for a, b in sorted(rng.sample(spans, min(len(spans), rng.choice((1, 2, 4)))), reverse=True):
        new = rng.choice(HOSTILE_VALUES) if rng.random() < 0.6 else rng.choice(values)
        out = out[:a] + new + out[b:]
    return out


def iwork_case(seed: bytes, app: str):
    """Cases for one seed. An IWA seed damages one or two of its .iwa members' archive bytes (Snappy undone), and
    re-chunks them stored: even cases damage the whole stream, framing included; odd cases change bytes inside one
    object's payload only (`damage_payload`), keeping the framing. A '09 seed damages its index.xml, bytes in one case of two (the parser refuses most) and
    attribute values in the other. The zip is rebuilt in memory, deflating as the seed did."""
    members = zip_members(seed)
    iwa = sorted(n for n in members if n.endswith(".iwa"))
    if iwa:
        raw = {n: snappy.iwa(members[n], flate.Budget(10**8)) for n in iwa}
        spans = {n: payload_spans(raw[n]) for n in iwa}
        for n in iwa:  # the walk finds one payload for every object the reader loads (no seed merges messages)
            assert len(iwork_iwa._archives(raw[n], {})[0]) == len(spans[n]), n
        methods = {}
    else:
        raw = {"index.xml": members["index.xml"]}
        methods = {n: 8 for n in members}
    names = sorted(raw)

    def make(rng, k):
        parts = dict(members)
        if iwa and k % 2:
            name = rng.choice(names)
            parts[name] = rechunk(rng, damage_payload(rng, raw[name], spans[name]))
            blob = iwf.iwork_zip(parts, methods)
            return lambda: read_doc(lambda b, ctx: iwork.parse_bytes(b, ctx, app), blob)
        for name in rng.sample(names, min(len(names), rng.choice((1, 1, 2)))):
            if iwa:
                parts[name] = rechunk(rng, apply(plan(rng, len(raw[name])), raw[name]))
            elif k % 2:
                parts[name] = damage_values(rng, raw[name])
            else:
                parts[name] = apply(plan(rng, len(raw[name])), raw[name])
        blob = iwf.iwork_zip(parts, methods)
        return lambda: read_doc(lambda b, ctx: iwork.parse_bytes(b, ctx, app), blob)
    return make


def test_iwork_mutations():
    for label, (seed, app, probe) in iwork_seeds().items():
        ctx = Context()
        out = render(iwork.parse_bytes(seed, ctx, app), ctx.skipped)
        assert probe in out and not ctx.skipped, (label, out, ctx.skipped)  # the clean seed reads whole
        check_floor(f"iwork {label}", run_cases(f"iwork {label}", iwork_case(seed, app), routed=False))


def check_clean(label, read, probe, expect=None):
    """The clean seed reads whole: `probe` in its Markdown, and nothing counted at parse time but `expect` (a zero
    count, which renders nothing, is no count)."""
    ctx = Context()
    doc = read(ctx)
    counted = dict(+ctx.skipped)
    out = render(doc, ctx.skipped)
    assert probe in out and counted == (expect or {}), (label, out, counted)


# ---- djvu: a bundled book and a single page built in memory, damaged whole and read from bytes
def djvu_seeds() -> dict:
    """A bundled two-page TXTz book with an ANTz (a link, a comment, a title) on its first page and a NAVM, and a
    single-page TXTa file."""
    ant = '(maparea "http://a.example/" "A" (rect 0 0 1 1)) (maparea "" "a note" (rect 0 0 1 1)) (metadata (title "T"))'
    book = djf.djvm([djf.page("First page\x1fsecond paragraph", z=True, ant=ant, antz=True),
                     djf.page("Second page words", z=True)], titles=["Intro", None],
                    navm=djf.navm([("Part", "#1", [("Chapter", "#2", [])])]))
    return {"book": (book, "Second page words"), "page": (djf.iff(djf.page("Plain page\nline two")), "Plain page")}


def test_djvu_mutations():
    for label, (seed, probe) in djvu_seeds().items():
        check_clean(f"djvu {label}", lambda ctx, seed=seed: djvu.parse_bytes(seed, ctx), probe)

        def make(rng, k, seed=seed):
            data = apply(plan(rng, len(seed)), seed)
            return lambda: read_doc(djvu.parse_bytes, data)

        check_floor(f"djvu {label}", run_cases(f"djvu {label}", make, routed=False))


# ---- Access: a Jet3 and a Jet4 file built in memory, damaged whole and read from bytes
def jet_seeds() -> dict:
    """Jet3: one table whose memo is an LVAL chain. Jet4: two tables (one with an attachment column), a select query
    and a relationship between them."""
    memo = " ".join(f"word{i}" for i in range(900))  # past one 2 KiB LVAL page: a chain
    jet3 = jf.jet_db(0, [jf.Tbl("Notes", [jf.Col("ID", jf.LONG), jf.Col("Name", jf.TEXT), jf.Col("Body", jf.MEMO)],
                                [[1, "first", memo], [2, "second", "short"]], pk="ID")])
    files = jf.Col("Files", jf.COMPLEX, attachments=True)
    a = jf.Tbl("A", [jf.Col("ID", jf.LONG), jf.Col("Name", jf.TEXT), files],
               [[1, "alpha", [("a.png", PNG)]], [2, "beta", None]], pk="ID")
    b = jf.Tbl("B", [jf.Col("ID", jf.LONG), jf.Col("AID", jf.LONG)], [[1, 1], [2, 2]])
    query = jf.Query("Q", [(1, None, 1, None, None), (5, None, None, "A", None), (6, "Name", None, None, None)])
    jet4 = jf.jet_db(1, [a, b], queries=[query], relationships=[("AB", "B", "AID", "A", "ID")])
    return {"jet3": (jet3, "word899"), "jet4": (jet4, "FROM A")}


def test_jet_mutations():
    for label, (seed, probe) in jet_seeds().items():
        check_clean(f"jet {label}", lambda ctx, seed=seed: jet.parse_bytes(seed, ctx), probe)

        def make(rng, k, seed=seed):
            data = apply(plan(rng, len(seed)), seed)
            return lambda: read_doc(jet.parse_bytes, data)

        check_floor(f"jet {label}", run_cases(f"jet {label}", make, routed=False))


# ---- Publisher: the Contents and Quill streams damaged, the compound file rebuilt and read in memory
PUB_STREAMS = ("Contents", "Quill/QuillSub/CONTENTS", "Escher/EscherStm")


def pub_seeds() -> dict:
    """2002+: two pages (text, a 2x2 table with a merged row, a picture), a master page with a footer, a link. 2000:
    two pages and a master, Quill text with bold. 97/98: 8-bit text in two boxes (no Quill)."""
    page = dict
    pages = [page(seq=0x25, shapes=[0x34], master="Master A"), page(seq=0x21, shapes=[0x30, 0x31], applied=0x25),
             page(seq=0x22, shapes=[0x32, 0x33], applied=0x25)]
    shapes = [dict(seq=0x30, text=1), dict(seq=0x31, text=2, type=pub_contents.TABLE, extra=pub_table(2, 2, 0x50)),
              dict(seq=0x50, type=pub_contents.CELLS, extra=pub_cells(pb.MERGED)), dict(seq=0x32),
              dict(seq=0x33, text=3), dict(seq=0x34, text=4)]
    stories = [(1, "Hello\rWorld"), (2, pb.TABLE_TEXT), (3, "Second page"), (4, "Footer")]
    escher = pub_escher(pub_sp(0x30, (0, 10, 50, 20)), pub_sp(0x32, (0, 30, 50, 60), pib=1),
                        store=[oa_fbse(blip_png(PNG))])
    v2002 = pub_file(pages, shapes, stories, escher=escher, bold=[(0, 5)], tcd={1: pb.TABLE_BOUNDS},
                     links=["https://a.example/"])
    v2000 = pub_2k_file([pb.PG, 0x10C, 0x109], [pb.text2k(0x30, 1, box=(0, 0, 9, 9)), pb.text2k(0x31, 2, parent=0x10C),
                                                pb.table2k(0x32, 3), pb.text2k(0x33, 4, parent=0x109)],
                        [(1, "Hello\rWorld"), (2, "Second"), (3, "Cell"), (4, "Footer")], bold=[(0, 5)])
    v97 = pb.file97(b"Hello\r\nWorld\x0bnext\x0cBox two")
    # a 2000 table's structure is not read: its story is, counted `table` once
    return {"2002": (v2002, "Footer", None), "2000": (v2000, "Cell", {"table": 1}), "97": (v97, "Box two", None)}


def pub_case(blob):
    """Cases that damage one or both of the Contents and Quill streams (Escher kept), rebuilt with cfb_bytes."""
    with Compound(blob) as cf:
        streams = {n: cf.read(n) for n in PUB_STREAMS if cf.exists(n)}
    names = [n for n in PUB_STREAMS[:2] if n in streams]

    def make(rng, k):
        parts = dict(streams)
        for name in rng.sample(names, min(len(names), rng.choice((1, 1, 2)))):
            parts[name] = apply(plan(rng, len(parts[name])), parts[name])
        damaged = cfb_bytes(parts)[0]
        return lambda: read_doc(pub.parse_compound, damaged)
    return make


def test_pub_mutations():
    for label, (seed, probe, expect) in pub_seeds().items():
        check_clean(f"pub {label}", lambda ctx, seed=seed: pub.parse_compound(seed, ctx), probe, expect)
        check_floor(f"pub {label}", run_cases(f"pub {label}", pub_case(seed)))


# ---- Visio binary: the VisioDocument stream damaged (stored and LZ77-coded pages), wrapped and read in memory
def vsd_seeds() -> dict:
    """v11: a page with a group, a shape taking its stencil master's text and a glued connector, and a second page
    LZ77-coded. v5: a page with a stencil master, a hidden shape and a cp1251 font run."""
    shapes = [{"id": 1, "type": 0x47, "text": "group", "pin": (5.0, 10.0, 1.0, 2.0)},
              {"id": 2, "parent": 1, "text": "child", "pin": (0.5, 1.0)},
              {"id": 3, "master": (0, 2), "pin": (0.0, 8.0)},
              {"id": 4, "text": "End", "pin": (0.0, 5.0)},
              {"id": 5, "text": "flows", "pin": (0.0, 7.0), "misc": vs.misc(11, ends=(1, 4))}]
    coded = {"name": "Coded", "compressed": True, "shapes": [{"id": 1, "text": "packed", "pin": (1.0, 1.0)}]}
    v11 = vs.drawing([{"name": "Main", "shapes": shapes}, coded],
                     stencils=[[{"id": 1, "text": "first"}, {"id": 2, "text": "Master"}]])
    v5_shapes = [{"id": 1, "text": "low", "pin": (0.0, 1.0)}, {"id": 3, "master": (0, 4), "pin": (0.0, 5.0)},
                 {"id": 5, "text": "quiet", "pin": (0.0, 3.0), "misc": vs.misc(5, hide=True)},
                 {"id": 6, "raw": "Мир".encode("cp1251"), "runs": [(3, 7)], "pin": (0.0, 2.0)}]
    v5 = vs.drawing([{"name": "Front", "shapes": v5_shapes}], version=5, stencils=[[{"id": 4, "text": "Master"}]],
                    extra=[vs.fonts([(7, 204, b"Arial")], version=5)])
    return {"v11": (v11, "packed"), "v5": (v5, "Мир")}


def test_vsd_mutations():
    for label, (seed, probe) in vsd_seeds().items():
        blob = cfb_bytes({"VisioDocument": seed})[0]
        check_clean(f"vsd {label}", lambda ctx, blob=blob: vsd.parse_compound(blob, ctx), probe)

        def make(rng, k, seed=seed):
            blob = cfb_bytes({"VisioDocument": apply(plan(rng, len(seed)), seed)})[0]
            return lambda: read_doc(vsd.parse_compound, blob)

        check_floor(f"vsd {label}", run_cases(f"vsd {label}", make))


# ---- diagrams: five seeds whose connectors become a Mermaid graph, damaged in memory and read from bytes
def mermaid_texts(node, out) -> list:
    """The text of every Mermaid Code block in a model, in order (the parsers bound its nesting)."""
    if isinstance(node, Code):
        if node.lang == "mermaid":
            out.append(node.text)
    elif isinstance(node, (list, tuple)):
        for x in node:
            mermaid_texts(x, out)
    elif isinstance(node, dict):
        for x in node.values():
            mermaid_texts(x, out)
    elif dataclasses.is_dataclass(node) and not isinstance(node, type):
        for f in dataclasses.fields(node):
            mermaid_texts(getattr(node, f.name), out)
    return out


def read_diagram(parse, data, label, drawn):
    """read_doc that also checks every Mermaid block the case writes against the grammar and counts, in drawn[0],
    the cases that write one."""
    ctx = Context()
    doc = parse(data, ctx)
    if isinstance(doc, Doc):
        texts = mermaid_texts(doc, [])
        for t in texts:
            assert mermaid_ok(t), (label, t)
        drawn[0] += bool(texts)
        render(doc, ctx.skipped)
    return doc


def pptx_diagram_seed() -> dict:
    """A slide: Start glued to Middle by one connector, and a loose one from Middle's box to End's (`deck` writes it,
    clean, to a temporary directory)."""
    s = (sl.sp("Start", id=2, off=(0, 0), ext=(100, 100)) + sl.sp("Middle", id=3, off=(0, 1000), ext=(100, 100))
         + sl.sp("End", id=6, off=(0, 2000), ext=(100, 100))
         + sl.cxn(4, 2, 3, off=(50, 100), ext=(0, 900), tail="triangle")
         + sl.cxn(5, off=(50, 1100), ext=(0, 900), tail="triangle"))
    return zip_members(sl.deck([s]))


def vsdx_diagram_seed() -> dict:
    """A page: A and B glued by a connector whose arrows come through a two-style LineStyle chain; built in memory."""
    shapes = vx.shape(1, "A", (0, 9)) + vx.shape(2, "B", (0, 1)) + vx.shape(3, None, (), "", 'LineStyle="5"')
    chain = {"5": ("3", vx.cell(EndArrow=0)), "3": (None, vx.cell(BeginArrow=1, EndArrow=1))}
    return zip_members(vx.vsdx({"P": vx.page_xml(shapes, vx.glued(3, 1, 2))},
                               extra={"visio/document.xml": vx.styles_xml(chain)}, mem=True))


def ppt_diagram_seed():
    """(streams, (start, end)): a slide with Start and End glued by a solver's rule, its connector between them (built
    once, clean, in a temporary directory), and the span of the PowerPoint Document stream from the slide's
    PPDrawing to the end of its solver."""
    slides, kw = ps.flow()
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "seed.ppt"
        make_ppt(path, slides, **kw)
        with Compound(path) as cf:
            streams = {n: cf.read(n) for n in ("PowerPoint Document", "Current User")}
    stream, solver = streams["PowerPoint Document"], oa_solver((2, 3, 4))
    end = stream.find(solver) + len(solver)
    start = stream.rfind(struct.pack("<HH", 0xF, 0x040C), 0, end)  # the PPDrawing container's header
    assert end > len(solver) and start >= 0, (start, end)
    return streams, (start, end)


def hwpx_diagram_seed() -> dict:
    """A section: Alpha and Beta glued by a connectLine (`hwpx_doc` writes it, clean, to a temporary directory)."""
    return zip_members(hx.hwpx_doc(hx._connectline_sec()))


def hwp_diagram_seed():
    """(streams, raw section, (start, end)): Alpha and Beta glued by a $col (built once, clean, in a temporary
    directory), the section's records before deflate, and the span of its drawing objects' paragraph."""
    raw = hw._connect_sec()
    start, end = len(hwp_para("before")), len(raw) - len(hwp_para("after"))
    with tempfile.TemporaryDirectory() as d:
        path = make_hwp(Path(d) / "seed.hwp", [raw])
        with Compound(path) as cf:
            streams = {n: cf.read(n) for n in ("FileHeader", "DocInfo", "BodyText/Section0")}
    return streams, raw, (start, end)


def read_hwp(blob, ctx):
    """hwp.parse's compound-file branch, from bytes (its sniff reads only the FileHeader, never damaged here)."""
    with Compound(blob) as cf:
        return hwp._Reader(cf, ctx).run()


def raw_deflate(data: bytes) -> bytes:
    c = zlib.compressobj(wbits=-15)
    return c.compress(data) + c.flush()


def xml_diagram_case(members, targets, parse, label, drawn, stored=()):
    """Cases that damage one or two of the `targets` XML members: bytes in even cases (the parser refuses most),
    attribute values in odd ones (`damage_values`: ids, offsets, extents, styles), the zip rebuilt in memory."""
    def make(rng, k):
        parts = dict(members)
        for name in rng.sample(targets, min(len(targets), rng.choice((1, 1, 2)))):
            parts[name] = damage_values(rng, parts[name]) if k % 2 else apply(plan(rng, len(parts[name])), parts[name])
        blob = make_zip(None, parts, stored=stored)
        return lambda: read_diagram(parse, blob, f"{label} case {k}", drawn)
    return make


def ppt_diagram_case(streams, span, label, drawn):
    """Cases that damage the PowerPoint Document stream: anywhere in even cases, with length-preserving edits inside
    the slide's drawing and solver in odd ones (the record framing around them kept)."""
    name = "PowerPoint Document"

    def make(rng, k):
        parts = dict(streams)
        data = parts[name]
        parts[name] = damage_payload(rng, data, [span]) if k % 2 else apply(plan(rng, len(data)), data)
        blob = cfb_bytes(parts)[0]
        return lambda: read_diagram(ppt.parse_compound, blob, f"{label} case {k}", drawn)
    return make


def hwp_diagram_case(streams, raw, span, label, drawn):
    """Cases that damage the section's records before deflate: anywhere in even cases, with length-preserving edits
    inside the drawing objects' paragraph in odd ones (the record headers around it kept)."""
    def make(rng, k):
        parts = dict(streams)
        data = damage_payload(rng, raw, [span]) if k % 2 else apply(plan(rng, len(raw)), raw)
        parts["BodyText/Section0"] = raw_deflate(data)
        blob = cfb_bytes(parts)[0]
        return lambda: read_diagram(read_hwp, blob, f"{label} case {k}", drawn)
    return make


def diagram_seeds() -> dict:
    """label: (read(blob, ctx), clean blob, case maker(label, drawn), its clean Mermaid text, routed)."""
    pptx_m, vsdx_m, hwpx_m = pptx_diagram_seed(), vsdx_diagram_seed(), hwpx_diagram_seed()
    (ppt_s, ppt_span), (hwp_s, hwp_raw, hwp_span) = ppt_diagram_seed(), hwp_diagram_seed()
    slide, page = "ppt/slides/slide1.xml", "visio/pages/page1.xml"
    return {
        "pptx": (ooxml.parse_package, make_zip(None, pptx_m, stored=()),
                 lambda label, drawn: xml_diagram_case(pptx_m, [slide], ooxml.parse_package, label, drawn),
                 'flowchart TD\n  n1["Start"]\n  n2["Middle"]\n  n3["End"]\n  n1 --> n2\n'
                 '  %% inferred from position\n  n2 --> n3', False),
        "vsdx": (vsdx.parse, make_zip(None, vsdx_m, stored=()),
                 lambda label, drawn: xml_diagram_case(vsdx_m, [page, "visio/document.xml"], vsdx.parse, label,
                                                       drawn),
                 'flowchart TD\n  n1["A"]\n  n2["B"]\n  n2 --> n1', False),
        "ppt": (ppt.parse_compound, cfb_bytes(ppt_s)[0],
                lambda label, drawn: ppt_diagram_case(ppt_s, ppt_span, label, drawn),
                'flowchart TD\n  n1["Start"]\n  n2["End"]\n  n1 --> n2', True),
        "hwpx": (hwpx.parse, make_zip(None, hwpx_m),
                 lambda label, drawn: xml_diagram_case(hwpx_m, ["Contents/section0.xml"], hwpx.parse, label, drawn,
                                                       stored=("mimetype",)),
                 'flowchart LR\n  n1["Alpha"]\n  n2["Beta"]\n  n1 --> n2', False),
        "hwp": (read_hwp, cfb_bytes(hwp_s)[0],
                lambda label, drawn: hwp_diagram_case(hwp_s, hwp_raw, hwp_span, label, drawn),
                'flowchart LR\n  n1["Alpha"]\n  n2["Beta"]\n  n1 --> n2', False),
    }


# (Doc outcomes, cases that still draw a diagram): the fewest a seed's CASES cases may give, about half the rates
# measured at SEED, so a regression that refuses every damaged case, or that stops drawing damaged diagrams, fails
# while the draw of 100 cases does not. Measured at SEED: pptx 52, 48; vsdx 63, 42; ppt 73, 53; hwpx 53, 53;
# hwp 100, 57.
DIAGRAM_FLOOR = {"pptx": (26, 24), "vsdx": (31, 21), "ppt": (36, 26), "hwpx": (26, 26), "hwp": (50, 28)}


def test_diagram_mutations():
    for label, (read, clean, cases, want, routed) in diagram_seeds().items():
        ctx = Context()
        doc = read(clean, ctx)
        texts = mermaid_texts(doc, [])
        assert texts == [want] and mermaid_ok(want) and not +ctx.skipped, (label, texts, ctx.skipped)  # it draws
        render(doc, ctx.skipped)
        drawn = [0]
        counts = run_cases(f"diagram {label}", cases(f"diagram {label}", drawn), routed=routed)
        docs, draws = DIAGRAM_FLOOR[label]
        assert sum(counts.values()) == CASES, (label, counts)
        assert counts.get("doc", 0) >= docs and drawn[0] >= draws, (label, counts, drawn[0], DIAGRAM_FLOOR[label])

# ---- figures: a WMF, an EMF+ dual file, a PICT 2 and a WPG 1 damaged in memory, parsed, read for text and drawn as SVG
def _pict_pixmap(w, h, rowbytes, table):
    out = struct.pack(">H", 0x8000 | rowbytes) + struct.pack(">hhhh", 0, 0, h, w)
    out += struct.pack(">HHIiiHHHHIII", 0, 0, 0, 72 << 16, 72 << 16, 0, 8, 1, 8, 0, 0, 0)
    out += struct.pack(">IHH", 0, 0, len(table) - 1)
    for i, (r, g, b) in enumerate(table):
        out += struct.pack(">HHHH", i, r << 8, g << 8, b << 8)
    return out


def figure_seeds() -> dict:
    """label: (parse(bytes), clean bytes, text the clean seed reads)."""
    wmf_seed = mf.wmf([mf.wmf_font(-100), mf.wmf_select(0), mf.wmf_brush(0xFF0000), mf.wmf_select(1),
                       mf.wmf_rect(10, 10, 600, 400), mf.wmf_text(100, 200, b"Seed text")])
    plus = mf.emf([mf.emfplus([mf.plus_header(), mf.plus_font(0, 12, "Arial"), mf.plus_string(0, 96, 96, "Plus text"),
                               mf.plus_fill_rect(0, 0, 50, 50)]),
                   mf.emf_font(-100), mf.emf_textw(100, 100, "Dual text")])
    packed = (_pict_pixmap(10, 2, 10, [(0, 0, 0), (255, 0, 0)]) + struct.pack(">hhhh", 0, 0, 2, 10)
              + struct.pack(">hhhh", 5, 6, 7, 16) + struct.pack(">H", 0)
              + bytes([2, 256 - 9, 1]) + bytes([2, 256 - 9, 0]))
    pict_seed = mf.pict([mf.pict_longtext(30, 20, b"Pict text"), mf.pict_op(0x98, packed)])
    wpg_seed = mf.wpg1([mf.wpg1_text_attr(avg=100, size=200, align=0, color=4), mf.wpg1_text(100, 1000, b"Wpg text"),
                        mf.wpg1_rect(10, 10, 300, 200)])
    return {"wmf": (metafile.parse, wmf_seed, "Seed text"), "emfplus": (metafile.parse, plus, "Plus text"),
            "pict": (pict.parse, pict_seed, "Pict text"), "wpg1": (wpg.parse, wpg_seed, "Wpg text")}


def read_figure(parse, data):
    """What a figure goes through after a picture's bytes are read: parse, text lines, SVG."""
    d = parse(data)
    assert isinstance(d, vector.Drawing), type(d)
    vector.text_lines(d)
    svg_write.write(d, d.size_px())
    return d


# The fewest parse successes of a seed's CASES cases: about half the rates measured at SEED (measured below), so a
# regression that refuses every damaged case fails while the draw of 100 cases does not.
# Measured at SEED: wmf 74, emfplus 84, pict 47, wpg1 57.
FIGURE_FLOOR = {"wmf": 37, "emfplus": 42, "pict": 23, "wpg1": 28}


def test_figure_mutations():
    for label, (parse, seed, probe) in figure_seeds().items():
        d = read_figure(parse, seed)
        assert any(probe in t for t in vector.text_lines(d)[0]) and not d.damaged, (label, vector.text_lines(d))

        def make(rng, k, seed=seed, parse=parse):
            data = apply(plan(rng, len(seed)), seed)
            return lambda: read_figure(parse, data)

        def to_doc(f):  # outcome() wants a Doc; a Drawing counts as one
            return lambda: (f(), Doc())[1]

        counts = run_cases(f"figure {label}", lambda rng, k: to_doc(make(rng, k)), routed=False)
        assert sum(counts.values()) == CASES, (label, counts)
        assert counts.get("doc", 0) >= FIGURE_FLOOR[label], (label, counts, FIGURE_FLOOR[label])


def main():
    t0 = time.monotonic()
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            t1 = time.monotonic()
            fn()
            print(f"{name}: {time.monotonic() - t1:.2f}s")
    print(f"mutation tests total: {time.monotonic() - t0:.2f}s")
    print("pass")


if __name__ == "__main__":
    main()
