"""Word 97-2003 (.doc) embedded objects: the EMBED field whose separator names an ObjectPool storage, equations
(Equation Native, MTEF v3 and v5, to TeX), charts and other objects with their previews, hidden objects, hostile
input.

Run: python -I -X utf8 tests/test_reader_doc_objects.py
"""
import struct
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader.formats.mtef import DEPTH, to_tex  # noqa: E402  (ModuleNotFoundError before mtef.py exists)
from reader import markdown  # noqa: E402
from reader.containers.cfb import CfbError, Compound  # noqa: E402
from reader.formats import doc, doc_objects, doc_pictures  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context, Equation, Hidden, Image, Math, Para, Text  # noqa: E402
from reader_fix import (blip_bitmap, blip_metafile, blip_png, comp_obj, doc_stories, eq_native, make_doc,  # noqa: E402
                        mtef_v3, mtef_v5, object_pool, picf_inline)

SPEC, PIC, OBJ, OLE, VANISH, DELETED = 0x0855, 0x6A03, 0x680E, 0x080A, 0x083C, 0x0800
AT = 16  # where the tests put the preview's PICF in the Data stream
EMBED = "\x13 EMBED Equation.3 \x14\x01\x15"  # Word's EMBED field: instruction, separator, the preview \x01, end

PNG = b"\x89PNG\r\n\x1a\n" + bytes(24)
EMF = struct.pack("<II", 1, 88) + bytes(32) + b" EMF" + bytes(44)
PNG_PREVIEW = picf_inline(blip_png(PNG))
EMF_PREVIEW = picf_inline(blip_metafile(0xF01A, 0x3D4, EMF))
WORKBOOK = struct.pack("<HHHH", 0x0809, 4, 0x0600, 0x0020) + struct.pack("<HH", 0x000A, 0)  # BOF, EOF


def tmp():
    return Path(tempfile.mkdtemp())


def u16(n):
    return struct.pack("<H", n)


def sp(op, value=1):
    """One sprm, its operand sized by op >> 13."""
    return u16(op) + value.to_bytes((1, 1, 2, 4, 2, 2, 0, 3)[op >> 13], "little")


# ---- MTEF records: v5 (a type byte, then an options byte for LINE to EMBELL) and v3 (options in the tag's high nibble)
def c5(s, face=0x83, opts=0):
    """v5 CHARs: tag 2, options, typeface + 128 (0x83 a variable), the MTCode."""
    return b"".join(bytes((2, opts, face)) + u16(ord(x)) for x in s)


def l5(body=b""):
    return b"\x01\x00" + body + b"\x00"


NULL5 = b"\x01\x01"  # a v5 LINE with mtefOPT_LINE_NULL: an empty slot


def t5(sel, *slots, var=0):
    return bytes((3, 0, sel, var, 0)) + b"".join(slots) + b"\x00"


def c3(s, face=0x83, opts=0):
    """v3 CHARs: the tag (CHAR, options in the high nibble), typeface + 128, the MTCode."""
    return b"".join(bytes((2 | opts << 4, face)) + u16(ord(x)) for x in s)


def l3(body=b""):
    return b"\x01" + body + b"\x00"


NULL3 = b"\x11"  # a v3 LINE with xfNULL


def t3(sel, *slots, var=0):
    return bytes((3, sel, var, 0)) + b"".join(slots) + b"\x00"


FRAC = mtef_v5(l5(t5(11, l5(c5("a")), l5(c5("b")))))  # a over b
SUBSUP = mtef_v5(l5(c5("x") + t5(29, l5(c5("1")), l5(c5("2")))))  # x_1^2


def tex_of(mtef):
    """to_tex and what it reports lost."""
    lost = set()
    return to_tex(mtef, lost), lost


def embed_doc(objects, text=EMBED + "\r", preview=None, oid=1, props=b"", sep=None, one=None, **kw):
    """`text` in one Unicode piece: each field separator (\\x14) has fSpec, fOle2 and PicLocation `oid` (or the
    sprms `sep`), each \\x01 fSpec and PicLocation AT (or the sprms `one`), `props` on both; the Data stream holds the
    PICF `preview` at AT, and the ObjectPool `objects` ({id: {stream name: bytes}})."""
    sep = sp(SPEC) + sp(OLE) + sp(PIC, oid) if sep is None else sep
    one = sp(SPEC) + sp(PIC, AT) if one is None else one
    spans = [(i, i + 1, (sep if ch == "\x14" else one) + props) for i, ch in enumerate(text) if ch in "\x14\x01"]
    kw.setdefault("chpx", [s for s in spans if s[2]])
    streams = object_pool(objects)
    if preview is not None:
        streams["Data"] = bytes(AT) + preview
    return make_doc(tmp() / "a.doc", [(text, False)], streams=streams, **kw)


def parse(path):
    ctx = Context()
    return doc.parse(Path(path), ctx), ctx


def md(path, hidden=False, out=None):
    d, ctx = parse(path)
    return markdown.render(d, (out or tmp()) / "assets", hidden, ctx.skipped)


