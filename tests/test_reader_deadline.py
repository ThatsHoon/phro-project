"""What a read loads and how long it may run.

Run: python -I -X utf8 tests/test_reader_deadline.py
"""
import itertools
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader_fix as fx  # noqa: E402
from reader import markdown  # noqa: E402
from reader.formats import ooxml  # noqa: E402
from reader.model import Context  # noqa: E402
import reader  # noqa: E402
from reader.limits import ReadError  # noqa: E402
import test_read_cli as cli  # noqa: E402
import test_reader_doc as dc  # noqa: E402
import test_reader_docm as dm  # noqa: E402
import test_reader_hwp as hp  # noqa: E402
import test_reader_mobi as mb  # noqa: E402
import test_reader_epub as ep  # noqa: E402
import test_reader_hwpx as hx  # noqa: E402
import test_reader_ods_odp as oo  # noqa: E402
import test_reader_odt as ot  # noqa: E402
import test_reader_slides as sl  # noqa: E402
import test_reader_xps as xp  # noqa: E402

CHILD = """
import json, sys
sys.path.insert(0, sys.argv[1])
import reader
meta = reader.convert(sys.argv[2], sys.argv[3], backend=("win-ko", "1"))
print(json.dumps({"mods": sorted(m for m in sys.modules if m.startswith("reader")), "ocr": meta.get("ocr")}))
"""
OCR_PARTS = {"reader.ocr", "reader.ocr_pixels", "reader.raster", "reader.emf_write", "reader.fonts",
             "reader.svg_write", "reader.vector"}
NO_COUNTS = {"pages": 0, "doubtful": 0, "failed": 0, "stopped_at": None,
             "pictures_left": 0, "pictures_doubtful": 0, "pictures_blank": 0}


def tmp():
    return Path(tempfile.mkdtemp())


def child(src) -> dict:
    got = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", CHILD, str(HERE / "scripts"), str(src),
                          str(tmp() / "out")], capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert got.returncode == 0, got.stderr
    return json.loads(got.stdout)


def test_a_read_with_no_raster_picture_loads_no_ocr_module():
    src = tmp() / "a.xlsx"
    fx.make_xlsx(src, [{"name": "S", "rows": fx.xlsx_row(1, fx.xlsx_cell("A1", "1"))}])
    got = child(src)
    assert not OCR_PARTS & set(got["mods"]), sorted(OCR_PARTS & set(got["mods"]))
    assert "reader.formats.xls" not in got["mods"] and "reader.formats.ooxml_sheets" in got["mods"]
    assert got["ocr"] == NO_COUNTS, got["ocr"]


def test_a_read_with_a_raster_picture_opens_the_ocr_session():
    got = child(cli.docx(cli.png(8, 8)))
    assert {"reader.ocr", "reader.raster"} <= set(got["mods"]), got["mods"]
    assert got["ocr"] == NO_COUNTS, got["ocr"]


def stopping(n):
    """A Context whose time is over at its n-th look at the clock."""
    ctx, looks = Context(), itertools.count(1)
    ctx.stop, ctx.clock = 1, lambda: 0 if next(looks) < n else 2
    return ctx


def shown(doc, ctx) -> str:
    return markdown.render(doc, tmp() / "assets", False, ctx.skipped)


REST = "the rest past the time limit)"


def test_context_is_late_from_its_stop_on_and_counts_it_once():
    ctx = Context()
    assert not ctx.late() and not ctx.skipped
    ctx = stopping(3)
    assert [ctx.late() for _ in range(5)] == [False, False, True, True, True] and ctx.skipped["time"] == 1


def test_a_workbook_stops_at_the_time_limit():
    rows, more = ("".join(fx.xlsx_row(r, fx.xlsx_cell(f"A{r}", f"{k}{r}", t="str")) for r in range(1, 6)) for k in "vw")
    data = fx.make_xlsx(None, [{"name": "S", "rows": rows}, {"name": "T", "rows": more}])
    ctx = stopping(4)
    out = shown(ooxml.parse_package(data, ctx), ctx)
    assert "v2" in out and "v3" not in out and "w1" not in out and REST in out, out
    assert "(Sheet cut at row 3 by the time limit; not read from there on)" in out, out
    ctx = stopping(7)
    out = shown(ooxml.parse_package(data, ctx), ctx)
    assert "v5" in out and "Sheet cut" not in out and "Sheet: T" not in out and REST in out, out


