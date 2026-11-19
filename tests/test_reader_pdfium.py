"""pdf through the pdfium front end: each test runs tests/pdfium_run.py under the pdf venv's
interpreter with the pdf on stdin. The venv is pyenv.venv_dir() (PHRO_READER_PYENV first); the
tests run when its interpreter exists, else they print "skip: no pdf venv" and pass.

Run: python -I -X utf8 tests/test_reader_pdfium.py
"""
import json
import os
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from pdf_fix import (JPEG, EncryptSpec, Name, Ref, Stream, acroform, font_type0, image_xobject,  # noqa: E402
                     link_annot, ocg_catalog, outline_items, rule_ops, simple_pdf, text_annot, text_ops, widget)

from reader import cache, images, pyenv  # noqa: E402

RUN = HERE / "tests" / "pdfium_run.py"
F1 = {"Font": {"F1": Ref(3)}}
FULL = b"q 612 0 0 792 0 0 cm /Im1 Do Q "


def _interpreter():
    if pyenv.forced_native():
        return None
    py = pyenv.interpreter(pyenv.venv_dir())
    return py if py.is_file() else None


PY = _interpreter()


def run(data, **opts):
    """The child's JSON line for the pdf `data`, with its stderr as "_stderr"; opts are patch, pages,
    hidden, engine (pdfium) and native_fails."""
    arg = json.dumps({"patch": opts.get("patch", {}), "pages": opts.get("pages"),
                      "hidden": bool(opts.get("hidden")), "engine": opts.get("engine"),
                      "native_fails": bool(opts.get("native_fails")), "count": opts.get("count")})
    r = subprocess.run([str(PY), "-I", "-X", "utf8", str(RUN), arg], input=data,
                       capture_output=True, timeout=300)
    err = r.stderr.decode("utf-8", "replace")
    assert r.returncode == 0, err
    lines = r.stdout.decode("utf-8").strip().splitlines()
    assert lines, err
    got = json.loads(lines[-1])
    got["_stderr"] = err
    return got


def with_image(content, image, **kw):
    res = dict(F1, XObject={"Im1": Ref(30)})
    return simple_pdf([content], resources=res, extra_objects={30: image}, **kw)


def test_pdfium_text():
    got = run(simple_pdf([text_ops("Hello world")]))
    md = got["md"]
    assert "## Page 1" in md and md.index("## Page 1") < md.index("Hello world"), md
    assert got["meta"]["engine"] == "pdfium", got["meta"]
    assert not got["skipped"], got["skipped"]


def test_pdfium_surrogates():
    # a simple font whose ToUnicode maps code 0x41 to U+1F600 (a surrogate pair in UTF-16)
    cmap = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
            b"1 begincodespacerange <00> <FF> endcodespacerange\n"
            b"1 beginbfchar\n<41> <D83DDE00>\nendbfchar\nendcmap end end\n")
    font = {"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name("Helvetica"),
            "ToUnicode": Ref(60)}
    data = simple_pdf([text_ops("xAx")], resources={"Font": {"F1": Ref(61)}},
                      extra_objects={60: Stream({}, cmap), 61: font})
    md = run(data)["md"]
    assert md.count("\U0001F600") == 1 and "�" not in md, md
    assert "x\U0001F600x" in md, md


def test_pdfium_spacing():
    # one glyph per TJ string, 0.5 pt between glyphs and 4.0 pt between the words (12 pt font)
    small, big = b" -41.667 ", b" -333.333 "
    tj = (b"BT /F1 12 Tf 72 700 Td [(W)" + small + b"(O)" + small + b"(R)" + small + b"(D)" + big
          + b"(O)" + small + b"(N)" + small + b"(E)] TJ ET")
    md = run(simple_pdf([tj]))["md"]
    assert "WORD ONE" in md, md


def _grid_ops():
    ops = [rule_ops(100, y, 300, y) for y in (500, 530, 560)]
    ops += [rule_ops(x, 500, x, 560) for x in (100, 200, 300)]
    return b" ".join(ops)


def test_pdfium_table():
    cells = [("Alpha", 110, 540), ("Beta", 210, 540), ("Gamma", 110, 510), ("Delta", 210, 510)]
    words = b" ".join(text_ops(t, x=x, y=y, size=10) for t, x, y in cells)
    page = text_ops("Before", y=700) + b" " + _grid_ops() + b" " + words + b" " + text_ops("After", y=400)
    got = run(simple_pdf([page]))
    md = got["md"]
    assert "|Alpha|Beta|" in md and "|Gamma|Delta|" in md, md
    assert md.count("Alpha") == 1 and md.index("Before") < md.index("|Alpha") < md.index("After"), md
    assert not got["skipped"], got["skipped"]


def test_pdfium_form_rules():
    # the grid is drawn by a form XObject moved 50 right by its /Matrix and 20 up by the cm
    # before its Do: pdfium's walk reaches its rules (native leaves rules inside forms out), and
    # each point goes through both matrices
    grid = [rule_ops(50, y, 250, y) for y in (480, 510, 540)] + [rule_ops(x, 480, x, 540) for x in (50, 150, 250)]
    form = Stream({"Type": Name("XObject"), "Subtype": Name("Form"), "BBox": [0, 0, 612, 792],
                   "Matrix": [1, 0, 0, 1, 50, 0]}, b" ".join(grid))
    cells = [("Alpha", 110, 540), ("Beta", 210, 540), ("Gamma", 110, 510), ("Delta", 210, 510)]
    words = b" ".join(text_ops(t, x=x, y=y, size=10) for t, x, y in cells)
    page = b"q 1 0 0 1 0 20 cm /Fm Do Q " + words
    got = run(simple_pdf([page], resources=dict(F1, XObject={"Fm": Ref(30)}), extra_objects={30: form}))
    assert "|Alpha|Beta|" in got["md"] and "|Gamma|Delta|" in got["md"], got["md"]