# ---- the brief's tests
def test_equation_fraction():
    assert to_tex(FRAC) == r"\frac{a}{b}"
    p = embed_doc({1: {"Equation Native": eq_native(FRAC)}}, preview=PNG_PREVIEW)
    d, ctx = parse(p)
    assert d.blocks == [Equation(r"\frac{a}{b}")] and not ctx.skipped, (d.blocks, ctx.skipped)
    assert md(p) == "$$\\frac{a}{b}$$\n", md(p)


def test_equation_sub_sup():
    assert to_tex(SUBSUP) == "x_{1}^{2}"
    # v3's one SCRIPT template: variation 2 has both slots, 0 the superscript, 1 the subscript
    assert to_tex(mtef_v3(l3(c3("x") + t3(15, l3(c3("1")), l3(c3("2")), var=2)))) == "x_{1}^{2}"
    assert to_tex(mtef_v3(l3(c3("x") + t3(15, NULL3, l3(c3("2")))))) == "x^{2}"
    assert to_tex(mtef_v3(l3(c3("x") + t3(15, l3(c3("i")), NULL3, var=1)))) == "x_{i}"
    # sibling scripts on one base are one script (x_{i}_{j} is not TeX), v5's tmSUB and tmSUP too
    assert to_tex(mtef_v3(l3(c3("x") + t3(15, l3(c3("i")), NULL3, var=1) + t3(15, NULL3, l3(c3("2")))))) == "x_{i}^{2}"
    assert to_tex(mtef_v5(l5(c5("x") + t5(27, l5(c5("i")), NULL5) + t5(28, NULL5, l5(c5("2")))))) == "x_{i}^{2}"
    assert to_tex(mtef_v5(l5(c5("x") + t5(28, l5(c5("2")))))) == "x^{2}"  # tmSUP with its one slot
    p = embed_doc({1: {"Equation Native": eq_native(SUBSUP)}})
    assert md(p) == "$$x_{1}^{2}$$\n", md(p)


def test_equation_bad_bytes():
    """Garbage in Equation Native: the preview stays and the object is counted."""
    p = embed_doc({1: {"Equation Native": eq_native(b"garbage, not MTEF")}}, preview=PNG_PREVIEW)
    assert md(p) == "(could not read: 1 embedded object)\n\n![](assets/img-001.png)\n", md(p)
    d, ctx = parse(p)
    assert d.blocks == [Image(PNG, codepage="cp1252")] and ctx.skipped == {"object": 1}, (d.blocks, ctx.skipped)
    # cbHdr past the stream, a stream too short for one, an empty one, MTEF v2 and v4, a header with no record
    for native in (eq_native(FRAC)[:20] + b"\xff\xff", b"\x1c", b"", eq_native(b"\x02\x01\x01\x01\x00\x00"),
                   eq_native(b"\x04" + FRAC[1:]), eq_native(mtef_v5(b""))):
        assert md(embed_doc({1: {"Equation Native": native}}, preview=PNG_PREVIEW)) == \
            "(could not read: 1 embedded object)\n\n![](assets/img-001.png)\n", native


def test_chart_object_counted():
    for streams in ({"Workbook": WORKBOOK}, {"Book": WORKBOOK},
                    {"\x01CompObj": comp_obj(b"MSGraph.Chart.8", b"Microsoft Graph Chart"), "\x01Ole": bytes(20)}):
        p = embed_doc({1: streams}, preview=PNG_PREVIEW)
        assert md(p) == "(could not read: 1 chart)\n\n![](assets/img-001.png)\n", (streams, md(p))
    # a chart without a preview is counted the same, once
    d, ctx = parse(embed_doc({1: {"Workbook": WORKBOOK}}))
    assert d.blocks == [] and ctx.skipped == {"chart": 1}, (d.blocks, ctx.skipped)


def test_equation_depth_cap():
    """40 nested fractions: the lists past DEPTH are dropped, the TeX up to them kept and the drop counted."""
    inner = c5("x")
    for _ in range(40):
        inner = t5(11, l5(inner), l5(c5("1")))
    tex, lost = tex_of(mtef_v5(l5(inner)))
    # the top list, the LINE and each fraction's template and slot lines nest: the 16th fraction's slots are past it
    want = r"\frac{}{}"
    for _ in range(15):
        want = r"\frac{" + want + "}{1}"
    assert DEPTH == 32 and tex == want and lost == {"depth"}, (tex, lost)
    p = embed_doc({1: {"Equation Native": eq_native(mtef_v5(l5(inner)))}})
    d, ctx = parse(p)
    assert d.blocks == [Equation(want)] and ctx.skipped == {"depth": 1}, (d.blocks, ctx.skipped)


