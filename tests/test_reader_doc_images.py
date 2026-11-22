"""Word 97-2003 (.doc) pictures through OfficeArt: inline pictures (\\x01, a PICF in the Data stream), floating ones
(\\x08, PlcSpaMom or PlcSpaHdr, the DggInfo's shapes and BStore), BLIP kinds, hidden and placed pictures, hostile
input.

Run: python -I -X utf8 tests/test_reader_doc_images.py
"""
import struct
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import limits, markdown, officeart  # noqa: E402
from reader.containers import flate  # noqa: E402
from reader.containers.cfb import Compound  # noqa: E402
from reader.formats import doc  # noqa: E402
from reader import raster  # noqa: E402

raster.backend = lambda: "none"  # these tests keep metafiles as files; drawing them is test_reader_raster's
from reader.model import Cell, Context, Image, Para, Table, Text  # noqa: E402
from reader_fix import (DOC_EXTRA, blip_bitmap, blip_metafile, blip_png, dgg_with_bstore, doc_spa,  # noqa: E402
                        doc_stories, make_doc, oa_container, oa_fbse, oa_group, oa_record, oa_shape, picf_inline)

SPEC, PIC, VANISH, DELETED, OLE = 0x0855, 0x6A03, 0x083C, 0x0800, 0x080A
IN_TABLE, TTP = 0x2416, 0x2417
AT = 16  # where the tests put a PICF in the Data stream

PNG = b"\x89PNG\r\n\x1a\n" + bytes(24)
JPEG = b"\xff\xd8\xff\xe0" + bytes(28)
# a 1x1 24-bit DIB: BITMAPINFOHEADER, then one row of one red pixel padded to 4 bytes
DIB = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, 4, 0, 0, 0, 0) + b"\x00\x00\xff\x00"
EMF = struct.pack("<II", 1, 88) + bytes(32) + b" EMF" + bytes(44)
WMF = struct.pack("<HHHIHIH", 1, 9, 0x300, 20, 0, 3, 0) + struct.pack("<IH", 3, 0) + bytes(16)
PICT = struct.pack(">H4h", 0, 0, 0, 10, 10) + b"\x00\x11\x02\xff\x0c\x00" + bytes(30)  # without its 512-byte head


def tmp():
    return Path(tempfile.mkdtemp())


def u16(n):
    return struct.pack("<H", n)


def sp(op, value=1):
    """One sprm, its operand sized by op >> 13."""
    return u16(op) + value.to_bytes((1, 1, 2, 4, 2, 2, 0, 3)[op >> 13], "little")


def parse(path):
    ctx = Context()
    return doc.parse(Path(path), ctx), ctx


def md(path, hidden=False, out=None):
    d, ctx = parse(path)
    return markdown.render(d, (out or tmp()) / "assets", hidden, ctx.skipped)


def inline_doc(picf, text="\x01\r", props=b"", data=True, **kw):
    """`text` (one Unicode piece) whose every \\x01 has fSpec and PicLocation AT, then `props`; the Data stream holds
    `picf` at AT, or is left out when `data` is false."""
    g = sp(SPEC) + sp(PIC, AT) + props
    kw.setdefault("chpx", [(i, i + 1, g) for i, ch in enumerate(text) if ch == "\x01"])
    streams = {"Data": bytes(AT) + picf} if data else None
    return make_doc(tmp() / "a.doc", [(text, False)], streams=streams, **kw)


def float_doc(text, dgg, anchors=None, props=b"", fc=None, **kw):
    """`text` whose every \\x08 has fSpec, then `props`; PlcSpaMom anchors (default: the k-th \\x08 to spid
    1025 + k) and DggInfo `dgg`."""
    at = [i for i, ch in enumerate(text) if ch == "\x08"]
    if anchors is None:
        anchors = [(cp, 1025 + k) for k, cp in enumerate(at)]
    entries = {50: dgg}
    if anchors:
        entries[40] = doc_spa(anchors)
    entries.update(fc or {})
    kw.setdefault("chpx", [(i, i + 1, sp(SPEC) + props) for i in at])
    return make_doc(tmp() / "a.doc", [(text, False)], fc=entries, **kw)


# ---- the brief's tests
def test_inline_png():
    out = tmp()
    assert md(inline_doc(picf_inline(blip_png(PNG))), out=out) == "![](assets/img-001.png)\n"
    assert (out / "assets" / "img-001.png").read_bytes() == PNG