def test_pdfium_bitmap_png():
    # decision 15: a Flate CMYK image, which native cannot make viewable and pdfium can
    samples = bytes((i * 7) % 256 for i in range(64 * 64 * 4))
    content = text_ops("Words", y=750) + b" q 100 0 0 100 72 500 cm /Im1 Do Q"
    got = run(with_image(content, image_xobject(64, 64, cs="DeviceCMYK", data=samples)))
    assert [a[0][-4:] for a in got["assets"]] == [".png"], got["assets"]
    assert got["assets"][0][2:] == [64, 64], got["assets"]
    assert "](assets/img-001.png)" in got["md"] and not got["skipped"], got


def test_pdfium_dct_kept():
    # DCT data is written as it is (the dictionary's 64 x 64 passes the size floor)
    content = text_ops("Words", y=750) + b" q 100 0 0 100 72 500 cm /Im1 Do Q"
    got = run(with_image(content, image_xobject(64, 64, data=JPEG, filter="DCTDecode")))
    assert [a[:2] for a in got["assets"]] == [["img-001.jpg", len(JPEG)]], got["assets"]
    assert not got["skipped"], got["skipped"]


def test_pdfium_scan_rendered():
    got = run(with_image(FULL, image_xobject(64, 64, data=bytes(range(64)) * 64)))
    assert "![Scanned page 1](" in got["md"], got["md"]
    assert len(got["assets"]) == 1 and got["assets"][0][2] == 935, got["assets"]
    assert got["meta"]["unrendered"] == [] and not got["skipped"], got