def test_a_late_convert_says_so_and_is_filed_partial():
    import time
    from reader import cache, gate, limits
    book = tmp() / "a.xlsx"
    fx.make_xlsx(book, [{"name": "S", "rows": fx.xlsx_row(1, fx.xlsx_cell("A1", "1"))}])
    src = dm.docm(dm.P("ALPHA"))
    whole = reader.convert(src, tmp() / "out", backend=("", ""))
    assert limits.READ_LATE not in whole["warnings"], whole
    saved = reader.STARTED
    reader.STARTED = time.monotonic() - 2 * limits.READ_SECONDS
    try:
        out = tmp() / "out"
        meta = reader.convert(src, out, backend=("", ""))
        with cli.env(PHRO_READER_CACHE=str(tmp())):
            filed = json.loads((cache.store("ab" * 32, src) / "meta.json").read_text(encoding="utf-8"))
        try:
            reader.convert(book, tmp() / "out", backend=("", ""))
            raise AssertionError("a workbook with no sheet read gave a result")
        except ReadError as e:
            assert (e.kind, e.detail) == ("damaged", "nothing read within the time limit"), e
    finally:
        reader.STARTED = saved
    assert limits.READ_LATE in meta["warnings"] and REST in (out / "out.md").read_text(encoding="utf-8"), meta
    assert filed["partial"] is True, filed
    assert gate._time_cut(filed) == " Cut by the time limit: the rest of the document not read.", gate._time_cut(filed)
    assert gate._time_cut(filed, True) == " Cut by the time limit."


def test_the_render_writes_no_block_past_its_stop():
    import time
    from reader.model import Doc, Para, Text
    doc = Doc(blocks=[Para([Text("ALPHA")]), Para([Text("OMEGA")])])
    for stop, late in ((None, False), (time.monotonic() + 600, False), (time.monotonic() - 1, True)):
        info = {}
        out = markdown.render(doc, tmp() / "assets", False, None, info, stop=stop)
        assert info["late"] is late and ("OMEGA" in out) is not late and (REST in out) is late, (stop, out, info)
    info = {}
    out = markdown.render(doc, tmp() / "assets", False, {"time": 1}, info, stop=time.monotonic() - 1)
    assert out.count(REST) == 1, out


def cut_mid(label, path):
    """The document at `path` holds ALPHA in its first part and OMEGA in its last: read with a clock that is over at
    its n-th look, for every n, it gives its parts in order up to where the time ran out and says the rest was not
    read; some n stops it between the two."""
    ctx = Context()
    out = shown(reader.parse(path, ctx), ctx)
    assert "ALPHA" in out and "OMEGA" in out and REST not in out, (label, out)
    mids = []
    for n in range(1, 400):
        ctx = stopping(n)
        try:
            out = shown(reader.parse(path, ctx), ctx)
        except ReadError as e:
            assert ctx.skipped["time"], (label, n, e)
            continue
        if not ctx.skipped["time"]:
            assert "OMEGA" in out and REST not in out, (label, n, out)
            break
        assert REST in out and not ("OMEGA" in out and "ALPHA" not in out), (label, n, out)
        if "ALPHA" in out and "OMEGA" not in out:
            mids.append(n)
    else:
        raise AssertionError(f"{label}: still late at the 400th look")
    assert mids, label


def words(make):
    """`make(text)` for ALPHA, three fillers and OMEGA."""
    return [make(t) for t in ("ALPHA", "m1", "m2", "m3", "OMEGA")]


def test_word_documents_stop_at_the_time_limit():
    cut_mid("docx", dm.docm("".join(words(dm.P))))
    cut_mid("hwpx", hx.hwpx_doc("".join(words(hx.P))))
    cut_mid("odt", ot.odt_doc("".join(words(lambda t: f"<text:p>{t}</text:p>"))))
    path = tmp() / "a.rtf"
    path.write_bytes(b"{\\rtf1 ALPHA\\par " + b"m\\par " * 3000 + b"OMEGA\\par}")
    cut_mid("rtf", path)
    cut_mid("hwp", hp.hwp_file(b"".join(words(fx.hwp_para))))
    cut_mid("doc", dc.doc_file([(t + "\r", True) for t in words(str)]))


def test_decks_stop_at_the_time_limit():
    cut_mid("pptx", sl.deck(words(lambda t: sl.sp(t, "title"))))
    cut_mid("odp", oo.odp("".join(oo.page(f"p{i}", oo.frame(t)) for i, t in enumerate(words(str)))))
    path = tmp() / "a.ppt"
    fx.make_ppt(path, [{"title": t, "body": []} for t in words(str)])
    cut_mid("ppt", path)


def test_other_workbooks_stop_at_the_time_limit():
    sheets = [{"name": f"S{i}", "cells": {(0, 0): t, (1, 0): t + "b"}} for i, t in enumerate(words(str))]
    for ext, make in (("xls", fx.make_xls), ("xlsb", fx.make_xlsb)):
        path = tmp() / ("a." + ext)
        make(path, sheets)
        cut_mid(ext, path)
    cut_mid("ods", oo.ods("".join(oo.sheet(f"S{i}", [oo.row(oo.c(t))]) for i, t in enumerate(words(str)))))


def test_paged_documents_and_books_stop_at_the_time_limit():
    cut_mid("xps", xp.xps_doc(words(xp.g)))
    cut_mid("epub", ep.epub_book({f"c{i}.xhtml": f"<p>{t}</p>" for i, t in enumerate(words(str))}))
    path = tmp() / "a.mobi"
    path.write_bytes(mb._mobi(b"<html><body>" + b"<mbp:pagebreak/>".join(b"<p>%s</p>" % t.encode() for t in words(str))
                              + b"</body></html>"))
    cut_mid("mobi", path)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
