"""pdf through the pdfium front end: each test runs tests/pdfium_run.py under the pdf venv's
interpreter with the pdf on stdin. The venv is pdfenv.venv_dir() (PHRO_READER_PYENV first); the
tests run when its interpreter exists, else they print "skip: no pdf venv" and pass.

Run: python -I -X utf8 tests/test_reader_pdfium.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from pdf_fix import (JPEG, EncryptSpec, Name, Ref, Stream, acroform, image_xobject, link_annot,  # noqa: E402
                     ocg_catalog, outline_items, rule_ops, simple_pdf, text_annot, text_ops, widget)

from reader import cache, pdfenv  # noqa: E402

RUN = HERE / "tests" / "pdfium_run.py"
F1 = {"Font": {"F1": Ref(3)}}
FULL = b"q 612 0 0 792 0 0 cm /Im1 Do Q "


def _interpreter():
    if pdfenv.forced_native():
        return None
    py = pdfenv.interpreter(pdfenv.venv_dir())
    return py if py.is_file() else None


PY = _interpreter()


def run(data, **opts):
    """The child's JSON line for the pdf `data`; opts are patch, pages and hidden."""
    arg = json.dumps({"patch": opts.get("patch", {}), "pages": opts.get("pages"),
                      "hidden": bool(opts.get("hidden"))})
    r = subprocess.run([str(PY), "-I", "-X", "utf8", str(RUN), arg], input=data,
                       capture_output=True, timeout=300)
    err = r.stderr.decode("utf-8", "replace")
    assert r.returncode == 0, err
    lines = r.stdout.decode("utf-8").strip().splitlines()
    assert lines, err
    return json.loads(lines[-1])


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
        assert d.name != cache.key(src, "native"), d.name
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        assert meta["engine"] == "pdfium", meta
        assert "From the CLI" in (d / "out.md").read_text(encoding="utf-8")


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