# ---- where an equation goes
def test_inline_equation_in_sentence():
    p = embed_doc({1: {"Equation Native": eq_native(FRAC)}}, text="a " + EMBED + " b\r", preview=PNG_PREVIEW)
    d, ctx = parse(p)
    assert d.blocks == [Para([Text("a "), Math(r"\frac{a}{b}"), Text(" b")])] and not ctx.skipped, d.blocks
    assert md(p) == "a $\\frac{a}{b}$ b\n", md(p)


def test_equation_alone_is_display():
    """Equations that are all their paragraph shows (white space aside) are display Equations after what is left of
    it, as hwp's lone_equations rule makes them; one beside text is inline Math."""
    objs = {1: {"Equation Native": eq_native(FRAC)}, 2: {"Equation Native": eq_native(SUBSUP)}}
    text = " " + EMBED + "\t" + EMBED + "\rz" + EMBED + "\r"  # the fields name _1, _2, then _1 again
    seps = [i for i, ch in enumerate(text) if ch == "\x14"]
    chpx = [(i, i + 1, sp(SPEC) + sp(PIC, AT)) for i, ch in enumerate(text) if ch == "\x01"]
    chpx += [(i, i + 1, sp(SPEC) + sp(OLE) + sp(PIC, oid)) for i, oid in zip(seps, (1, 2, 1))]
    p = embed_doc(objs, text=text, chpx=sorted(chpx))
    d, ctx = parse(p)
    assert d.blocks == [Para([Text(" "), Text("\t")]), Equation(r"\frac{a}{b}"), Equation("x_{1}^{2}"),
                        Para([Text("z"), Math(r"\frac{a}{b}")])] and not ctx.skipped, (d.blocks, ctx.skipped)
    assert md(p) == "$$\\frac{a}{b}$$\n\n$$x_{1}^{2}$$\n\nz$\\frac{a}{b}$\n", md(p)


def test_object_in_hidden_text():
    """An object in vanished or deleted text is hidden as its text is and never counted: an equation stays inline
    Math in its Hidden (even alone in its paragraph), a preview is a hidden Image."""
    eq = {1: {"Equation Native": eq_native(FRAC)}}
    for props, kind in ((sp(VANISH), "hidden"), (sp(DELETED), "deleted")):
        d, ctx = parse(embed_doc(eq, text="a" + EMBED + "\r", preview=PNG_PREVIEW, props=props))
        assert d.blocks == [Para([Text("a"), Hidden([Math(r"\frac{a}{b}")], kind)])] and not ctx.skipped, d.blocks
        d, ctx = parse(embed_doc(eq, preview=PNG_PREVIEW, props=props))
        assert d.blocks == [Para([Hidden([Math(r"\frac{a}{b}")], kind)])] and not ctx.skipped, d.blocks
        # a chart, a failed equation, another object with a preview that is not viewable, a missing storage
        for objs, preview in (({1: {"Workbook": WORKBOOK}}, PNG_PREVIEW),
                              ({1: {"Equation Native": b"bad"}}, PNG_PREVIEW),
                              ({1: {"\x01Ole10Native": bytes(8)}}, EMF_PREVIEW), ({2: {"x": b"x"}}, None)):
            d, ctx = parse(embed_doc(objs, text="a" + EMBED + "\r", preview=preview, props=props))
            pic = [Image(EMF if preview is EMF_PREVIEW else PNG, hidden=kind, codepage="cp1252")] if preview else []
            assert d.blocks == [Para([Text("a")])] + pic and not ctx.skipped, (objs, d.blocks, ctx.skipped)
    p = embed_doc(eq, text="a" + EMBED + "\r", props=sp(VANISH))
    assert md(p) == "(1 hidden places left out)\n\na\n", md(p)
    assert md(p, hidden=True) == "a(hidden: $\\frac{a}{b}$)\n", md(p, hidden=True)


# ---- what is and is not a lost object
def test_missing_storage_counted():
    """An id that names no storage is counted under object, never raised; the preview stays."""
    for objs, oid in (({1: {"Equation Native": eq_native(FRAC)}}, 7), ({}, 1), ({}, 0xFFFFFFFF)):
        p = embed_doc(objs, oid=oid, preview=PNG_PREVIEW)
        assert md(p) == "(could not read: 1 embedded object)\n\n![](assets/img-001.png)\n", (oid, md(p))
    # the id is a u32: _4294967295 is found
    p = embed_doc({0xFFFFFFFF: {"Equation Native": eq_native(FRAC)}}, oid=0xFFFFFFFF)
    assert md(p) == "$$\\frac{a}{b}$$\n", md(p)


def test_other_object_png_preview_kept():
    p = embed_doc({1: {"\x01Ole10Native": bytes(8), "\x01CompObj": comp_obj(b"Package")}}, preview=PNG_PREVIEW)
    d, ctx = parse(p)
    assert d.blocks == [Image(PNG, codepage="cp1252")] and not ctx.skipped, (d.blocks, ctx.skipped)
    assert md(p) == "![](assets/img-001.png)\n", md(p)
    # a DIB preview is viewable when it converts to PNG, as Assets converts it; one without its pixels is not
    dib = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, 4, 0, 0, 0, 0)
    for pixels, want in ((b"\x00\x00\xff\x00", "![](assets/img-001.png)\n"),
                         (b"", "(could not read: 1 embedded object)\n\nNot viewable here: assets/img-001.bmp\n")):
        p = embed_doc({1: {"\x01Ole10Native": bytes(8)}}, preview=picf_inline(blip_bitmap(0xF01F, 0x7A8, dib + pixels)))
        assert md(p) == want, md(p)