def _figure_body():
    # 40 filled 5 x 5 squares spread over a 230 x 275 box: about 13% of the page
    return b"".join(b"%d %d 5 5 re f " % (100 + 45 * (i % 6), 100 + 45 * (i // 6)) for i in range(40))


def test_pdfium_figure_crop():
    got = run(simple_pdf([text_ops("Caption", y=720) + b" " + _figure_body()]))
    assert got["meta"]["figures"] == [1] and got["meta"]["unrendered"] == [], got["meta"]
    assert got["md"].count("](assets/") == 1 and len(got["assets"]) == 1, got
    assert "Caption" in got["md"] and not got["skipped"], got


def test_pdfium_renders_cap():
    image = image_xobject(64, 64, data=bytes(range(64)) * 64)
    # a small square at a different place on each page keeps the renders distinct
    pages = [FULL + b"%d 100 40 40 re f" % (100 * n) for n in range(1, 5)]
    data = simple_pdf(pages, resources=dict(F1, XObject={"Im1": Ref(30)}), extra_objects={30: image})
    got = run(data, patch={"PDF_RENDERS": 2})
    assert got["md"].count("![Scanned page") == 2 and len(got["assets"]) == 2, got
    assert got["meta"]["unrendered"] == [3, 4] and got["skipped"] == {"scan": 2}, got


def test_pdfium_pixel_cap():
    # 5000 x 5000 is past images._MAX_PIXELS: the image's box is rendered at CROP_DPI instead
    content = text_ops("Words", y=750) + b" q 400 0 0 400 72 200 cm /Im1 Do Q"
    got = run(with_image(content, image_xobject(5000, 5000)))
    assert len(got["assets"]) == 1, got
    _name, _size, w, h = got["assets"][0]
    assert max(w, h) <= 1568 and w != 5000, got["assets"]
    assert abs(w - 400 * 150 / 72) <= 2 and abs(h - 400 * 150 / 72) <= 2, got["assets"]
    assert not got["skipped"], got["skipped"]


def test_pdfium_image_cap():
    res = dict(F1, XObject={f"Im{i}": Ref(30 + i) for i in range(3)})
    body = text_ops("Words", y=750) + b"".join(
        b" q 100 0 0 100 %d 400 cm /Im%d Do Q" % (72 + 150 * i, i) for i in range(3))
    images = {30 + i: image_xobject(64, 64, data=bytes([40 * (i + 1)]) * 4096) for i in range(3)}
    got = run(simple_pdf([body], resources=res, extra_objects=images), patch={"PDF_PAGE_IMAGES": 2})
    assert len(got["assets"]) == 2, got["assets"]
    assert got["skipped"] == {"image": 1}, got["skipped"]


def _fields_pdf():
    ap = Stream({"Type": Name("XObject"), "Subtype": Name("Form"), "BBox": [0, 0, 10, 10]}, b"")
    fields = acroform([
        widget((72, 600, 200, 620), Ref(4), T="full_name", TU="Name", FT=Name("Tx"), V="Kim"),
        widget((72, 560, 90, 578), Ref(4), T="Agree", FT=Name("Btn"), V=Name("Yes"), AS=Name("Yes"),
               AP={"N": {"Yes": Ref(150), "Off": Ref(150)}}),
        widget((72, 520, 200, 538), Ref(4), T="Empty", FT=Name("Tx")),
    ])
    objects = dict(fields)
    objects[150] = ap
    return simple_pdf([text_ops("Body text")], catalog={"AcroForm": Ref(200)}, extra_objects=objects,
                      page_extra={"Annots": [Ref(201), Ref(202), Ref(203)]})


def test_pdfium_fields():
    got = run(_fields_pdf())
    md = got["md"]
    assert "> Field Name: Kim" in md and "> Field Agree: Yes" in md, md
    assert "Empty" not in md and md.index("Body text") < md.index("> Field Name"), md
    assert not got["skipped"], got["skipped"]


def test_pdfium_field_without_widget():
    # a field with no widget is in /AcroForm /Fields only: native lists it on page 1, and so must pdfium
    objects = acroform([{"T": "Lonely", "FT": Name("Tx"), "V": "Solo"}])
    data = simple_pdf([text_ops("First page"), text_ops("Second page")], catalog={"AcroForm": Ref(200)},
                      extra_objects=objects)
    got, native = run(data), run(data, engine="native")
    assert native["md"].count("> Field Lonely: Solo") == 1, native["md"]
    assert got["md"] == native["md"], (got["md"], native["md"])
    md = got["md"]
    assert md.index("First page") < md.index("> Field Lonely") < md.index("## Page 2"), md
    assert got["meta"]["engine"] == "pdfium", got["meta"]


def _sizes_page():
    return b" ".join([text_ops("Line one", y=700), text_ops("Line two", y=680), text_ops("Line three", y=660),
                      text_ops("Small", y=600, size=0.5),
                      b"BT /F1 1 Tf 12 0 0 12 72 560 Tm (Matrix words) Tj ET",
                      text_ops("Narrow words", y=520, extra=b"50 Tz")])


def test_pdfium_font_size():
    # Fragment.size is the font size in device units, as native has it: 12 pt lines 20 pt apart are
    # three paragraphs, a 0.5 pt line is tiny, and a 1 Tf line under a 12 Tm (or a 50 Tz line) is not
    data = simple_pdf([_sizes_page()])
    got, native = run(data), run(data, engine="native")
    assert got["md"] == native["md"], (got["md"], native["md"])
    md = got["md"]
    assert md.splitlines()[0] == "(1 hidden places left out)", md
    paras = [p.strip() for p in md.split("\n\n") if p.strip()]
    for line in ("Line one", "Line two", "Line three", "Matrix words", "Narrow words"):
        assert line in paras, (line, paras)
    assert "Small" not in md and "(hidden) Small" in run(data, hidden=True)["md"], md


def test_pdfium_comments():
    objects = {100: text_annot((10, 10, 20, 20), "note", author="Ann"),
               101: text_annot((10, 30, 20, 40), "reply", author="Bob", irt=Ref(100))}
    data = simple_pdf([text_ops("Body text")], extra_objects=objects,
                      page_extra={"Annots": [Ref(101), Ref(100)]})
    md = run(data)["md"]
    assert "> Comment by Ann: note" in md and "> Comment by Bob: reply" in md, md
    assert md.index("Comment by Ann") < md.index("Comment by Bob"), md


def test_pdfium_link_schemes():
    objects = {100: link_annot((70, 715, 100, 735), "https://ex.org"),
               101: link_annot((70, 695, 100, 712), "javascript:alert(1)")}
    data = simple_pdf([text_ops("here") + b" " + text_ops("there", y=700)], extra_objects=objects,
                      page_extra={"Annots": [Ref(100), Ref(101)]})
    md = run(data)["md"]
    assert "[here](https://ex.org)" in md, md
    assert "there" in md and "[there]" not in md and "javascript" not in md, md


def _hidden_page():
    return b" ".join([text_ops("Visible", y=700), text_ops("Offcrop", x=700, y=900),
                      text_ops("Tiny", y=600, size=0.5), text_ops("Ghost", y=500, extra=b"3 Tr")])


def test_pdfium_hidden_rules():
    got = run(simple_pdf([_hidden_page()]))
    md = got["md"]
    assert md.splitlines()[0] == "(3 hidden places left out)", md
    assert "Visible" in md and not any(w in md for w in ("Offcrop", "Tiny", "Ghost")), md
    md = run(simple_pdf([_hidden_page()]), hidden=True)["md"]
    for word in ("Offcrop", "Tiny", "Ghost"):
        assert f"(hidden) {word}" in md, (word, md)


def test_pdfium_ocg_via_native():
    body = text_ops("Visible words", y=700) + b" /OC /L1 BDC " + text_ops("Secret", y=400) + b" EMC"
    data = simple_pdf([body], resources=dict(F1, Properties={"L1": Ref(50)}),
                      extra_objects={50: {"Type": Name("OCG"), "Name": "Layer"}},
                      catalog=ocg_catalog(off=[50]))
    got = run(data)
    md = got["md"]
    assert "Visible words" in md and "Secret" not in md, md
    assert md.splitlines()[0] == "(1 hidden places left out)", md
    assert got["meta"]["engine"] == "pdfium", got["meta"]
    assert "(hidden) Secret" in run(data, hidden=True)["md"]


def test_pdfium_encrypted():
    got = run(simple_pdf([text_ops("Locked words")], encrypt=EncryptSpec(6)))
    assert "Locked words" in got["md"] and got["meta"]["engine"] == "pdfium", got
    got = run(simple_pdf([text_ops("Locked words")], encrypt=EncryptSpec(6, user=b"secret")))
    assert got.get("error", [None])[0] == "encrypted", got


def test_pdfium_crypto_oracle():
    # pdfium decrypts pdf_fix's encrypted fixtures on its own: the independent check of the
    # fixtures the native decryptor is tested with
    specs = (EncryptSpec(2), EncryptSpec(3), EncryptSpec(4, aes=False), EncryptSpec(4),
             EncryptSpec(6), EncryptSpec(4, metadata=False))
    for spec in specs:
        got = run(simple_pdf([text_ops("Sealed text")], encrypt=spec))
        assert "Sealed text" in got.get("md", ""), (spec.revision, spec.aes, spec.metadata, got)
        assert got["meta"]["engine"] == "pdfium", got["meta"]


def test_pdfium_no_xref_falls_back():
    got = run(simple_pdf([text_ops("Scanned for")], xref="none"))
    assert "Scanned for" in got["md"] and got["meta"]["engine"] == "native", got


def test_pdfium_damaged_both():
    got = run(b"%PDF-1.7\nnot a pdf at all\n")
    assert got.get("error", [None])[0] == "damaged", got


def test_pdfium_outline_headings():
    data = simple_pdf([text_ops("Intro", y=720) + b" " + text_ops("Body line one", y=700)
                       + b" " + text_ops("Body line two", y=686)],
                      catalog={"Outlines": Ref(300)}, extra_objects=outline_items([(1, "Intro", 0)]))
    md = run(data)["md"]
    assert "### Intro" in md and md.count("Intro") == 1, md


def _turned(text, vx, vy, size=10):
    """Text that reads left to right once a /Rotate 90 page (612 wide) is turned."""
    return b"BT /F1 %d Tf 0 1 -1 0 %d %d Tm (%s) Tj ET" % (size, 612 - vy, vx, text.encode())


def test_pdfium_rotated_page():
    ops = [_turned("First body line", 72, 520), _turned("second body line", 72, 507),
           _turned("third body line", 72, 494)]
    got = run(simple_pdf([b" ".join(ops)], page_extra={"Rotate": 90}))
    assert "First body line second body line third body line" in got["md"], got["md"]
    assert not got["skipped"], got["skipped"]


def _both(data):
    """pdfium's markdown, with the native page as a backstop and from pdfium's geometry alone."""
    return run(data)["md"], run(data, native_fails=True)["md"]


def test_pdfium_baseline_out_of_order():
    row = [text_ops("9.50", 400, 700), text_ops("0001", 72, 700), text_ops("PLAN", 120, 700),
           text_ops("19.00", 480, 700), text_ops("LUMP", 300, 700)]
    gaps = [text_ops("and", 121.668, 650), b"BT /F1 12 Tf 72 650 Td [(IMPORT) -2250 (TABLE)] TJ ET"]
    twice = [text_ops("Annual", 72, 600), text_ops("Report", 120, 600),
             text_ops("Annual", 72.4, 600), text_ops("Report", 120.4, 600)]
    # (boxes side by side are not tried: pdfium hands their chars over left box first)
    for md in _both(simple_pdf([b" ".join(row + gaps + twice)])):
        assert "0001 PLAN LUMP 9.50 19.00" in md and "IMPORT and TABLE" in md, md
        assert "AAnn" not in md and md.count("Annual") == 1 and "Annual Report" in md, md


def test_pdfium_sideways_page():
    for rotate in (90, 180, 270):
        ops = [text_ops("Apache", 72, 700), text_ops("Tika", 116, 700), text_ops("toolkit", 72, 686)]
        for md in _both(simple_pdf([b" ".join(ops)], page_extra={"Rotate": rotate})):
            assert "Apache Tika toolkit" in md, (rotate, md)
    # Identity-V columns with Latin set clockwise inside them keep the page as it is
    chars = "縦書日本語文章"
    codes = {c: 0x100 + i for i, c in enumerate(chars)}

    def column(text, x):
        return b"BT /F2 12 Tf %d 700 Td <%s> Tj ET" % (x, "".join("%04X" % codes[c] for c in text).encode())

    latin = b"BT /F1 10 Tf 0 -1 1 0 %d %d Tm (%s) Tj ET"
    body = [column(chars[:4], 500), latin % (496, 640, b"Unicode"), column(chars[4:], 480),
            latin % (476, 650, b"OpenType")]
    data = simple_pdf([b" ".join(body)], resources=dict(F1, Font={"F1": Ref(3), "F2": Ref(100)}),
                      extra_objects=font_type0(tounicode={code: c for c, code in codes.items()}, vertical=True))
    for md in _both(data):
        assert chars[:4] in md and chars[4:] in md and md.index(chars[:4]) < md.index(chars[4:]), md


def test_pdfium_ligature_stays_in_word():
    # code A is one glyph whose ToUnicode is "fi": pdfium gives both chars the glyph's origin and
    # box, and the word must not break after the f
    cmap = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
            b"1 begincodespacerange <00> <FF> endcodespacerange\n"
            b"1 beginbfchar\n<41> <00660069>\nendbfchar\nendcmap end end\n")
    font = {"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name("Helvetica"),
            "Encoding": Name("WinAnsiEncoding"), "ToUnicode": Ref(60)}
    ops = [text_ops("efAcient viewing", 72, 700), text_ops("Multiple Ales are bound", 72, 680, size=20)]
    data = simple_pdf([b" ".join(ops)], resources={"Font": {"F1": Ref(61)}},
                      extra_objects={60: Stream({}, cmap), 61: font})
    for md in _both(data):
        assert "efficient viewing" in md and "Multiple files are bound" in md, md


def test_pdfium_overhang_keeps_space():
    # an f whose advance (/Widths 250) is narrower than its ink: the word after it starts 0.25 em
    # past the advance, a word space, though under 0.2 em past the ink
    narrow = {"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name("Helvetica"),
              "Encoding": Name("WinAnsiEncoding"), "FirstChar": 102, "LastChar": 102, "Widths": [250]}
    o_end = 72 + 0.556 * 12
    ops = [text_ops("o", 72, 700), b"BT /F4 12 Tf %.3f 700 Td (f) Tj ET" % o_end,
           text_ops("Class", o_end + 3.0 + 3.0, 700)]
    data = simple_pdf([b" ".join(ops)], resources={"Font": {"F1": Ref(3), "F4": Ref(60)}},
                      extra_objects={60: narrow})
    for md in _both(data):
        assert "of Class" in md, md


def test_pdfium_angled_text():
    # words set at 19 degrees and kerned tight, Tf 1 and the size in the text matrix, as a poster's
    # banner is: each word reads whole, not glyph by glyph
    tm = b"BT /F1 1 Tf 12.292 4.232 -4.232 12.292 %d %d Tm [%s] TJ ET"
    ops = [tm % (40, 440, b"(M) 50 (O) 50 (S) 200 (T)"),
           tm % (44, 426, b"(IN) 80 (-) 80 (DE) 100 (MAND)"),
           tm % (48, 412, b"(E) 60 (N) 60 (G) 60 (I) 60 (N) 60 (E) 60 (E) 60 (R) 60 (I) 60 (N) 60 (G)")]
    for md in _both(simple_pdf([b" ".join(ops)])):
        for word in ("MOST", "IN-DEMAND", "ENGINEERING"):
            assert word in md, (word, md)


def test_pdfium_advance_calls():
    # 30 lines of plain Helvetica with no glyph narrower than its loose box (pdfium's stand-in
    # font overhangs only a few, such as r): the tight box is not read, and the font's width is
    # asked once per char, font and size, not once per glyph
    lines = [text_ops("Quick lazy dogs vex a big town %d" % k, 72, 760 - 20 * k)
             for k in range(30)]
    got = run(simple_pdf([b" ".join(lines)]),
              count=["FPDFText_GetCharBox", "FPDFFont_GetGlyphWidth"])
    assert "Quick lazy dogs vex a big town 29" in got["md"], got["md"]
    distinct = len(set("Quick lazy dogs vex a big town 0123456789"))
    assert got["calls"]["FPDFText_GetCharBox"] <= 30, got["calls"]
    assert got["calls"]["FPDFFont_GetGlyphWidth"] <= distinct, got["calls"]


def test_pdfium_pages_range():
    got = run(simple_pdf([text_ops(f"P{n}") for n in range(1, 5)]), pages=[2, 3])
    md = got["md"]
    assert "P2" in md and "P3" in md and "P1" not in md and "P4" not in md, md
    assert got["meta"]["pages_total"] == 4, got["meta"]


def test_pdfium_imports_only():
    modules = run(simple_pdf([text_ops("A")]))["modules"]
    for name in ("PIL", "pdfplumber", "pdfminer"):
        assert not any(m == name or m.startswith(name + ".") for m in modules), name
    assert "pypdfium2" in modules


def test_pdfium_cli_engine():
    # read.py convert --engine pdfium under the venv: the cache key holds the pdfium engine id
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "clean.pdf"
        src.write_bytes(simple_pdf([text_ops("From the CLI")]))
        env = dict(os.environ, PHRO_READER_CACHE=str(Path(tmp) / "cache"))
        r = subprocess.run([str(PY), "-I", "-X", "utf8", str(HERE / "scripts" / "read.py"), "convert",
                            str(src), "--engine", "pdfium"], capture_output=True, env=env, timeout=300)
        out = r.stdout.decode("utf-8").strip()
        assert r.returncode == 0, (out, r.stderr.decode("utf-8", "replace"))
        d = Path(out).parent
        assert d.name == cache.key(src, run(simple_pdf([text_ops("id")]))["engine_id"]), d.name
        assert d.name != cache.key(src, "native"), d.name
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        assert meta["engine"] == "pdfium", meta
        assert "From the CLI" in (d / "out.md").read_text(encoding="utf-8")


def _vertical_pdf():
    chars = sorted(set(RIGHT_COLUMN + LEFT_COLUMN))
    codes = {ch: 0x100 + i for i, ch in enumerate(chars)}
    objs = font_type0(tounicode={codes[c]: c for c in chars}, vertical=True)

    def column(text, x):
        hexs = "".join("%04X" % codes[c] for c in text)
        return b"BT /F2 12 Tf %d 700 Td <%s> Tj ET" % (x, hexs.encode())

    return simple_pdf([column(RIGHT_COLUMN, 500) + b" " + column(LEFT_COLUMN, 480)],
                      resources=dict(F1, Font={"F1": Ref(3), "F2": Ref(100)}), extra_objects=objs)


RIGHT_COLUMN = "\ubcf4\uace0\uc11c\ub294\uc138\uc9c0\uc5ed\uc744"
LEFT_COLUMN = "\ub2e4\ub8e8\uc5c8\uace0\uac01\ud300\uc740"


def test_pdfium_vertical():
    # Identity-V columns: native's markdown, the right column first and no spaces, both with the native
    # page as a backstop and from pdfium's own geometry (native failed)
    data = _vertical_pdf()
    native = run(data, engine="native")["md"]
    assert RIGHT_COLUMN in native and native.index(RIGHT_COLUMN) < native.index(LEFT_COLUMN), native
    got = run(data)
    assert got["md"] == native, (got["md"], native)
    alone = run(data, native_fails=True)
    assert alone["md"] == native and alone["meta"]["engine"] == "pdfium", (alone["md"], native)


def _bomb_pdf(n, side=5000):
    objs, xo = {}, {}
    for i in range(n):
        packer = zlib.compressobj(9)
        row = bytes([i * 3 % 256]) * side
        body = b"".join(packer.compress(row) for _ in range(side)) + packer.flush()
        objs[30 + i] = Stream({"Type": Name("XObject"), "Subtype": Name("Image"), "Width": side, "Height": side,
                               "ColorSpace": Name("DeviceGray"), "BitsPerComponent": 8,
                               "Filter": Name("FlateDecode")}, body)
        xo[f"I{i}"] = Ref(30 + i)
    page = text_ops("Words", y=780) + b"".join(
        b" q 60 0 0 60 %d %d cm /I%d Do Q" % (10 + 70 * (i % 8), 10 + 70 * (i // 8), i) for i in range(n))
    return simple_pdf([page], resources=dict(F1, XObject=xo), extra_objects=objs)


def test_pdfium_decode_budget():
    # 64 distinct 5000 x 5000 images on one page: past PDF_PAGE_DECODE_PIXELS nothing more is decoded or
    # rendered, each such image counts `image`, and the page still reads
    got = run(_bomb_pdf(64))
    assert "Words" in got["md"] and not got["assets"], got
    assert got["skipped"].get("image", 0) >= 1, got["skipped"]
    assert got["decoded"] and got["decoded"][0] is not None, got["decoded"]
    assert got["decoded"][0] <= 4 * images._MAX_PIXELS, got["decoded"]


def test_pdfium_char_cap_once():
    lines = b" ".join(text_ops("abcdefghij", y=700 - 14 * i) for i in range(10))
    data = simple_pdf([lines])
    got = run(data, patch={"PDF_PAGE_CHARS": 10})
    native = run(data, patch={"PDF_PAGE_CHARS": 10}, engine="native")
    assert got["skipped"] == native["skipped"] == {"text": 1}, (got["skipped"], native["skipped"])


def test_pdfium_ocg_without_native():
    # native failed: a text object in /OC marked content is hidden "ocg" from its own content mark
    body = text_ops("Visible words", y=700) + b" /OC /L1 BDC " + text_ops("Secret", y=400) + b" EMC"
    data = simple_pdf([body], resources=dict(F1, Properties={"L1": Ref(50)}),
                      extra_objects={50: {"Type": Name("OCG"), "Name": "Layer"}}, catalog=ocg_catalog(off=[50]))
    got = run(data, native_fails=True)
    md = got["md"]
    assert "Visible words" in md and "Secret" not in md and got["meta"]["engine"] == "pdfium", got
    assert md.splitlines()[0] == "(1 hidden places left out)", md
    assert "(hidden) Secret" in run(data, hidden=True, native_fails=True)["md"]


def test_pdfium_xfa_quiet():
    objs = dict(acroform([widget((72, 600, 200, 620), Ref(4), T="a", FT=Name("Tx"), V="v")]))
    objs[200]["XFA"] = Ref(250)
    objs[250] = Stream({}, b"<xdp:xdp xmlns:xdp='http://ns.adobe.com/xdp/'><template/></xdp:xdp>")
    data = simple_pdf([text_ops("XFA host")], catalog={"AcroForm": Ref(200)}, extra_objects=objs,
                      page_extra={"Annots": [Ref(201)]})
    got = run(data)
    assert "XFA host" in got["md"], got
    assert "auto-v8" not in got["_stderr"] and "XFA support" not in got["_stderr"], got["_stderr"]


def test_pdfium_closed():
    assert run(simple_pdf([text_ops("Closed after")]))["closed"] == [True]


def test_pdfium_own_password():
    # native failed, so the user password is pdfium's own finding
    got = run(simple_pdf([text_ops("Locked words")], encrypt=EncryptSpec(6, user=b"secret")), native_fails=True)
    assert got.get("error") == ["encrypted", "user password"], got


def _hidden_form_pdf():
    """Fields whose one widget has /F 2 (Hidden) or 32 (NoView), a field with a hidden and a shown
    widget, a shown field, and a comment with /F 32."""
    fields = acroform([
        widget((72, 600, 200, 620), Ref(4), T="Hid", FT=Name("Tx"), V="hidden value", F=2),
        widget((72, 570, 200, 590), Ref(4), T="NoView", FT=Name("Tx"), V="noview value", F=32),
        {"T": "Two", "FT": Name("Tx"), "V": "two value",
         "Kids": [widget((72, 540, 200, 560), Ref(4), F=2), widget((72, 510, 200, 530), Ref(4))]},
        widget((72, 480, 200, 500), Ref(4), T="Seen", FT=Name("Tx"), V="seen value"),
    ])
    objects = dict(fields)
    objects[100] = text_annot((10, 10, 20, 20), "noview note", author="Ann", flags=32)
    annots = [Ref(100)] + [Ref(n) for n, obj in sorted(fields.items()) if "Rect" in obj]
    return simple_pdf([text_ops("Body text")], catalog={"AcroForm": Ref(200)}, extra_objects=objects,
                      page_extra={"Annots": annots})


HIDDEN_FORM_LINES = ("> (hidden) Field Hid: hidden value", "> (hidden) Field NoView: noview value",
                     "> (hidden) Comment by Ann: noview note")


def test_pdfium_hidden_fields():
    # native's fields and pdfium's own walk of the widgets (native failed) hide the same places
    data = _hidden_form_pdf()
    for opts in ({}, {"native_fails": True}):
        md = run(data, **opts)["md"]
        assert md.splitlines()[0] == "(3 hidden places left out)", (opts, md)
        assert "> Field Two: two value" in md and "> Field Seen: seen value" in md, (opts, md)
        shown = run(data, hidden=True, **opts)["md"].splitlines()
        assert all(line in shown for line in HIDDEN_FORM_LINES), (opts, shown)


def test_pdfium_widget_walk():
    # native failed: fields come from the page's widgets and match native's
    data = _fields_pdf()
    want = [ln for ln in run(data, engine="native")["md"].splitlines() if ln.startswith("> Field")]
    got = run(data, native_fails=True)
    assert want and [ln for ln in got["md"].splitlines() if ln.startswith("> Field")] == want, (got, want)


ARIAL = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "Fonts" / "arial.ttf"
NO_FONT_CODES = b"04101234 2F000C210D55"  # pdfium shows these CIDs as U+0410 U+1234 U+2F00 ...


def _identity_pdf(codes, font_file=None, tounicode=None):
    """An Identity-H Type0 font of Ordering (Identity) showing `codes`, with no ToUnicode or the
    `tounicode` given, then a line in the plain font; `font_file` (TrueType bytes) is embedded with
    CIDToGIDMap Identity."""
    objs = font_type0(tounicode=tounicode, ordering="Identity")
    if font_file is not None:
        objs[101]["FontDescriptor"] = Ref(102)
        objs[101]["CIDToGIDMap"] = Name("Identity")
        objs[102] = {"Type": Name("FontDescriptor"), "FontName": Name("F2"), "Flags": 4, "FontFile2": Ref(104),
                     "FontBBox": [0, -200, 1000, 800], "Ascent": 800, "Descent": -200, "CapHeight": 700,
                     "ItalicAngle": 0, "StemV": 80}
        objs[104] = Stream({}, font_file, flate=True)
    body = b"BT /F2 12 Tf 72 700 Td <" + codes + b"> Tj ET " + text_ops("plain words here", 72, 680)
    return simple_pdf([body], resources=dict(F1, Font={"F1": Ref(3), "F2": Ref(100)}), extra_objects=objs)


def _arial_pdf(text, tounicode=None):
    from reader.pdfnative import cid_unicode
    font = ARIAL.read_bytes()
    gid_of = {}
    for gid, ch in sorted(cid_unicode.reverse_truetype_cmap(font).items()):
        gid_of.setdefault(ch, gid)
    return _identity_pdf("".join("%04X" % gid_of[c] for c in text).encode(), font, tounicode)


def test_pdfium_identity_without_tounicode():
    # pdfium reads an Identity-H code with no ToUnicode as a code point: the page is native's (the
    # embedded cmap reversed, or the text dropped and counted `glyphs`); with native failed, those
    # chars are dropped and counted
    cases = [(_identity_pdf(NO_FONT_CODES), None, "\u0410\u1234")]
    if ARIAL.is_file():
        cases.append((_arial_pdf("HelloWorld"), "HelloWorld", "+HOOR"))
        # a ToUnicode that maps none of the codes (seen in the bench): the same as none
        cases.append((_arial_pdf("HelloWorld", tounicode={}), "HelloWorld", "+HOOR"))
    for data, want, garbage in cases:
        native = run(data, engine="native")
        assert "plain words here" in native["md"] and (want is None or want in native["md"]), native
        got = run(data)
        assert (got["md"], got["skipped"]) == (native["md"], native["skipped"]), (got["md"], native["md"])
        assert got["meta"]["engine"] == "pdfium", got["meta"]
        alone = run(data, native_fails=True)
        md = alone["md"]
        assert "plain words here" in md and garbage not in md, md
        assert alone["skipped"] == {"glyphs": 1}, alone["skipped"]


def test_pdfium_simple_font_code_stays():
    # a simple font's code with no glyph name (0x1F) is flagged too, but native reads that font
    # by its encoding: the page stays pdfium's (its "fi" glyph read as two letters, not native's U+FB01)
    font = {"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name("Helvetica"),
            "Encoding": {"Type": Name("Encoding"), "BaseEncoding": Name("WinAnsiEncoding"),
                         "Differences": [128, Name("fi")]}}
    data = simple_pdf([b"BT /F3 12 Tf 72 700 Td (\\200nal \\037 word) Tj ET " + text_ops("plain words here", 72, 680)],
                      resources=dict(F1, Font={"F1": Ref(3), "F3": Ref(100)}), extra_objects={100: font})
    assert "ﬁnal" in run(data, engine="native")["md"]
    got = run(data)
    assert "final" in got["md"] and not got["skipped"], (got["md"], got["skipped"])


def test_pdfium_unmapped_page_rendered():
    # its glyphs without Unicode outnumber its chars: pdfium renders the page, with the native page
    # as a backstop and from pdfium's own chars
    body = b" ".join(b"BT /F2 12 Tf 72 %d Td <0001000200030004> Tj ET" % (750 - 20 * i) for i in range(30))
    data = simple_pdf([body + b" " + text_ops("Title", y=100)], resources=dict(F1, Font={"F1": Ref(3), "F2": Ref(100)}),
                      extra_objects=font_type0(tounicode=None, ordering="Identity"))
    for got in (run(data), run(data, native_fails=True)):
        assert "Title" in got["md"] and "](assets/" in got["md"] and len(got["assets"]) == 1, got["md"]
        assert got["skipped"] == {"glyphs": 1} and got["meta"]["unrendered"] == [], (got["skipped"], got["meta"])


def test_pdfium_native_damaged():
    got = run(simple_pdf([text_ops("Only pdfium reads")]), native_fails=True)
    assert "Only pdfium reads" in got["md"] and got["meta"]["engine"] == "pdfium", got


# children run as `python -I -X utf8 -c CODE <scripts> ...`: scripts/ goes after site-packages (its queue.py must not
# shadow the stdlib's), the pdf on stdin, one JSON line out
_GRAY = r"""
import json, sys
sys.path.append(sys.argv[1])
from reader.formats import pdf_pdfium
from reader.pdfnative.document import NativeDoc
data = sys.stdin.buffer.read()
doc = pdf_pdfium.PdfiumDoc(data, NativeDoc(data))
try:
    pd = doc.page(0)
    out = [[type(g).__name__, g.w, g.h, min(g.data), max(g.data)]
           for g in (pd.render(None, 200, gray=True), pd.render(None, 200, gray=True, cap=1_000_000))]
finally:
    doc.close()
print(json.dumps(out))
"""

_SCAN = r"""
import json, struct, sys, tempfile
from pathlib import Path
sys.path.append(sys.argv[1])
from reader import markdown, ocr
from reader.formats import pdf
from reader.model import Context
data = sys.stdin.buffer.read()
ctx = Context()
with ocr.opened(None, ("win-ko", "test"), cmd=[sys.executable, "-I", "-X", "utf8", sys.argv[2]]) as s:
    sent, real = [], s.submit
    def submit(g, kind, dpi):
        sent.append([g.w, g.h, kind, dpi])
        return real(g, kind, dpi)
    s.submit = submit
    doc = pdf.parse_bytes(data, ctx, engine="pdfium")
with tempfile.TemporaryDirectory() as tmp:
    text = markdown.render(doc, Path(tmp) / "assets", False, ctx.skipped)
    folder = Path(tmp) / "assets"
    assets = [[p.name, *struct.unpack(">II", p.read_bytes()[16:24])] for p in sorted(folder.glob("*.png"))]
print(json.dumps({"md": text, "sent": sent, "meta": doc.meta, "skipped": dict(ctx.skipped), "assets": assets}))
"""


def child(code, data, *args, env=None):
    r = subprocess.run([str(PY), "-I", "-X", "utf8", "-c", code, str(HERE / "scripts"), *args], input=data,
                       capture_output=True, timeout=300, env=env)
    err = r.stderr.decode("utf-8", "replace")
    assert r.returncode == 0, err
    return json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])


def test_pdfium_gray_render_size():
    whole, capped = child(_GRAY, simple_pdf([text_ops("Gray")]))
    assert whole[:3] == ["Gray", 1700, 2200] and whole[3] < 128 and whole[4] == 255, whole
    assert capped[0] == "Gray" and 900_000 < capped[1] * capped[2] <= 1_000_000, capped
    assert abs(capped[1] / capped[2] - 1700 / 2200) < 0.01, capped


def test_pdfium_scan_read_by_ocr():
    """A scanned page renders once: the gray at OCR_DPI goes to the worker, the page PNG at RENDER_DPI is held and
    linked only when the page is doubtful."""
    data = with_image(FULL, image_xobject(64, 64, data=bytes(range(64)) * 64))
    stub = str(HERE / "tests" / "ocr_stub_worker.py")
    for unread, linked in ((0.0, False), (0.5, True)):
        conf = {"answer": {"unread": {"share": unread, "box": None, "area": 0.0}}}
        got = child(_SCAN, data, stub, env=dict(os.environ, PHRO_OCR_STUB=json.dumps(conf)))
        assert got["sent"] == [[1700, 2200, "page", 200.0]], got["sent"]
        md = got["md"]
        assert md.index("_Scanned page, text by OCR._") < md.index("w1700"), md
        assert ("![Scanned page 1](assets/img-001.png)" in md) == linked, md
        assert got["assets"] == ([["img-001.png", 935, 1210]] if linked else []), got["assets"]
        assert got["meta"]["ocr"] == {"pages": 1, "doubtful": int(linked), "failed": 0, "stopped_at": None}
        assert not got["skipped"], got["skipped"]


def main():
    if PY is None:
        print("skip: no pdf venv")
        print("pass")
        return
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
