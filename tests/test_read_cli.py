"""read.py's convert command, and reader.convert with OCR: lone TIFF scans, meta.json, the cache rule.

Run: python -I -X utf8 tests/test_read_cli.py
"""
import contextlib
import io
import json
import os
import struct
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))
import read  # noqa: E402

import ocr_fix  # noqa: E402
import reader  # noqa: E402
from pdf_fix import Ref, image_xobject, simple_pdf  # noqa: E402
from reader import images, limits, ocr, ocr_probe  # noqa: E402
from reader.formats import hwp_records as hr  # noqa: E402
from reader.ocr_pixels import Gray  # noqa: E402
from reader_fix import hwp_para, hwp_picture, hwp_record, make_hwp, make_zip  # noqa: E402

SCANNED = "_Scanned page, text by OCR._"


def tmp():
    return Path(tempfile.mkdtemp())


def test_convert_takes_a_file_uri():
    assert read.source("d/a b.docx") == "d/a b.docx"
    assert read.source("file:d/it%27s%20%24%28x%29.docx") == os.path.join("d", "it's $(x).docx")
    if os.name == "nt":
        assert read.source("file:///C:/d/John%27s%20report.docx") == "C:\\d\\John's report.docx"
        assert read.source("file://host/d/a%27.docx") == "\\\\host\\d\\a'.docx"
    else:
        assert read.source("file:///d/it%27s.docx") == "/d/it's.docx"


def test_internal_error_keeps_document_text_out():
    def boom(src):
        raise RuntimeError("line 3 of the document: account 1234")

    saved = read._reexec, read.reader.precheck
    read._reexec, read.reader.precheck = (lambda a: None), boom
    args = SimpleNamespace(file="x.docx", engine=None, pages=None, out=None, key=None, hidden=False)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = read.cmd_convert(args)
    finally:
        read._reexec, read.reader.precheck = saved
    assert code == 1 and out.getvalue() == "FAIL\tdamaged\tinternal error RuntimeError\n", out.getvalue()


# ---- documents built in memory, written to a temporary folder (synthetic and clean)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = (f'xmlns:w="{W}" xmlns:r="{REL}" '
      'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
      'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"')


def png(w, h, value=255):
    """A gray PNG of w x h pixels: white with a dark band across its middle."""
    rows = [bytes([value]) * w] * h
    rows[h // 2] = bytes(w)
    return images.gray_png(Gray(w, h, b"".join(rows)))


def docx(picture: bytes) -> Path:
    """A docx of a paragraph, the picture, and a paragraph."""
    pic = ('<w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="p" descr=""/><a:graphic><a:graphicData><pic:pic>'
           '<pic:blipFill><a:blip r:embed="rI"/></pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline>'
           '</w:drawing></w:r></w:p>')
    body = f'<w:p><w:r><w:t>Before</w:t></w:r></w:p>{pic}<w:p><w:r><w:t>After</w:t></w:r></w:p>'
    main = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    members = {
        "[Content_Types].xml": ('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                                '<Default Extension="png" ContentType="image/png"/>'
                                f'<Override PartName="/word/document.xml" ContentType="{main}"/></Types>'),
        "_rels/.rels": ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                        f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="word/document.xml"/>'
                        '</Relationships>'),
        "word/document.xml": f'<w:document {NS}><w:body>{body}</w:body></w:document>',
        "word/_rels/document.xml.rels": ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                                         f'relationships"><Relationship Id="rI" Type="{REL}/image" '
                                         'Target="media/i.png"/></Relationships>'),
        "word/media/i.png": picture,
    }
    return make_zip(tmp() / "a.docx", members)


def hwp(picture: bytes) -> Path:
    """An hwp of one paragraph and one picture, stored uncompressed."""
    info = (hwp_record(hr.DOCUMENT_PROPERTIES, 0, struct.pack("<H", 1) + bytes(24))
            + hwp_record(hr.BIN_DATA, 1, struct.pack("<HH", 1 | 2 << 4, 1) + struct.pack("<H", 3)
                         + "png".encode("utf-16-le")))
    body = hwp_para("Words") + hwp_para("￼", controls=(hwp_picture(1),))
    return make_hwp(tmp() / "a.hwp", [body], doc_info=info, bins={"BIN0001.png": picture})


def scanned_pdf(g: Gray) -> Path:
    """A one-page pdf whose page is `g` as one full-page Flate image."""
    data = simple_pdf([b"q 612 0 0 792 0 0 cm /Im1 Do Q "], resources={"XObject": {"Im1": Ref(30)}},
                      extra_objects={30: image_xobject(g.w, g.h, data=g.data)})
    path = tmp() / "a.pdf"
    path.write_bytes(data)
    return path