def test_other_object_emf_preview_counted():
    out = tmp()
    p = embed_doc({1: {"\x01Ole10Native": bytes(8)}}, preview=EMF_PREVIEW)
    got = md(p, out=out)
    assert got == "(could not read: 1 embedded object)\n\nNot viewable here: assets/img-001.emf\n", got
    assert (out / "assets" / "img-001.emf").read_bytes() == EMF
    # no preview, or one that does not decode: counted once, under object, not image
    for preview in (None, picf_inline(b"", pib=3), b"\x01\x02"):
        d, ctx = parse(embed_doc({1: {"WordDocument": bytes(8)}}, preview=preview))
        assert d.blocks == [] and ctx.skipped == {"object": 1}, (preview, d.blocks, ctx.skipped)


def test_equation_preview_not_decoded():
    """An equation that converts replaces its preview, which is not even read."""
    calls = []
    real = doc_pictures.Pictures.inline
    doc_pictures.Pictures.inline = lambda self, at: calls.append(at) or real(self, at)
    try:
        d, ctx = parse(embed_doc({1: {"Equation Native": eq_native(FRAC)}}, preview=PNG_PREVIEW))
        assert d.blocks == [Equation(r"\frac{a}{b}")] and calls == [], (d.blocks, calls)
        d, ctx = parse(embed_doc({1: {"Equation Native": b"bad"}}, preview=PNG_PREVIEW))
        assert d.blocks == [Image(PNG, codepage="cp1252")] and calls == [AT], (d.blocks, calls)
    finally:
        doc_pictures.Pictures.inline = real


# ---- the shapes Word writes
def test_real_embed_shapes():
    eq = {5: {"Equation Native": eq_native(FRAC)}}
    # Word 97 and later: the separator has fSpec, fOle2 and PicLocation the object's id; the \x01 is the preview's PICF
    d, ctx = parse(embed_doc(eq, oid=5, preview=PNG_PREVIEW))
    assert d.blocks == [Equation(r"\frac{a}{b}")] and not ctx.skipped, d.blocks
    other = {5: {"\x01Ole10Native": bytes(8)}}
    d, ctx = parse(embed_doc(other, oid=5, preview=PNG_PREVIEW))
    assert d.blocks == [Image(PNG, codepage="cp1252")] and not ctx.skipped, (d.blocks, ctx.skipped)  # one preview, not two
    # older files: the \x01 shares the separator's run, fOle2 and PicLocation the id: there is no PICF to show
    merged = sp(SPEC) + sp(OLE) + sp(PIC, 5)
    d, ctx = parse(embed_doc(eq, oid=5, one=merged))
    assert d.blocks == [Equation(r"\frac{a}{b}")] and not ctx.skipped, d.blocks
    d, ctx = parse(embed_doc(other, oid=5, one=merged))
    assert d.blocks == [] and ctx.skipped == {"object": 1}, (d.blocks, ctx.skipped)
    # in a text box Word leaves the result's \x01 without fSpec: the object is placed at the field's end, and so
    # it is when the result has no \x01 at all
    for text, one in ((EMBED + "\r", b""), ("\x13 EMBED Equation.3 \x14\x15\r", None)):
        d, ctx = parse(embed_doc(eq, text="a" + text, oid=5, one=one))
        assert d.blocks == [Para([Text("a"), Math(r"\frac{a}{b}")])] and not ctx.skipped, d.blocks
    d, ctx = parse(embed_doc(other, text="a" + EMBED + "\r", oid=5, one=b""))
    assert d.blocks == [Para([Text("a")])] and ctx.skipped == {"object": 1}, (d.blocks, ctx.skipped)
    # the brief's shape: a \x01 with fOle2 outside such a field, its ObjLocation (else PicLocation) the id
    for one in (sp(SPEC) + sp(OLE) + sp(OBJ, 5) + sp(PIC, 9), sp(SPEC) + sp(OLE) + sp(PIC, 5)):
        d, ctx = parse(embed_doc(eq, text="a\x01\r", one=one))
        assert d.blocks == [Para([Text("a"), Math(r"\frac{a}{b}")])] and not ctx.skipped, d.blocks
    # ObjLocation wins on the separator too; a LINK field's cached object is read as an EMBED's
    d, ctx = parse(embed_doc(eq, sep=sp(SPEC) + sp(OLE) + sp(PIC, 9) + sp(OBJ, 5)))
    assert d.blocks == [Equation(r"\frac{a}{b}")] and not ctx.skipped, d.blocks
    d, ctx = parse(embed_doc(eq, text="\x13 LINK Equation.3 x \x14\x01\x15\r", oid=5, preview=PNG_PREVIEW))
    assert d.blocks == [Equation(r"\frac{a}{b}")] and not ctx.skipped, d.blocks
    # a separator without fOle2 (a picture field's result) leaves its \x01 a plain picture
    d, ctx = parse(embed_doc(eq, text="\x13 INCLUDEPICTURE x \x14\x01\x15\r", sep=sp(SPEC), preview=PNG_PREVIEW))
    assert d.blocks == [Image(PNG, codepage="cp1252")] and not ctx.skipped, d.blocks


