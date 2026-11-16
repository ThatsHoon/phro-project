"""Child of test_reader_pdfium.py, run under the pdf venv interpreter with -I.

It reads pdf bytes on stdin and a JSON option object from argv[1]:
{"patch": {name: value}, "pages": [a, b] | null, "hidden": bool, "engine": "pdfium" | "native",
"native_fails": bool}. It patches the caps of reader.formats.pdf (native_fails makes NativeDoc raise a
damaged ReadError, so PdfiumDoc opens with native None), runs parse_bytes and markdown.render into a
temporary dir, and prints one JSON line: {"md", "skipped", "meta", "assets": [[name, size, width,
height]], "modules", "engine_id", "decoded": [pixels decoded by each PdfiumDoc], "closed": [bool]},
or {"error": [kind, detail]} on a ReadError.

Two measurements (test_reader_pdf.py measure) print one other line and nothing else: "measure":
"tables" runs each page's PageData through pdf_tables.tables alone, {"tables": [pages, seconds]};
"measure": "convert" times parse_bytes alone, {"convert": [pages read, seconds, skipped]}.
"count": [pdfium function names] counts each named pypdfium2.raw call, as "calls": {name: n}.
"""
import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")


def _place(paths: list, here: str) -> list:
    """read.py's rule: `here` after the standard library and before site-packages (scripts/ holds
    queue.py, which must not shadow the stdlib queue)."""
    key = os.path.normcase(here)
    rest = [p for p in paths if os.path.normcase(os.path.abspath(p or ".")) != key]
    for i, p in enumerate(rest):
        if os.path.basename(os.path.normpath(p)).lower() in ("site-packages", "dist-packages"):
            return rest[:i] + [here] + rest[i:]
    return rest + [here]


sys.path[:] = _place(sys.path, _SCRIPTS)

import json  # noqa: E402
import struct  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402


def _dims(data: bytes):
    """(width, height) of a PNG or JPEG, else (0, 0)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return 0, 0


def _measure_tables(data, pdf, pdf_pdfium):
    """[pages, seconds spent in pdf_tables.tables] over every page of `data` under pdfium."""
    import time
    from collections import Counter
    from reader.pdfnative.document import NativeDoc
    tb = pdf.pdf_tables
    doc = pdf_pdfium.PdfiumDoc(data, NativeDoc(data))
    budget, cut, spent = tb.TableBudget(), Counter(), 0.0
    try:
        for index in range(doc.page_count):
            pd = doc.page(index)
            segs = [tb.Segment(s.x0, s.y0, s.x1, s.y1) for s in pd.segments]
            short = [tb.Segment(s.x0, s.y0, s.x1, s.y1) for s in pd.short]
            frags = sorted(pd.fragments, key=lambda f: f.order)
            t0 = time.perf_counter()
            tb.tables(segs, frags, pd.number, budget, cut, short=short)
            spent += time.perf_counter() - t0
    finally:
        doc.close()
    return [doc.page_count, spent]


def main():
    opts = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    data = sys.stdin.buffer.read()
    from reader import markdown
    from reader.formats import pdf
    from reader.limits import ReadError
    from reader.model import Context
    for name, value in (opts.get("patch") or {}).items():
        if not name.isupper() or not hasattr(pdf, name):
            raise SystemExit(f"unknown cap {name}")
        setattr(pdf, name, value)
    if opts.get("native_fails"):
        def _fails(_data, *_a, **_k):
            raise ReadError("damaged", "native made to fail by the test")
        from reader.pdfnative import document
        document.NativeDoc = _fails  # parse_bytes imports it from there at call time
    from reader import pyenv
    from reader.formats import pdf_pdfium
    made = []

    class _Seen(pdf_pdfium.PdfiumDoc):
        def __init__(self, *a, **k):
            made.append(self)
            super().__init__(*a, **k)

    pdf_pdfium.PdfiumDoc = _Seen
    calls = {}
    for name in opts.get("count") or ():
        fn = getattr(pdf_pdfium.raw, name)

        def counted(*a, _fn=fn, _name=name):
            calls[_name] += 1
            return _fn(*a)

        calls[name] = 0
        setattr(pdf_pdfium.raw, name, counted)
    pages = opts.get("pages")
    ctx = Context()
    if opts.get("measure") == "tables":
        print(json.dumps({"tables": _measure_tables(data, pdf, pdf_pdfium)}))
        return
    if opts.get("measure") == "convert":
        import time
        t0 = time.perf_counter()
        doc = pdf.parse_bytes(data, ctx, engine="pdfium")
        took = time.perf_counter() - t0
        print(json.dumps({"convert": [len(doc.blocks), took, dict(ctx.skipped)]}))
        return
    try:
        doc = pdf.parse_bytes(data, ctx, engine=opts.get("engine") or "pdfium",
                              pages=tuple(pages) if pages else None)
    except ReadError as e:
        print(json.dumps({"error": [e.kind, e.detail]}))
        return
    with tempfile.TemporaryDirectory() as tmp:
        assets_dir = Path(tmp) / "assets"
        text = markdown.render(doc, assets_dir, bool(opts.get("hidden")), ctx.skipped)
        assets = []
        if assets_dir.is_dir():
            for p in sorted(assets_dir.iterdir()):
                body = p.read_bytes()
                assets.append([p.name, len(body), *_dims(body)])
    print(json.dumps({"md": text, "skipped": dict(ctx.skipped), "meta": doc.meta, "assets": assets,
                      "modules": sorted(sys.modules), "engine_id": pyenv.engine_id_from_module(),
                      "decoded": [getattr(d, "decoded", None) for d in made],
                      "closed": [getattr(d, "_pdf", None) is None for d in made],
                      "calls": calls}, ensure_ascii=False))


if __name__ == "__main__":
    main()