def tiff(pages, dpi=None) -> bytes:
    """A little-endian, uncompressed 8-bit grey TIFF (0 is black), one strip a page; pages are Grays."""
    out = bytearray(b"II*\x00\x00\x00\x00\x00")
    prev = 4
    for g in pages:
        at = len(out)
        out += g.data + b"\0" * (len(g.data) & 1)
        res = len(out)
        if dpi:
            out += struct.pack("<II", dpi, 1)
        struct.pack_into("<I", out, prev, len(out))
        tags = [(256, 4, g.w), (257, 4, g.h), (258, 3, 8), (259, 3, 1), (262, 3, 1), (273, 4, at), (277, 3, 1),
                (278, 4, g.h), (279, 4, g.w * g.h)]
        if dpi:
            tags += [(282, 5, res), (283, 5, res), (296, 3, 2)]
        out += struct.pack("<H", len(tags))
        for tag, typ, val in tags:
            out += struct.pack("<HHI", tag, typ, 1) + (struct.pack("<HH", val, 0) if typ == 3 else struct.pack("<I", val))
        prev = len(out)
        out += b"\0\0\0\0"
    return bytes(out)


def tiff_file(pages, dpi=None) -> Path:
    path = tmp() / "scan.tif"
    path.write_bytes(tiff(pages, dpi))
    return path


@contextlib.contextmanager
def env(**values):
    """Environment variables set for the body (None removes one), restored after."""
    saved = {k: os.environ.get(k) for k in values}
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def stub_engine(deadline=None, **conf):
    """reader.convert with an OCR engine: the backend id patched in, the stand-in worker (tests/ocr_stub_worker.py)
    behaving as `conf` says, a temporary cache root, and PHRO_READER_DEADLINE `deadline` seconds from now."""
    saved = ocr.backend, ocr.WORKER_CMD
    ocr.backend, ocr.WORKER_CMD = (lambda: ("win-ko", "test")), ocr_fix.STUB
    wall = None if deadline is None else repr(time.time() + deadline)
    try:
        with ocr_fix.stub_env(**conf), env(PHRO_READER_CACHE=str(tmp()), PHRO_READER_DEADLINE=wall):
            yield
    finally:
        ocr.backend, ocr.WORKER_CMD = saved


def convert(path, **opts):
    """(out.md, meta) of reader.convert into a temporary folder."""
    out = tmp()
    if path.suffix == ".pdf":
        opts.setdefault("started", time.monotonic())
    meta = reader.convert(path, out, **opts)
    return (out / "out.md").read_text(encoding="utf-8"), meta


# ---- OCR through reader.convert

def words(*lines):
    """A worker answer of the given lines of words, for a page of 600 x 800 pixels."""
    out = []
    for n, line in enumerate(lines):
        x = 100
        for t in line.split():
            out.append({"text": t, "x": x, "y": 200 + 40 * n, "w": 24 * len(t), "h": 30, "line": n, "conf": None})
            x += 24 * len(t) + 18
    page = {"w": 600, "h": 800, "words": out, "angle": 0.0, "turn": 0, "engine": "stub", "version": "1"}
    return {"page": page, "rules": [], "unread": {"share": 0.0, "box": None, "area": 0.0}}