def test_object_field_cut_by_paragraph_placed():
    """An object field that a paragraph mark cuts before its result put the object out is placed there."""
    d, ctx = parse(embed_doc({1: {"Equation Native": eq_native(FRAC)}}, text="a\x13 EMBED Equation.3 \x14\rb\r"))
    assert d.blocks == [Para([Text("a"), Math(r"\frac{a}{b}")]), Para([Text("b")])], d.blocks
    assert not ctx.skipped, ctx.skipped


def test_object_in_field_instruction_not_read():
    """An EMBED field inside another field's instruction is part of that instruction: nothing read or counted."""
    text = "\x13 X \x13 EMBED Equation.3 \x14\x01\x15\x14y\x15\r"
    sep, one = text.index("\x14"), text.index("\x01")
    chpx = [(sep, sep + 1, sp(SPEC) + sp(OLE) + sp(PIC, 1)), (one, one + 1, sp(SPEC) + sp(PIC, AT))]
    d, ctx = parse(embed_doc({}, text=text, preview=PNG_PREVIEW, chpx=chpx))
    assert d.blocks == [Para([Text("y")])] and not ctx.skipped, (d.blocks, ctx.skipped)


def test_header_objects_skipped():
    """A header is a line of text: its objects are neither read nor counted, as its pictures."""
    s = doc_stories("m\r", headers=[""] * 7 + ["h" + EMBED])
    h = s["ccp"]["text"]
    sep, one = h + 1 + EMBED.index("\x14"), h + 1 + EMBED.index("\x01")
    for objs in ({}, {1: {"Equation Native": eq_native(FRAC)}}, {1: {"Workbook": WORKBOOK}}):
        chpx = [(sep, sep + 1, sp(SPEC) + sp(OLE) + sp(PIC, 1)), (one, one + 1, sp(SPEC) + sp(OLE) + sp(PIC, 1))]
        p = make_doc(tmp() / "a.doc", s["pieces"], ccp=s["ccp"], fc=s["fc"], chpx=chpx, streams=object_pool(objs))
        d, ctx = parse(p)
        assert d.headers == ["h"] and d.blocks == [Para([Text("m")])] and not ctx.skipped, (d.headers, ctx.skipped)


# ---- hostile input
def test_objects_read_once():
    """An object named again and again is read and converted once; each reference to a missing one is a loss."""
    n = 300
    reads, converts = [], []
    real_read, real_tex = Compound.read, doc_objects.mtef.to_tex
    Compound.read = lambda self, name: reads.append(name) or real_read(self, name)
    doc_objects.mtef.to_tex = lambda data, lost=None: converts.append(1) or real_tex(data, lost)
    try:
        t0 = time.monotonic()
        d, ctx = parse(embed_doc({1: {"Equation Native": eq_native(FRAC)}}, text="a" + EMBED * n + "\r"))
        assert len(d.blocks[0].inlines) == n + 1 and not ctx.skipped, ctx.skipped
        assert reads.count("ObjectPool/_1/Equation Native") == 1 and converts == [1], (reads, converts)
        d, ctx = parse(embed_doc({}, text="a" + EMBED * n + "\r", preview=EMF_PREVIEW))
        assert ctx.skipped == {"object": n}, ctx.skipped
        assert time.monotonic() - t0 < 10
    finally:
        Compound.read, doc_objects.mtef.to_tex = real_read, real_tex


def test_bad_streams_counted():
    """A stream that cannot be read (a broken chain, damaged, too large) or an Equation Native past the size MTEF
    is read to costs its object alone, counted; another ReadError ends the document."""
    p = embed_doc({1: {"Equation Native": eq_native(FRAC)}}, preview=PNG_PREVIEW)
    real = Compound.read
    for err in (CfbError("chain"), ReadError("damaged", "x"), ReadError("too_large", "part: x"), KeyError("x")):
        def broken(self, name, err=err):
            if name.startswith("ObjectPool"):
                raise err
            return real(self, name)
        Compound.read = broken
        try:
            assert md(p) == "(could not read: 1 embedded object)\n\n![](assets/img-001.png)\n", (err, md(p))
        finally:
            Compound.read = real
    Compound.read = lambda self, name: (_ for _ in ()).throw(ReadError("timeout")) if "Pool" in name \
        else real(self, name)
    try:
        try:
            parse(p)
            raise AssertionError("a timeout must end the document")
        except ReadError as e:
            assert e.kind == "timeout"
    finally:
        Compound.read = real
    old = doc_objects.mtef.MAX_BYTES
    doc_objects.mtef.MAX_BYTES = len(FRAC) - 1
    try:
        assert md(p) == "(could not read: 1 embedded object)\n\n![](assets/img-001.png)\n", md(p)
    finally:
        doc_objects.mtef.MAX_BYTES = old


