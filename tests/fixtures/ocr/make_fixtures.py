"""The OCR test fixtures, made from the engine spike's synthetic pages.

The pages come from `make_pages.py` (a copy of the spike's script, kept here so the fixtures can be made again):
synthetic A4 pages drawn from system fonts with text written for the spike, at 300 dpi, then 150 dpi, a 2-degree
skew or noise. No outside document is copied. For each page named below this writes:
- `<name>.gray.z`: zlib(level 9) of a ">II" header (width, height) and the Gray8 pixels, row-major, 0 black;
- `<name>.txt`: the ground truth, one printed line per line;
- `<name>.win.json`: the spike's Windows OCR result for the page (PowerShell WinRT, `winocr.ps1`), BOM stripped.

Run with an interpreter that has Pillow (not the reader's):
    python -I -X utf8 make_fixtures.py <spike_dir> [<out_dir>]
<spike_dir> holds `pages/` (from make_pages.py) and `out-winocr/` (from winocr.ps1).
"""
import struct
import sys
import zlib
from pathlib import Path

from PIL import Image

NAMES = ("ko-malgun-11.150dpi", "en-times-11.150dpi", "mix-table-malgun.150dpi")


def main(spike, out=None):
    spike = Path(spike)
    out = Path(out) if out else Path(__file__).resolve().parent
    for name in NAMES:
        with Image.open(spike / "pages" / f"{name}.png") as im:
            g = im.convert("L")
            w, h = g.size
            data = g.tobytes()
        (out / f"{name}.gray.z").write_bytes(zlib.compress(struct.pack(">II", w, h) + data, 9))
        (out / f"{name}.txt").write_text((spike / "pages" / f"{name}.txt").read_text(encoding="utf-8"),
                                         encoding="utf-8", newline="\n")
        (out / f"{name}.win.json").write_text(
            (spike / "out-winocr" / f"{name}.json").read_text(encoding="utf-8-sig"), encoding="utf-8")
        print(name, w, h)


if __name__ == "__main__":
    main(*sys.argv[1:])
