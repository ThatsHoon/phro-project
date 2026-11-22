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
import test_read_cli as cli  # noqa: E402

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


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