def test_partial_equation_kept_and_counted():
    """An unknown record or an unmapped template stops the conversion: what was built is kept, as its text is real,
    and the preview follows it, as a failed equation's does; the object is counted once."""
    body = l5(c5("a") + bytes((99, 0)) + c5("b"))
    assert tex_of(mtef_v5(body)) == ("a", {"stop"})
    assert tex_of(mtef_v5(l5(c5("a") + t5(15, l5(c5("x")), NULL5, NULL5) + c5("b")))) == ("a", {"stop"})
    assert tex_of(mtef_v5(l5(c5("a=") + t5(11, l5(c5("1")))[:-1]))) == (r"a=\frac{1}{}", {"stop"})  # cut short
    p = embed_doc({1: {"Equation Native": eq_native(mtef_v5(body))}}, preview=PNG_PREVIEW)
    d, ctx = parse(p)
    assert d.blocks == [Equation("a"), Image(PNG, codepage="cp1252")] and ctx.skipped == {"object": 1}, (d.blocks, ctx.skipped)
    assert md(p) == "(could not read: 1 embedded object)\n\n$$a$$\n\n![](assets/img-001.png)\n", md(p)
    # beside text: inline Math, the preview after the paragraph; hidden, both hidden and nothing counted
    d, ctx = parse(embed_doc({1: {"Equation Native": eq_native(mtef_v5(body))}}, text="z" + EMBED + "\r",
                             preview=PNG_PREVIEW))
    assert d.blocks == [Para([Text("z"), Math("a")]), Image(PNG, codepage="cp1252")] and ctx.skipped == {"object": 1}, d.blocks
    d, ctx = parse(embed_doc({1: {"Equation Native": eq_native(mtef_v5(body))}}, text="z" + EMBED + "\r",
                             preview=PNG_PREVIEW, props=sp(VANISH)))
    assert d.blocks == [Para([Text("z"), Hidden([Math("a")], "hidden")]), Image(PNG, hidden="hidden", codepage="cp1252")], d.blocks
    assert not ctx.skipped, ctx.skipped
    # no preview: the partial equation alone, still counted once
    d, ctx = parse(embed_doc({1: {"Equation Native": eq_native(mtef_v5(body))}}))
    assert d.blocks == [Equation("a")] and ctx.skipped == {"object": 1}, (d.blocks, ctx.skipped)
    # nothing built: the equation failed, so only its preview shows
    assert md(embed_doc({1: {"Equation Native": eq_native(mtef_v5(bytes((99, 0))))}}, preview=PNG_PREVIEW)) == \
        "(could not read: 1 embedded object)\n\n![](assets/img-001.png)\n"


def test_template_first_equation_preview_only():
    """An unmapped template before anything was built (a v3 sum first) leaves no TeX: a ValueError, then the preview
    alone, counted once."""
    sum_first = mtef_v3(l3(t3(20, l3(c3("x")), NULL3, NULL3, c3("∑")) + c3("=1")))
    try:
        to_tex(sum_first)
        raise AssertionError("no TeX before the template")
    except ValueError:
        pass
    d, ctx = parse(embed_doc({1: {"Equation Native": eq_native(sum_first)}}, preview=PNG_PREVIEW))
    assert d.blocks == [Image(PNG, codepage="cp1252")] and ctx.skipped == {"object": 1}, (d.blocks, ctx.skipped)


def test_v3_fence_sides():
    """v3 fence variations: 0 both sides, 1 the left alone, 2 the right alone (the absent side a '.'); another
    stops the conversion. With fewer characters than sides, a character goes to the side it opens or closes, the
    other keeps the template's own."""
    x = l3(c3("x"))
    assert tex_of(mtef_v3(t3(1, x, c3("("), c3(")")))) == (r"\left(x\right)", set())
    assert tex_of(mtef_v3(t3(2, x, c3("{"), var=1))) == (r"\left\{x\right.", set())
    assert tex_of(mtef_v3(t3(1, x, c3(")"), var=2))) == (r"\left.x\right)", set())
    assert tex_of(mtef_v3(t3(3, x, var=1))) == (r"\left[x\right.", set())
    assert tex_of(mtef_v3(t3(1, x, c3(")")))) == (r"\left(x\right)", set())
    assert tex_of(mtef_v3(t3(1, x, c3("[")))) == (r"\left[x\right)", set())
    assert tex_of(mtef_v5(t5(1, l5(c5("x")), c5(")"), var=3))) == (r"\left(x\right)", set())
    assert tex_of(mtef_v3(l3(c3("a") + t3(1, x, c3("("), c3(")"), var=3) + c3("b")))) == ("a", {"stop"})
    # the stop takes the partial path: the TeX and the preview, counted once
    p = embed_doc({1: {"Equation Native": eq_native(mtef_v3(l3(c3("a") + t3(1, x, c3("("), var=7))))}},
                  preview=PNG_PREVIEW)
    d, ctx = parse(p)
    assert d.blocks == [Equation("a"), Image(PNG, codepage="cp1252")] and ctx.skipped == {"object": 1}, (d.blocks, ctx.skipped)