def pages(n, w=600, h=800):
    """n white Grays of w x h with a dark bar, each its own shade so no two pages share pixels."""
    out = []
    for k in range(n):
        rows = [bytes([250 - k]) * w] * h
        rows[h // 3] = bytes(w)
        out.append(Gray(w, h, b"".join(rows)))
    return out


def test_tiff_pages_become_text():
    if sys.platform != "win32":
        print("skip: test_tiff_pages_become_text (needs GDI+)")
        return
    path = tiff_file(pages(2))
    with stub_engine(answer=words("Alpha beta gamma", "delta epsilon")):
        text, meta = convert(path)
    page = f"{SCANNED}\n\nAlpha beta gamma delta epsilon"
    assert text == f"(2 scanned pages read by OCR)\n\n{page}\n\n{page}\n", text
    assert meta["ocr"] == {"pages": 2, "doubtful": 0, "failed": 0, "stopped_at": None, "pictures_left": 0}, meta
    doubtful = words("Alpha beta gamma")
    doubtful["unread"] = {"share": 0.5, "box": None, "area": 0.0}
    with stub_engine(answer=doubtful):
        text, meta = convert(path)
    assert text == ("(2 scanned pages read by OCR; 2 of them doubtful, shown with the page image)\n\n"
                    f"{SCANNED}\n\nAlpha beta gamma\n\n![page 1](assets/img-001.png)\n\n"
                    f"{SCANNED}\n\nAlpha beta gamma\n\n![page 2](assets/img-002.png)\n"), text
    area = words("Alpha beta gamma")
    area["unread"] = {"share": 0.02, "box": [0.1, 0.5, 0.6, 0.9], "area": 0.2, "density": 0.5}
    with stub_engine(answer=area):
        text, meta = convert(tiff_file(pages(1)))
    assert text == (f"(1 scanned page read by OCR)\n\n{SCANNED}\n\nAlpha beta gamma\n\n"
                    "![Picture on page 1](assets/img-001.png)\n"), text
    with stub_engine(die_after=0):
        text, meta = convert(path)
    assert text == ("(OCR failed on 2 pages; shown as images.)\n\n"
                    "![page 1](assets/img-001.png)\n\n![page 2](assets/img-002.png)\n"), text
    assert meta["ocr"]["failed"] == 2 and "unread" in meta and not meta["unread"], meta


def test_tiff_late_pages_are_images():
    if sys.platform != "win32":
        print("skip: test_tiff_late_pages_are_images (needs GDI+)")
        return
    with stub_engine(deadline=limits.FIGURE_RESERVE + 2, delay=30, answer=words("Alpha beta")):
        text, meta = convert(tiff_file(pages(2)))
    assert text.startswith("(OCR stopped at page 1 for the time; later scanned pages are shown as images)\n\n"), text
    assert SCANNED not in text and "page 1" in text and "page 2" in text, text  # the pages as today, past the time
    assert meta["ocr"] == {"pages": 0, "doubtful": 0, "failed": 0, "stopped_at": 1, "pictures_left": 0}, meta
    assert limits.OCR_LATE in meta["warnings"], meta


def test_tiff_page_without_pixels_counts_as_failed():
    from reader import raster
    real = raster.gray

    def gray(data, page=0, deadline=None):
        return None if page == 0 else real(data, page=page, deadline=deadline)

    raster.gray = gray
    try:
        with stub_engine(answer=words("Alpha beta gamma")):
            text, meta = convert(tiff_file(pages(2)))
    finally:
        raster.gray = real
    assert text.startswith("(1 scanned page read by OCR)\n\n(OCR failed on 1 page; shown as an image.)\n\n"), text
    assert meta["ocr"] == {"pages": 1, "doubtful": 0, "failed": 1, "stopped_at": None, "pictures_left": 0}, meta


def test_tiff_discarded_layout_leaves_no_skips():
    from reader import ocr_layout
    real = ocr_layout.blocks

    def blocks(res, number, dpi, skipped, *, budget=None):
        skipped["cells"] += 1
        raise ValueError("layout")

    ocr_layout.blocks = blocks
    try:
        with stub_engine(answer=words("Alpha beta gamma")):
            text, meta = convert(tiff_file(pages(1)))
    finally:
        ocr_layout.blocks = real
    assert text == "(OCR failed on 1 page; shown as an image.)\n\n![](assets/img-001.png)\n", text
    assert meta["unread"] == [], meta


def test_tiff_crop_is_gray_from_the_read_pixels():
    if sys.platform != "win32":
        print("skip: test_tiff_crop_is_gray_from_the_read_pixels (needs GDI+)")
        return
    area = words("Alpha beta gamma")
    area["unread"] = {"share": 0.02, "box": [0.1, 0.5, 0.6, 0.9], "area": 0.2, "density": 0.5}
    out = tmp()
    with stub_engine(answer=area):
        reader.convert(tiff_file(pages(1)), out)
    crop = (out / "assets" / "img-001.png").read_bytes()
    w, h, ch, _ = images.png_pixels(crop)
    assert (w, h, ch) == (300, 320, 1), (w, h, ch)
    whole = words("Alpha beta gamma")
    whole["unread"] = {"share": 0.02, "box": [0.0, 0.1, 1.0, 0.9], "area": 0.8, "density": 0.5}
    with stub_engine(answer=whole):
        text, _ = convert(tiff_file(pages(1)))
    assert "![Picture on page 1](assets/img-001.png)" in text, text


def test_late_conversion_filed_partial():
    from reader import cache
    path = docx(png(80, 80))
    with stub_engine(deadline=limits.FIGURE_RESERVE + 2, delay=30, answer=words("Hello world")):
        k = cache.key(path)
        got = cache.store(k, path)
        filed = cache.lookup(k)
        meta = json.loads((got / "meta.json").read_text(encoding="utf-8"))
        text = (got / "out.md").read_text(encoding="utf-8")
    assert got.name == k and filed == got and meta["partial"] is True, (got, filed, meta)
    assert limits.OCR_LATE in meta["warnings"] and limits.FIGURES_LATE not in meta["warnings"], meta
    assert meta["ocr"]["pictures_left"] == 1, meta
    assert text.startswith("(OCR stopped; 1 picture not read)\n\n"), text


def test_finished_conversion_filed():
    from reader import cache
    path = docx(png(80, 80))
    with stub_engine(answer=words("Hello world")):
        k = cache.key(path)
        got = cache.store(k, path)
        assert cache.lookup(k) == got and not got.name.startswith(".tmp-"), got


def test_meta_json_has_ocr_counts():
    out = tmp()
    with stub_engine(answer=words("Hello world", "second line")):
        meta = reader.convert(docx(png(80, 80)), out)
    on_disk = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    counts = {"pages": 0, "doubtful": 0, "failed": 0, "stopped_at": None, "pictures_left": 0}
    assert meta["ocr"] == on_disk["ocr"] == counts, on_disk
    text = (out / "out.md").read_text(encoding="utf-8")
    assert text == "Before\n\n![](assets/img-001.png)\n\nPicture text: Hello world / second line\n\nAfter\n", text


# captured from the code before OCR reached reader.convert (1912329): a docx with a PNG picture, an hwp with a
# picture and a scanned pdf
NO_ENGINE = (
    "Before\n\n![](assets/img-001.png)\n\nAfter\n",
    "Words\n\n![](assets/img-001.png)\n",
    "## Page 1\n\n![Scanned page 1](assets/img-001.png)\n",
)


def test_no_engine_output_unchanged():
    with env(PHRO_READER_OCR="none"):
        assert ocr.backend() == ("", ""), ocr.backend()
        got = [convert(p) for p in (docx(png(80, 80)), hwp(png(80, 80)), scanned_pdf(pages(1)[0]))]
    assert tuple(text for text, _ in got) == NO_ENGINE, got
    for _, meta in got:
        assert "ocr" not in meta and limits.OCR_LATE not in meta["warnings"], meta


def test_read_cli_tiff_end_to_end():
    backend = _ko_backend()
    if backend is None:
        print("skip: test_read_cli_tiff_end_to_end (needs Windows OCR ko)")
        return
    name = "ko-malgun-11.150dpi"
    path = tiff_file([ocr_fix.load(name)], dpi=150)
    out = tmp()
    saved = ocr_probe.backend
    ocr_probe.backend = lambda: backend
    try:
        with env(PHRO_READER_CACHE=str(tmp()), PHRO_READER_DEADLINE=None), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            code = read.main(["convert", str(path), "--out", str(out)])
    finally:
        ocr_probe.backend = saved
    assert code == 0 and printed.getvalue().strip() == str(out / "out.md"), printed.getvalue()
    text = (out / "out.md").read_text(encoding="utf-8")
    assert text.startswith(f"(1 scanned page read by OCR)\n\n{SCANNED}\n\n"), text[:200]
    body = "\n".join(ln.lstrip("#").strip() for ln in text.splitlines()[4:])
    c = ocr_fix.cer(body, ocr_fix.truth(name))
    print(f"read.py convert on the ko fixture as a TIFF: CER {c:.4f}")
    assert c <= 0.03, c


def test_convert_probes_ocr_once():
    """read.py convert works the OCR id out once, for the key and for reader.convert, which opens its session with
    that same id."""
    calls, seen = [], []
    saved = ocr_probe.backend, ocr.opened

    def counted():
        calls.append(1)
        return "", ""

    def opened(deadline, backend, **k):
        seen.append(backend)
        return saved[1](deadline, backend, **k)
    ocr_probe.backend, ocr.opened = counted, opened
    try:
        for extra in ([], ["--out", str(tmp())]):
            calls.clear()
            seen.clear()
            with env(PHRO_READER_CACHE=str(tmp())), contextlib.redirect_stdout(io.StringIO()):
                code = read.main(["convert", str(docx(png(80, 80)))] + extra)
            assert code == 0 and calls == [1] and seen == [("", "")], (extra, code, calls, seen)
    finally:
        ocr_probe.backend, ocr.opened = saved


def _ko_backend():
    """("win-ko", version) when Windows OCR with the ko recognizer is here, else None."""
    e = ocr.engines()
    try:
        return (e.win.engine, e.win.version) if e.win is not None and e.win_korean else None
    finally:
        if e.win is not None:
            e.win.close()


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
