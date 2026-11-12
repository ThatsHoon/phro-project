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
from reader.formats import doc  # noqa: E402
from reader.model import Cell, Context, Image, Para, Table, Text  # noqa: E402
from reader_fix import (DOC_EXTRA, blip_bitmap, blip_metafile, blip_png, dgg_with_bstore, doc_spa,  # noqa: E402
                        doc_stories, make_doc, oa_container, oa_fbse, oa_record, oa_shape, picf_inline)

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
    assert d.blocks == [Para([Text("a")]), Image(PNG, hidden="hidden")] and not ctx.skipped, (d.blocks, ctx.skipped)
    d, ctx = parse(inline_doc(picf_inline(blip_png(PNG)), "a\x01\r", props=sp(DELETED)))
    assert d.blocks == [Para([Text("a")]), Image(PNG, hidden="deleted")] and not ctx.skipped, (d.blocks, ctx.skipped)
    d, ctx = parse(float_doc("a\x08\r", dgg_with_bstore([blip_png(PNG)]), props=sp(VANISH)))
    assert d.blocks == [Para([Text("a")]), Image(PNG, hidden="hidden")] and not ctx.skipped, (d.blocks, ctx.skipped)


# ---- where pictures go: after their paragraph, in its cell or note
def test_picture_in_table_cell():
    cell, row = u16(0) + sp(IN_TABLE), u16(0) + sp(IN_TABLE) + sp(TTP)
    p = inline_doc(picf_inline(blip_png(PNG)), "x\x01\x07z\x07\x07y\r", papx=[(0, 5, cell), (5, 6, row)])
    d, ctx = parse(p)
    want = [Table([[Cell([Para([Text("x")]), Image(PNG)]), Cell([Para([Text("z")])])]]), Para([Text("y")])]
    assert d.blocks == want and not ctx.skipped, d.blocks
    assert md(p).split("\n")[0] == "|x<br>![](assets/img-001.png)|z|", md(p)


def test_picture_in_note():
    s = doc_stories("a\x02\r", footnotes=[(1, "\x02 \x01\r")])
    pic = s["ccp"]["text"] + 2  # the footnote's \x01 in the whole text
    p = inline_doc(picf_inline(blip_png(PNG)), s["pieces"][0][0], ccp=s["ccp"], fc=s["fc"],
                   chpx=[(pic, pic + 1, sp(SPEC) + sp(PIC, AT))])
    d, ctx = parse(p)
    assert d.notes == {"fn-1": [Para([Text(" ")]), Image(PNG)]} and not ctx.skipped, (d.notes, ctx.skipped)
    assert md(p) == "a[^1]\n\n[^1]: ![](assets/img-001.png)\n", md(p)


# ---- what is and is not a lost picture
def test_textbox_anchor_not_counted():
    p = float_doc("a\x08\r", dgg_with_bstore([], shapes=[(1025, None)]))
    d, ctx = parse(p)
    assert md(p) == "a\n" and not ctx.skipped, ctx.skipped
    # a \x08 with no FSPA at its CP, a shape no drawing holds, a \x08 without fSpec: no picture, no loss
    for anchors, props in (([(5, 1025)], b""), ([(1, 4000)], b""), (None, sp(SPEC, 0))):
        p = float_doc("a\x08\r", dgg_with_bstore([blip_png(PNG)]), anchors=anchors, props=props)
        d, ctx = parse(p)
        assert md(p) == "a\n" and not ctx.skipped, (anchors, ctx.skipped)


def test_pib_out_of_range_counted():
    for shapes in ([(1025, 5)], [(1025, 0xFFFFFFFF)]):
        got = md(float_doc("a\x08\r", dgg_with_bstore([blip_png(PNG)], shapes=shapes)))
        assert got == "(could not read: 1 image)\n\na\n", (shapes, got)
    # an empty BStore slot (cRef 0) and a BLIP that is not there (no DggInfo BStore entry, foDelay past WordDocument)
    empty = dgg_with_bstore([blip_png(PNG)])
    empty = empty.replace(oa_fbse(blip_png(PNG)), oa_fbse(blip_png(PNG), cref=0))
    for dgg in (empty, dgg_with_bstore([0x7FFFFFF0])):
        assert md(float_doc("a\x08\r", dgg)) == "(could not read: 1 image)\n\na\n"


def test_header_anchor_relative_to_header_story():
    """PlcSpaHdr CPs count from the header story's start (MS-DOC FibRgFcLcb97.fcPlcSpaHdr). The header line is
    text, so a header picture that reads is not output (as in the docx reader); one whose pib fails is counted."""
    s = doc_stories("m\r", headers=[""] * 7 + ["h\x08"])
    pic = s["ccp"]["text"] + 1  # the header's \x08, at CP 1 of the header story
    dgg = dgg_with_bstore([blip_png(PNG)], shapes=[], header_shapes=[(1025, 5)])

    def run(cp):
        fc = {**s["fc"], 41: doc_spa([(cp, 1025)]), 50: dgg}
        p = make_doc(tmp() / "a.doc", s["pieces"], ccp=s["ccp"], fc=fc, chpx=[(pic, pic + 1, sp(SPEC))])
        return md(p)
    assert run(1) == "(could not read: 1 image)\n\nHeader: h\n\nm\n", run(1)
    assert run(pic) == "Header: h\n\nm\n", run(pic)  # an absolute CP is not the header's anchor


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
    (fOle2) is Task 14's. Neither is a lost picture."""
    p = inline_doc(b"", "\x13FORMTEXT \x01\x14x\x15\r", data=False)
    d, ctx = parse(p)
    assert md(p) == "x\n" and not ctx.skipped, ctx.skipped
    d, ctx = parse(inline_doc(b"", "a\x01\r", props=sp(OLE), data=False))
    assert d.blocks == [Para([Text("a")])] and not ctx.skipped, ctx.skipped


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
    assert d.blocks == [Para([Text("a")])] and ctx.skipped["depth"] == 1 and not ctx.skipped["image"], ctx.skipped


def test_many_shapes_linear():
    n = 20000
    dgg = dgg_with_bstore([blip_png(PNG)], shapes=[(1025 + k, 1) for k in range(n)])
    p = float_doc("\x08" * n + "\r", dgg, chpx=[(0, n, sp(SPEC))])
    t0 = time.monotonic()
    d, ctx = parse(p)
    assert len(d.blocks) == n and all(b == Image(PNG) for b in d.blocks) and not ctx.skipped
    assert time.monotonic() - t0 < 10


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