def test_symbol_loss_counted():
    """A private-use character and an embellishment with no TeX here are lost symbols: the conversion goes on, the
    TeX stays balanced, and the object is counted with its preview. A space that isprintable rejects (U+00A0,
    U+2009) is spacing, no loss."""
    assert tex_of(mtef_v3(c3("ab"))) == ("ab", {"symbol"})
    assert tex_of(mtef_v3(c3("ab", 0x81))) == (r"\text{ab}", {"symbol"})
    assert tex_of(mtef_v5(c5("x", opts=1) + b"\x06\x00\x63\x00" + c5("y"))) == ("xy", {"symbol"})
    assert tex_of(mtef_v3(c3("x", opts=2) + b"\x06\x09\x06\x07\x00")) == (r"\hat{x}", {"symbol"})  # back prime
    assert tex_of(mtef_v3(t3(14, l3(c3("a")), l3(c3("b"))))) == (r"\frac{a}{b}", {"symbol"})
    for sp_ in (" ", " "):
        assert tex_of(mtef_v3(c3("a" + sp_ + "b"))) == (r"a{\ }b", set()), sp_
        assert tex_of(mtef_v3(c3("a" + sp_ + "b", 0x81))) == (r"\text{a b}", set()), sp_
    p = embed_doc({1: {"Equation Native": eq_native(mtef_v3(c3("ab")))}}, preview=PNG_PREVIEW)
    d, ctx = parse(p)
    assert d.blocks == [Equation("ab"), Image(PNG, codepage="cp1252")] and ctx.skipped == {"object": 1}, (d.blocks, ctx.skipped)
    d, ctx = parse(embed_doc({1: {"Equation Native": eq_native(mtef_v3(c3("a b")))}}, preview=PNG_PREVIEW))
    assert d.blocks == [Equation(r"a{\ }b")] and not ctx.skipped, (d.blocks, ctx.skipped)