def test_inline_jpeg():
    assert md(inline_doc(picf_inline(blip_bitmap(0xF01D, 0x46A, JPEG)))) == "![](assets/img-001.jpg)\n"
    # the second instance of a kind carries a second UID; CMYK JPEG (0x6E2/0x6E3) and 0xF02A are JPEG too
    for typ, inst in ((0xF01D, 0x46B), (0xF01D, 0x6E3), (0xF02A, 0x6E2)):
        assert md(inline_doc(picf_inline(blip_bitmap(typ, inst, JPEG)))) == "![](assets/img-001.jpg)\n", hex(inst)


def test_inline_dib_becomes_png():
    out = tmp()
    assert md(inline_doc(picf_inline(blip_bitmap(0xF01F, 0x7A8, DIB))), out=out) == "![](assets/img-001.png)\n"
    assert (out / "assets" / "img-001.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_inline_emf_listed():
    out = tmp()
    got = md(inline_doc(picf_inline(blip_metafile(0xF01A, 0x3D4, EMF))), out=out)
    assert got == "Not viewable here: assets/img-001.emf\n", got
    assert (out / "assets" / "img-001.emf").read_bytes() == EMF
    # stored, not deflated (compression 0xFE), and the WMF kind
    got = md(inline_doc(picf_inline(blip_metafile(0xF01B, 0x216, WMF, deflate=False))))
    assert got == "Not viewable here: assets/img-001.wmf\n", got


def test_floating_png_via_bstore():
    assert md(float_doc("\x08\r", dgg_with_bstore([blip_png(PNG)]))) == "![](assets/img-001.png)\n"
    # the anchor's FSPA names the shape, the shape its pib, the pib its BStore entry
    dgg = dgg_with_bstore([blip_bitmap(0xF01D, 0x46A, JPEG), blip_png(PNG)], shapes=[(1025, 2), (1026, 1)])
    assert md(float_doc("a\x08b\r", dgg, anchors=[(1, 1026)])) == "ab\n\n![](assets/img-001.jpg)\n"


def test_floating_delay_blip():
    p = float_doc("\x08\r", dgg_with_bstore([DOC_EXTRA]), word_extra=blip_png(PNG))
    assert md(p) == "![](assets/img-001.png)\n"


def test_missing_data_stream_counted():
    got = md(inline_doc(picf_inline(blip_png(PNG)), "a\x01\r", data=False))
    assert got == "(could not read: 1 image)\n\na\n", got


def test_metafile_bomb_counted():
    bomb = blip_metafile(0xF01A, 0x3D4, EMF + bytes(4 << 20))
    assert len(bomb) < 64 * 1024
    t0 = time.monotonic()
    got = md(inline_doc(picf_inline(bomb), "a\x01\r"))
    assert got == "(could not read: 1 image)\n\na\n", got
    assert time.monotonic() - t0 < 10


# ---- hidden and deleted pictures follow their text
def test_picture_in_hidden_text():
    p = inline_doc(picf_inline(blip_png(PNG)), "a\x01\r", props=sp(VANISH))
    assert md(p) == "(1 hidden places left out)\n\na\n", md(p)
    assert md(p, hidden=True) == "a\n\n(hidden) ![](assets/img-001.png)\n", md(p, hidden=True)
    d, ctx = parse(p)
    assert d.blocks == [Para([Text("a")]), Image(PNG, hidden="hidden", codepage="cp1252")] and not ctx.skipped, (d.blocks, ctx.skipped)
    d, ctx = parse(inline_doc(picf_inline(blip_png(PNG)), "a\x01\r", props=sp(DELETED)))
    assert d.blocks == [Para([Text("a")]), Image(PNG, hidden="deleted", codepage="cp1252")] and not ctx.skipped, (d.blocks, ctx.skipped)
    d, ctx = parse(float_doc("a\x08\r", dgg_with_bstore([blip_png(PNG)]), props=sp(VANISH)))
    assert d.blocks == [Para([Text("a")]), Image(PNG, hidden="hidden", codepage="cp1252")] and not ctx.skipped, (d.blocks, ctx.skipped)


# ---- where pictures go: after their paragraph, in its cell or note
def test_picture_in_table_cell():
    cell, row = u16(0) + sp(IN_TABLE), u16(0) + sp(IN_TABLE) + sp(TTP)
    p = inline_doc(picf_inline(blip_png(PNG)), "x\x01\x07z\x07\x07y\r", papx=[(0, 5, cell), (5, 6, row)])
    d, ctx = parse(p)
    want = [Table([[Cell([Para([Text("x")]), Image(PNG, codepage="cp1252")]), Cell([Para([Text("z")])])]]), Para([Text("y")])]
    assert d.blocks == want and not ctx.skipped, d.blocks
    assert md(p).split("\n")[0] == "|x<br>![](assets/img-001.png)|z|", md(p)


def test_picture_in_note():
    s = doc_stories("a\x02\r", footnotes=[(1, "\x02 \x01\r")])
    pic = s["ccp"]["text"] + 2  # the footnote's \x01 in the whole text
    p = inline_doc(picf_inline(blip_png(PNG)), s["pieces"][0][0], ccp=s["ccp"], fc=s["fc"],
                   chpx=[(pic, pic + 1, sp(SPEC) + sp(PIC, AT))])
    d, ctx = parse(p)
    assert d.notes == {"fn-1": [Para([Text(" ")]), Image(PNG, codepage="cp1252")]} and not ctx.skipped, (d.notes, ctx.skipped)
    assert md(p) == "a[^1]\n\n[^1]: ![](assets/img-001.png)\n", md(p)


# ---- what is and is not a lost picture
def test_textbox_anchor_not_counted():
    p = float_doc("a\x08\r", dgg_with_bstore([], shapes=[(1025, None)]))
    d, ctx = parse(p)
    assert md(p) == "a\n" and not ctx.skipped, ctx.skipped
    # a \x08 with no FSPA at its CP, a \x08 without fSpec: no picture, no loss
    for anchors, props in (([(5, 1025)], b""), (None, sp(SPEC, 0))):
        p = float_doc("a\x08\r", dgg_with_bstore([blip_png(PNG)]), anchors=anchors, props=props)
        d, ctx = parse(p)
        assert md(p) == "a\n" and not ctx.skipped, (anchors, ctx.skipped)


def test_unresolvable_anchor_counted():
    """An FSPA whose spid no drawing holds is a lost shape, counted once an FSPA under shape; so is every FSPA of a
    document with anchors and no DggInfo."""
    got = md(float_doc("a\x08\r", dgg_with_bstore([blip_png(PNG)]), anchors=[(1, 4000)]))
    assert got == "(could not read: 1 shape)\n\na\n", got
    got = md(float_doc("a\x08\x08\r", b""))
    assert got == "(could not read: 2 shapes)\n\na\n", got


def test_inline_shape_without_picture_not_counted():
    """Word keeps every inline drawing object (a horizontal rule, inline WordArt, an AutoShape) as an MM_SHAPE PICF:
    an SpContainer with no FBSE after it and no pib (or pib 0) is no picture and no loss."""
    for picf in (picf_inline(b""), picf_inline(b"", pib=0), picf_inline(b"", 0x66, name=b"x")):
        d, ctx = parse(inline_doc(picf, "a\x01\r"))
        assert d.blocks == [Para([Text("a")])] and not ctx.skipped, ctx.skipped
    # still lost: a pib the BStore does not hold, and a PICF with no SpContainer at all
    p = inline_doc(picf_inline(b"", pib=3), "a\x01\r", fc={50: dgg_with_bstore([blip_png(PNG)], shapes=[])})
    assert md(p) == "(could not read: 1 image)\n\na\n", md(p)
    bare = struct.pack("<IHH", 68, 0x44, 0x64).ljust(68, b"\0")
    assert md(inline_doc(bare, "a\x01\r")) == "(could not read: 1 image)\n\na\n"


def test_group_members_in_record_order():
    """An anchor naming a group shows its members' pictures in record order, nested groups' too; a member without a
    pib is skipped, one whose pib, BStore entry or BLIP fails is counted under image."""
    png, jpeg = blip_png(PNG), blip_bitmap(0xF01D, 0x46A, JPEG)
    group = oa_group(1025, oa_shape(1026, 2), oa_shape(1027), oa_group(1028, oa_shape(1029, 1)))
    dgg = dgg_with_bstore([png, jpeg], shapes=[group])
    d, ctx = parse(float_doc("a\x08\r", dgg))
    assert d.blocks == [Para([Text("a")]), Image(JPEG, codepage="cp1252"), Image(PNG, codepage="cp1252")] and not ctx.skipped, (d.blocks, ctx.skipped)
    # a member anchored itself is that member's picture
    d, ctx = parse(float_doc("a\x08\r", dgg, anchors=[(1, 1029)]))
    assert d.blocks == [Para([Text("a")]), Image(PNG, codepage="cp1252")] and not ctx.skipped, (d.blocks, ctx.skipped)
    # one bad member: the other is shown, the bad one counted
    group = oa_group(1025, oa_shape(1026, 1), oa_shape(1027, 9))
    got = md(float_doc("a\x08\r", dgg_with_bstore([png], shapes=[group])))
    assert got == "(could not read: 1 image)\n\na\n\n![](assets/img-001.png)\n", got


def test_group_anchored_again_bounded():
    """A group's members are placed once a member in all: anchors past that (no Word file has them) count the
    group's pictures under image instead of placing them again, so n anchors cost n, not n x members."""
    m, n = 400, 400
    group = oa_group(1025, *(oa_shape(1026 + k, 1) for k in range(m)))
    p = float_doc("\x08" * n + "\r", dgg_with_bstore([blip_png(PNG)], shapes=[group]),
                  anchors=[(k, 1025) for k in range(n)], chpx=[(0, n, sp(SPEC))])
    t0 = time.monotonic()
    d, ctx = parse(p)
    assert len(d.blocks) == m and ctx.skipped == {"image": (n - 1) * m}, (len(d.blocks), ctx.skipped)
    assert time.monotonic() - t0 < 10


def test_pib_out_of_range_counted():
    for shapes in ([(1025, 5)], [(1025, 0xFFFFFFFF)]):
        got = md(float_doc("a\x08\r", dgg_with_bstore([blip_png(PNG)], shapes=shapes)))
        assert got == "(could not read: 1 image)\n\na\n", (shapes, got)
    # an empty BStore slot (cRef 0) and a BLIP that is not there (no DggInfo BStore entry, foDelay past WordDocument)
    empty = dgg_with_bstore([blip_png(PNG)])
    empty = empty.replace(oa_fbse(blip_png(PNG)), oa_fbse(blip_png(PNG), cref=0))
    for dgg in (empty, dgg_with_bstore([0x7FFFFFF0])):
        assert md(float_doc("a\x08\r", dgg)) == "(could not read: 1 image)\n\na\n"


def test_header_pictures_skipped():
    """A header is a line of text, so its pictures (\\x01 and \\x08) are not read at all, as the docx reader drops
    them: a large header picture does not use up the image budget the body's pictures need, and a broken one is not
    counted."""
    big = b"\x89PNG\r\n\x1a\n" + bytes(4096)
    s = doc_stories("m\x08\r", headers=[""] * 7 + ["h\x01\x08"])
    h = s["ccp"]["text"]  # the header story's first CP: its \x01 is at h + 1, its \x08 at h + 2

    def run(picf, header_pib):
        dgg = dgg_with_bstore([blip_png(PNG), blip_png(big)], shapes=[(1025, 1)], header_shapes=[(1026, header_pib)])
        fc = {**s["fc"], 40: doc_spa([(1, 1025)]), 41: doc_spa([(2, 1026)]), 50: dgg}
        chpx = [(1, 2, sp(SPEC)), (h + 1, h + 2, sp(SPEC) + sp(PIC, AT)), (h + 2, h + 3, sp(SPEC))]
        return make_doc(tmp() / "a.doc", s["pieces"], ccp=s["ccp"], fc=fc, chpx=chpx,
                        streams={"Data": bytes(AT) + picf})
    old = doc.IMAGE_BYTES
    doc.IMAGE_BYTES = len(big)
    try:
        for p in (run(picf_inline(blip_png(big)), 2), run(b"\x01\x02", 9)):
            d, ctx = parse(p)
            assert d.headers == ["h"] and d.blocks == [Para([Text("m")]), Image(PNG, codepage="cp1252")], (d.headers, d.blocks)
            assert not ctx.skipped, ctx.skipped
    finally:
        doc.IMAGE_BYTES = old


# ---- more kinds and layouts
def test_inline_variants():
    # MM_SHAPEFILE: cchPicName and the name before the SpContainer
    assert md(inline_doc(picf_inline(blip_png(PNG), 0x66, name=b"x.png"))) == "![](assets/img-001.png)\n"
    # mm 8: a raw WMF after the PICF
    assert md(inline_doc(picf_inline(WMF, 8))) == "Not viewable here: assets/img-001.wmf\n"
    # no FBSE after the inline shape: its pib in the document's BStore
    p = inline_doc(picf_inline(b"", pib=1), fc={50: dgg_with_bstore([blip_png(PNG)], shapes=[])})
    assert md(p) == "![](assets/img-001.png)\n"
    # an FBSE whose BLIP is delay-loaded from WordDocument
    fbse = oa_fbse(delay=DOC_EXTRA)
    assert md(inline_doc(picf_inline(b"", fbses=fbse), word_extra=blip_png(PNG))) == "![](assets/img-001.png)\n"
    # PICT is kept with its 512-byte file head and listed
    out = tmp()
    got = md(inline_doc(picf_inline(blip_metafile(0xF01C, 0x542, PICT))), out=out)
    assert got == "Not viewable here: assets/img-001.pict\n", got
    assert (out / "assets" / "img-001.pict").read_bytes() == bytes(512) + PICT


def test_field_instruction_and_ole_not_pictures():
    """A \\x01 in a field instruction (a form field's data) is not shown, as text there is not; an OLE object's \\x01
    (fOle2) is an embedded object (test_reader_doc_objects.py), here one with no ObjectPool storage. Neither is a
    lost picture."""
    p = inline_doc(b"", "\x13FORMTEXT \x01\x14x\x15\r", data=False)
    d, ctx = parse(p)
    assert md(p) == "x\n" and not ctx.skipped, ctx.skipped
    d, ctx = parse(inline_doc(b"", "a\x01\r", props=sp(OLE), data=False))
    assert d.blocks == [Para([Text("a")])] and ctx.skipped == {"object": 1}, ctx.skipped


def ffdata(kind, res, wdef=0, items=(), text=""):
    """A form field's NilPICFAndBinData: the 68-byte header, then the FFData (MS-DOC 2.9.78)."""
    def xstz(s):
        return u16(len(s)) + s.encode("utf-16-le") + u16(0)
    body = struct.pack("<IHHH", 0xFFFFFFFF, kind | res << 2, 0, 0) + xstz("F1")
    body += xstz(text) if kind == 0 else u16(wdef)
    body += xstz("") * 5
    if kind == 2:
        body += struct.pack("<HHH", 0xFFFF, len(items), 0) + b"".join(u16(len(s)) + s.encode("utf-16-le") for s in items)
    return struct.pack("<IH", 0x44 + len(body), 0x44) + bytes(62) + body


def test_form_data_is_read_once_a_field_and_only_for_a_form_field():
    n = 200_000
    dd = ffdata(2, 1, 0, ("DD-FIRST", "DD-SECOND" + "x" * 30_000))
    text = "pick \x13 FORMDROPDOWN " + "\x01" * n + "\x15\r"
    at = text.index("\x01")
    p = inline_doc(dd, text, chpx=[(at, at + n, sp(SPEC) + sp(PIC, AT))])
    t = time.perf_counter()
    out = md(p)
    assert time.perf_counter() - t < 10, time.perf_counter() - t
    assert out.startswith("pick DD-SECONDxxx"), out[:40]
    link = md(inline_doc(ffdata(1, 1), 'see \x13 HYPERLINK "http://example.com/" \x01\x14site\x15\r'))
    assert "☒" not in link and "site" in link, link


def test_form_data_at_one_place_is_read_once_a_document():
    n = 100_000
    dd = ffdata(2, 1, 0, ("DD-FIRST", "DD-SECOND" + "x" * 30_000))
    p = inline_doc(dd, "\x13\x01\x15" * n + "\r", chpx=[(0, 3 * n, sp(SPEC) + sp(PIC, AT))])
    t = time.perf_counter()
    out = md(p)
    assert time.perf_counter() - t < 10 and out.startswith("DD-SECONDxxx"), (time.perf_counter() - t, out[:40])
    old = limits.FORM_BYTES
    limits.FORM_BYTES = 0
    try:
        _, ctx = parse(inline_doc(dd, "pick \x13 FORMDROPDOWN \x01\x15\r"))
    finally:
        limits.FORM_BYTES = old
    assert ctx.skipped["form"] == 1, ctx.skipped


def test_form_fields_show_their_state():
    """A legacy check box shows ☒ or ☐ (its result, else its default), a drop-down its chosen entry, a text field its
    result; form data that cannot be read shows nothing."""
    def box(res, default):
        return md(inline_doc(ffdata(1, res, default), "agree \x13 FORMCHECKBOX \x01\x14\x15 end\r"))
    assert box(1, 0) == "agree ☒ end\n" and box(0, 1) == "agree ☐ end\n" and box(25, 1) == "agree ☒ end\n", box(1, 0)
    dd = ffdata(2, 1, 0, ("DD-FIRST", "DD-SECOND"))
    assert md(inline_doc(dd, "pick \x13 FORMDROPDOWN \x01\x15\r")) == "pick DD-SECOND\n"
    assert md(inline_doc(ffdata(2, 25, 0, ("ONLY",)), "pick \x13 FORMDROPDOWN \x01\x15\r")) == "pick ONLY\n"
    assert md(inline_doc(ffdata(2, 7, 0, ("ONLY",)), "pick \x13 FORMDROPDOWN \x01\x15\r")) == "pick\n"
    assert md(inline_doc(ffdata(0, 0, text="x"), "\x13 FORMTEXT \x01\x14typed\x15\r")) == "typed\n"
    for cut in (3, 40, 75, 90):
        assert md(inline_doc(dd[:cut], "pick \x13 FORMDROPDOWN \x01\x15\r")) == "pick\n", cut


def test_duplicates_parsed_once_copies_counted():
    p = float_doc("\x08\x08\r", dgg_with_bstore([blip_png(PNG)], shapes=[(1025, 1), (1026, 1)]))
    out = tmp()
    assert md(p, out=out) == "![](assets/img-001.png)\n\n![](assets/img-001.png)\n"
    assert sorted(x.name for x in (out / "assets").iterdir()) == ["img-001.png"]
    # the image byte budget counts every copy
    old = doc.IMAGE_BYTES
    doc.IMAGE_BYTES = len(PNG) + 1
    try:
        assert md(p) == "(could not read: 1 image)\n\n![](assets/img-001.png)\n"
    finally:
        doc.IMAGE_BYTES = old
    # a deflated BLIP placed twice is inflated once, and every BLIP of the document shares one Budget
    seen = []
    real = flate.inflate

    def spy(data, budget):
        seen.append(budget)
        return real(data, budget)
    flate.inflate = spy
    try:
        emfs = [blip_metafile(0xF01A, 0x3D4, EMF), blip_metafile(0xF01A, 0x3D4, EMF + b"x" * 8)]
        dgg = dgg_with_bstore(emfs, shapes=[(1025, 1), (1026, 2), (1027, 1)])
        got = md(float_doc("\x08\x08\x08\r", dgg))
        assert got == "Not viewable here: assets/img-001.emf, assets/img-002.emf\n", got
    finally:
        flate.inflate = real
    assert len(seen) == 2 and seen[0] is seen[1] and seen[0].used == 2 * len(EMF) + 8, seen


# ---- hostile input
def test_officeart_records_bounded():
    a = oa_record(0xF00A, b"12345678")
    # a body past the end stops the walk
    assert list(officeart.records(a + struct.pack("<HHI", 0, 0xF00B, 100) + bytes(10))) == [(0, 0, 0xF00A, 8, 8)]
    assert list(officeart.records(a, 0, 12)) == []
    assert list(officeart.records(a[:7])) == []
    # zero-length records move on 8 bytes each; a walk is capped at MAX_RECORDS
    assert len(list(officeart.records(bytes(8 * 1000)))) == 1000
    old = officeart.MAX_RECORDS
    officeart.MAX_RECORDS = 10
    try:
        assert len(list(officeart.records(bytes(8 * 1000)))) == 10
    finally:
        officeart.MAX_RECORDS = old
    # ver and inst
    assert list(officeart.records(oa_record(0xF007, b"", ver=2, inst=0x123))) == [(2, 0x123, 0xF007, 8, 0)]
    # the BStore's file blocks and their offsets; a block that is a BLIP itself reads as one
    dgg = dgg_with_bstore([blip_png(PNG), 7])
    store = officeart.bstore(dgg)
    assert len(store) == 2 and all(struct.unpack_from("<H", dgg, o + 2)[0] == 0xF007 for o in store), store
    assert officeart.bstore(b"") == [] and officeart.bstore(oa_record(0xF00B, bytes(6))) == []
    b = flate.Budget()
    assert officeart.fbse_blip(dgg, store[0], None, b) == PNG
    assert officeart.fbse_blip(dgg, store[1], None, b) is None
    assert officeart.fbse_blip(blip_png(PNG), 0, None, b) == PNG
    # foDelay is checked against the delay stream; cbName against the FBSE
    assert officeart.fbse_blip(oa_fbse(delay=4), 0, blip_png(PNG), b) is None
    assert officeart.fbse_blip(oa_fbse(delay=0), 0, blip_png(PNG), b) == PNG
    assert officeart.fbse_blip(oa_fbse(blip_png(PNG), name=b"\0" * 200)[:60], 0, None, b) is None
    named = oa_fbse(blip_png(PNG))
    assert officeart.fbse_blip(named[:41] + b"\xff" + named[42:], 0, None, b) is None
    # a BLIP with an instance not of its kind, cut short, or past its record
    assert officeart.blip(blip_bitmap(0xF01E, 0x46A, PNG), 0, b) is None
    assert officeart.blip(blip_png(PNG)[:30], 0, b) is None
    assert officeart.blip(blip_png(PNG), 1, b) is None and officeart.blip(blip_png(PNG), -1, b) is None
    # shape_pib: property 0x4104 of the shape's FOPT; none without one, or when the FOPT's count passes its body
    shape = oa_shape(1025, 3)
    assert officeart.shape_pib(shape, 0) == 3 and officeart.shape_pib(oa_shape(1025), 0) is None
    cut = oa_container(0xF004, oa_record(0xF00B, struct.pack("<HI", 0x0080, 1), ver=3, inst=900))
    assert officeart.shape_pib(cut, 0) is None and officeart.shape_pib(shape, 3) is None


def test_hostile_picf_counted():
    picf = picf_inline(blip_png(PNG))
    lcb_past = struct.pack("<I", 1 << 30) + picf[4:]
    header_past = picf[:4] + struct.pack("<H", 0xFFFF) + picf[6:]
    name_past = picf_inline(blip_png(PNG), 0x66, name=b"x")
    name_past = name_past[:68] + b"\xff" + name_past[69:]
    for bad in (lcb_past, header_past, name_past, b"\x01\x02"):
        got = md(inline_doc(bad, "a\x01\r"))
        assert got == "(could not read: 1 image)\n\na\n", (bad[:8], got)
    # a PicLocation past the Data stream
    p = inline_doc(picf, "a\x01\r", chpx=[(1, 2, sp(SPEC) + sp(PIC, 1 << 31))])
    assert md(p) == "(could not read: 1 image)\n\na\n"
    # bytes no reader knows are a lost picture, not an empty file
    assert md(inline_doc(picf_inline(blip_png(b"not a picture at all")), "a\x01\r")) == "(could not read: 1 image)\n\na\n"


def test_drawing_damage_counted():
    # a DgContainer cut short: its shapes past the cut are lost, counted once under shape
    dgg = dgg_with_bstore([blip_png(PNG)])
    got = md(float_doc("a\x08\r", dgg[:-4]))
    assert got == "(could not read: 1 shape)\n\na\n", got
    # a PlcSpaMom that is not a whole PLC loses the anchors, counted once
    got = md(float_doc("a\x08\r", dgg, anchors=[], fc={40: bytes(7)}))
    assert got == "(could not read: 1 shape)\n\na\n", got


def test_shape_nesting_depth():
    shape = oa_shape(1025, 1)
    for _ in range(limits.MAX_DEPTH + 5):
        shape = oa_container(0xF003, shape)
    dgg = dgg_with_bstore([blip_png(PNG)], shapes=[]) + bytes([0]) + oa_container(0xF002, shape)
    p = float_doc("a\x08\r", dgg)
    d, ctx = parse(p)
    # the shape past MAX_DEPTH is counted under depth once, not again as an anchor whose shape is not found
    assert d.blocks == [Para([Text("a")])] and ctx.skipped == {"depth": 1}, ctx.skipped


def test_shapes_index_groups():
    """officeart.Shapes: spid -> SpContainer, the pib of each shape with one, the pictures (pibs) in record order and,
    for each group shape (the first SpContainer of an SpgrContainer), its members' slice of them, nested groups'
    members included."""
    patriarch = oa_container(0xF004, oa_record(0xF00A, struct.pack("<II", 1024, 5), ver=2))
    data = oa_container(0xF003, patriarch, oa_shape(1025, 2), oa_group(1026, oa_shape(1027, 1), oa_shape(1028)),
                        oa_shape(1029, 3))
    sh = officeart.Shapes()
    sh.add(data, 0, len(data))
    assert sorted(sh.at) == list(range(1024, 1030)) and sh.pics == [2, 1, 3], (sh.at, sh.pics)
    assert sh.pib == {sh.at[1025]: 2, sh.at[1027]: 1, sh.at[1029]: 3}, sh.pib
    assert sh.groups == {sh.at[1024]: (0, 3), sh.at[1026]: (1, 2)}, sh.groups


def test_delay_blips_read_once_within_budget():
    """FBSEs that delay-load one BLIP read it once and hold one copy (the read is memoized by stream and offset),
    and a document's delay BLIPs read at most len(WordDocument) bytes in all, so overlapping ones past that are
    counted under image (the review's probe: 96 FBSEs on one 4 MiB BLIP held 384 MiB)."""
    n, big = 64, b"\x89PNG\r\n\x1a\n" + bytes(1 << 20)
    reads, real = [], officeart.blip

    def spy(data, off, budget):
        h = officeart.header(data, off)
        reads.append(8 + h[3] if h else 0)
        return real(data, off, budget)

    def run(p):
        reads.clear()
        officeart.blip = spy
        try:
            return parse(p)
        finally:
            officeart.blip = real
    d, ctx = run(float_doc("\x08" * n + "\r", dgg_with_bstore([DOC_EXTRA] * n), chpx=[(0, n, sp(SPEC))],
                           word_extra=blip_png(big)))
    assert len(reads) == 1 and not ctx.skipped, (len(reads), ctx.skipped)
    assert len(d.blocks) == n and len({id(b.data) for b in d.blocks}) == 1
    # n offsets whose BLIP records overlap, each running to the end of one shared BLIP
    inner = blip_png(big)
    heads = b"".join(struct.pack("<HHI", 0x6E0 << 4, 0xF01E, 8 * (n - 1 - j) + len(inner)) for j in range(n))
    p = float_doc("\x08" * n + "\r", dgg_with_bstore([DOC_EXTRA + 8 * j for j in range(n)]),
                  chpx=[(0, n, sp(SPEC))], word_extra=heads + inner)
    with Compound(p) as cf:
        word = len(cf.read("WordDocument"))
    t0 = time.monotonic()
    d, ctx = run(p)
    assert sum(reads) <= word and ctx.skipped == {"image": n}, (len(reads), sum(reads), word, ctx.skipped)
    assert time.monotonic() - t0 < 10


def test_overlapping_picfs_read_within_budget():
    """Distinct PicLocations whose PICFs overlap one region read at most len(Data) bytes of PICFs in all, the rest
    counted under image (the review's probe: 400 PICFs over 64K zero-length records took 5 s, growing as n^2)."""
    n, zone = 200, 8 * 1024
    z = 8 * n  # the zone starts after n 8-byte PICF heads, each PICF running from its head to the zone's end
    data = b"".join(struct.pack("<IHH", z + 8 * zone - 8 * j, z - 8 * j, 0x64) for j in range(n)) + bytes(8 * zone)
    p = make_doc(tmp() / "a.doc", [("\x01" * n + "\r", False)], streams={"Data": data},
                 chpx=[(j, j + 1, sp(SPEC) + sp(PIC, 8 * j)) for j in range(n)])
    walked, real = [0], officeart.records

    def spy(*args):
        for rec in real(*args):
            walked[0] += 1
            yield rec
    officeart.records = spy
    t0 = time.monotonic()
    try:
        d, ctx = parse(p)
    finally:
        officeart.records = real
    assert ctx.skipped == {"image": n} and walked[0] <= len(data) // 8, (walked, ctx.skipped)
    assert time.monotonic() - t0 < 10


def test_many_shapes_linear():
    n = 20000
    dgg = dgg_with_bstore([blip_png(PNG)], shapes=[(1025 + k, 1) for k in range(n)])
    p = float_doc("\x08" * n + "\r", dgg, chpx=[(0, n, sp(SPEC))])
    t0 = time.monotonic()
    d, ctx = parse(p)
    assert len(d.blocks) == n and all(b == Image(PNG, codepage="cp1252") for b in d.blocks) and not ctx.skipped
    assert time.monotonic() - t0 < 10


def test_picture_codepage_is_the_document_code_page():
    # lid 0x0412 is Korean (double byte): compressed text is cp1252 but the picture's code page is cp949
    p = inline_doc(picf_inline(blip_png(PNG)), "a\x01\r", lid=0x0412)
    d, ctx = parse(p)
    assert [b.codepage for b in d.blocks if isinstance(b, Image)] == ["cp949"], d.blocks
    d, ctx = parse(inline_doc(picf_inline(blip_png(PNG)), "a\x01\r", lid=0x0409))
    assert [b.codepage for b in d.blocks if isinstance(b, Image)] == ["cp1252"]

def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