def test_mtef_records():
    """The records an equation holds: size and font records skipped, nudges, rulers, fences, roots, bars, piles,
    matrices, embellishments, text, symbols and v5's definitions."""
    # Equation Editor 3.0's own layout: FULL, a LINE holding FULL and a LINE, scripts after a SUB size, text '2'
    v3 = mtef_v3(b"\x0a" + l3(b"\x0a" + l3(c3("E=mc") + t3(15, b"\x0b", NULL3, l3(c3("2", 0x81))) + b"\x0a")))
    assert to_tex(v3) == r"E=mc^{\text{2}}"
    # sizes (FULL to SUBSYM, SIZE in its three forms), FONT, a nudged CHAR, a LINE with a RULER, trailing bytes
    size = b"\x09\x02\x80" + b"\x09\x65\x0c\x00" + b"\x09\x64\x01\x00\x01"
    nudged = bytes((0x82, 0x83, 0x81, 0x83)) + u16(ord("x"))  # CHAR with xfLMOVE: dx, dy, then typeface and MTCode
    body = b"\x0a\x0b\x0c\x0d\x0e" + size + b"\x08\x83\x00Times New Roman\x00" + nudged
    assert to_tex(mtef_v3(body + b"\x21\x07\x01\x00\x10\x00" + c3("y") + b"\x00") + b"junk") == "xy"
    # fences, with their characters or the template's own (v5: tvFENCE_L 1, tvFENCE_R 2 say which are there)
    assert to_tex(mtef_v3(t3(1, l3(c3("x")), c3("("), c3(")")))) == r"\left(x\right)"
    assert to_tex(mtef_v5(t5(3, l5(c5("x")), c5("["), c5("]"), var=3))) == r"\left[x\right]"
    assert to_tex(mtef_v5(t5(2, l5(c5("x")), var=3))) == r"\left\{x\right\}"
    assert to_tex(mtef_v5(t5(1, l5(c5("x")), c5("("), var=1))) == r"\left(x\right."
    assert to_tex(mtef_v5(t5(0, l5(c5("x")), c5("⟩"), var=2))) == r"\left.x\right\rangle"
    assert to_tex(mtef_v5(t5(1, l5(c5("x")), c5("a"), c5(")"), var=3))) == r"\left.x\right)"  # not a delimiter
    assert to_tex(mtef_v3(t3(13, l3(c3("x")), NULL3))) == r"\sqrt{x}"
    assert to_tex(mtef_v5(t5(10, l5(c5("x")), l5(c5("3")), var=1))) == r"\sqrt[3]{x}"
    assert to_tex(mtef_v3(t3(16, l3(c3("x"))) + t3(17, l3(c3("y"))))) == r"\underline{x}\overline{y}"
    assert to_tex(mtef_v5(t5(12, l5(c5("x"))) + t5(13, l5(c5("y"))))) == r"\underline{x}\overline{y}"
    # a pile of lines and a 2 x 2 matrix (valign, h_just, v_just, rows, cols, then the partition bytes)
    assert to_tex(mtef_v3(b"\x04\x02\x01" + l3(c3("a")) + l3(c3("b")) + b"\x00")) == \
        r"\begin{array}{c}a\\b\end{array}"
    matrix = b"\x05\x00\x00\x00\x02\x02\x00\x00" + l3(c3("a")) + l3(c3("b")) + l3(c3("c")) + l3(c3("d")) + b"\x00"
    assert to_tex(mtef_v3(matrix)) == r"\begin{matrix}a&b\\c&d\end{matrix}"
    # embellishments: v3 xfEMBELL (0x2), v5 mtefOPT_CHAR_EMBELL (0x1)
    assert to_tex(mtef_v3(c3("x", opts=2) + b"\x06\x09\x06\x05\x00")) == r"\hat{x}'"  # hat, then a prime
    assert to_tex(mtef_v3(c3("x", opts=2) + b"\x06\x05\x06\x09\x00")) == r"\hat{x'}"  # a prime, then hat
    assert to_tex(mtef_v5(c5("x", opts=1) + b"\x06\x00\x08\x00")) == r"\tilde{x}"
    assert to_tex(mtef_v5(c5("x", opts=1) + b"\x06\x00\x63\x00")) == "x"  # an embellishment with no TeX here
    # text, escaped; symbols; unknown private-use characters are left out
    assert to_tex(mtef_v3(c3("a_b", 0x81) + c3("α≤") + c3(""))) == r"\text{a\_b}\alpha \leq"
    assert to_tex(mtef_v3(c3("a", 0x81) + c3("\x07", 0x81) + c3("b", 0x81))) == r"\text{ab}"  # a control character
    # v5 CHAR options: a function's start (0x2), the 8- and 16-bit font positions after the MTCode (0x4, 0x10)
    chars = (bytes((2, 2, 0x82)) + u16(ord("s")) + bytes((2, 4, 0x83)) + u16(ord("x")) + b"x"
             + bytes((2, 0x10, 0x83)) + u16(ord("y")) + b"y\x00")
    assert to_tex(mtef_v5(chars)) == "sxy"
    # v5 definitions before the equation: ENCODING_DEF, FONT_DEF, EQN_PREFS (two sizes, one space, one style),
    # FONT_STYLE_DEF, COLOR_DEF (RGB), COLOR
    defs = (b"\x13MTCode\x00" + b"\x11\x01Times New Roman\x00" + b"\x12\x00\x02\x1f\x2f\x01\xf0\x01\x01\x00"
            + b"\x08\x01\x00" + b"\x10\x00" + bytes(6) + b"\x0f\x00")
    assert tex_of(mtef_v5(defs + l5(c5("y")))) == ("y", set())


def test_mtef_bounds():
    """Every count and length is checked against the data; the walk and the TeX stay linear in the input."""
    for bad in (b"", b"\x05", b"\x05\x01\x00\x06\x00DSMT6", b"\x03\x01\x01\x03\x00", b"\x02" + bytes(10)):
        try:
            to_tex(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    # a string, a ruler, a matrix's partitions and a nudge past the end, a string over 256 bytes, a LINE with
    # xfLSPACE and an END with options each stop the conversion with what was built
    for tail in (b"\x08\x83\x00Times", b"\x07\xff\x00", b"\x05\x00\x00\x00\xff\xff", bytes((0x82, 0x80, 0x80, 1)),
                 b"\x08\x83\x00" + b"x" * 300 + b"\x00", b"\x41\x00" + c3("b") + b"\x00", b"\x10\x00"):
        assert tex_of(mtef_v3(c3("a"))[:-1] + tail) == ("a", {"stop"}), tail
    # the size limit, then 1 MiB of size records, a character with 300,000 embellishments, 20,000 sibling fractions
    try:
        to_tex(mtef_v3(b"\x0a" * (1 << 20)))
        raise AssertionError("past MAX_BYTES")
    except ValueError:
        pass
    t0 = time.monotonic()
    assert to_tex(mtef_v3(b"\x0a" * ((1 << 20) - 16) + c3("x"))) == "x"
    big = mtef_v3(c3("x", opts=2) + b"\x06\x09" * 300_000 + b"\x00")
    tex = to_tex(big)
    assert tex.startswith(r"\hat{\hat{") and len(tex) == 300_000 * 6 + 1
    tex = to_tex(mtef_v5(l5(t5(11, l5(c5("a")), l5(c5("b"))) * 20_000)))
    assert tex == r"\frac{a}{b}" * 20_000
    assert time.monotonic() - t0 < 20


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
