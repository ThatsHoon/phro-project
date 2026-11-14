"""ppt pictures: the BStore of the PPDrawingGroup, BLIPs in the Pictures stream, placements, budget, DIB to PNG.

Run: python -I -X utf8 tests/test_reader_ppt_pictures.py
"""
import struct
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import markdown  # noqa: E402
from reader.formats import ppt_pictures  # noqa: E402
from reader.model import Context  # noqa: E402
from reader_fix import blip_bitmap, blip_png, make_ppt  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"P" * 24
# a 1x1 24-bit DIB: BITMAPINFOHEADER, then one row of one red pixel padded to 4 bytes
DIB = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, 4, 0, 0, 0, 0) + b"\x00\x00\xff\x00"


def build(slides, **kw):
    """Build a .ppt, parse and render it: (markdown, ctx, {asset name: bytes})."""
    d = Path(tempfile.mkdtemp())
    path = d / "a.ppt"
    make_ppt(path, slides, **kw)
    ctx = Context()
    doc = reader.parse(path, ctx)
    out = markdown.render(doc, d / "assets", False, ctx.skipped)
    assets = {p.name: p.read_bytes() for p in (d / "assets").glob("*")} if (d / "assets").exists() else {}
    return out, ctx, assets


def pic(pib=1, box=(0, 200, 400, 300)):
    return {"kind": "picture", "pib": pib, "box": box}


def test_picture_on_slide():
    slide = {"shapes": [{"text": "above", "box": (0, 10, 400, 50)}, pic(), {"text": "below", "box": (0, 500, 400, 550)}]}
    out, ctx, assets = build([slide], pictures=[blip_png(PNG)])
    assert assets == {"img-001.png": PNG}, assets
    assert out.index("above") < out.index("![](assets/img-001.png)") < out.index("below"), out
    assert "could not read" not in out and not ctx.skipped, (out, dict(ctx.skipped))


def test_picture_reused_charged():
    slides = [{"shapes": [pic()]} for _ in range(3)]
    old = ppt_pictures.IMAGE_BYTES
    ppt_pictures.IMAGE_BYTES = 2 * len(PNG) + 1
    try:
        out, ctx, assets = build(slides, pictures=[blip_png(PNG)])
    finally:
        ppt_pictures.IMAGE_BYTES = old
    assert out.count("![](assets/") == 2 and "(could not read: 1 image)" in out, out


def test_no_pictures_stream():
    slide = {"shapes": [{"text": "still here", "box": (0, 10, 400, 50)}, pic()]}
    out, ctx, assets = build([slide], pictures=[blip_png(PNG)], no_pictures_stream=True)
    assert not assets and "still here" in out and "(could not read: 1 image)" in out, out
    # a pib outside the BStore, with no BStore at all, is counted the same way
    out, _, assets = build([{"shapes": [pic(5)]}])
    assert not assets and "(could not read: 1 image)" in out, out


def test_dib_to_png():
    out, ctx, assets = build([{"shapes": [pic()]}], pictures=[blip_bitmap(0xF01F, 0x7A8, DIB)])
    assert list(assets) == ["img-001.png"] and assets["img-001.png"].startswith(b"\x89PNG"), (out, assets)


def test_picture_keeps_its_text():
    shape = dict(pic(), text="caption text")
    out, ctx, assets = build([{"shapes": [shape]}], pictures=[blip_png(PNG)])
    assert len(assets) == 1 and out.count("caption text") == 1, out
    assert out.index("![](assets/img-001.png)") < out.index("caption text"), out
    slide = {"title": "T", "body": ["outline caption"], "outline": True,
             "shapes": [dict(pic(), ref=1), {"placeholder": 13, "ref": 0, "box": (0, 10, 400, 50)}]}
    out, ctx, assets = build([slide], pictures=[blip_png(PNG)])
    assert len(assets) == 1 and out.count("outline caption") == 1, out
    assert out.index("![](assets/img-001.png)") < out.index("outline caption"), out


def test_skipped_placeholder_picture():
    out, ctx, assets = build([{"shapes": [dict(pic(), placeholder=9)]}], pictures=[blip_png(PNG)])
    assert not assets and not ctx.skipped, (assets, dict(ctx.skipped))


def test_unreadable_or_unknown_blip_counted():
    from reader_fix import oa_record
    junk = [oa_record(0xF01E, b"short"),                  # a PNG BLIP cut inside its UID
            blip_bitmap(0xF01E, 0x6E0, b"")]              # a BLIP with no image bytes (under 16)
    unknown = blip_png(b"this is not an image at all")   # long enough, but images.sniff has no name for it
    for blip in (*junk, unknown):
        slide = {"shapes": [{"text": "kept", "box": (0, 10, 400, 50)}, pic()]}
        out, ctx, assets = build([slide], pictures=[blip])
        assert not assets and "kept" in out and "(could not read: 1 image)" in out, (out, dict(ctx.skipped))
        assert ctx.skipped["image"] == 1 and sum(ctx.skipped.values()) == 1, dict(ctx.skipped)


def test_picture_in_notes_is_placed():
    notes = {1: {"shapes": [{"text": "spoken", "box": (0, 10, 400, 50), "text_kind": 2}, pic()]}}
    out, ctx, assets = build([{"title": "T"}], pictures=[blip_png(PNG)], notes=notes)
    assert assets == {"img-001.png": PNG} and "spoken" in out and "![](assets/img-001.png)" in out, (out, assets)
    assert out.index("## Slide 1") < out.index("spoken") and not ctx.skipped, (out, dict(ctx.skipped))


def test_picture_free_slide_counts_nothing():
    out, ctx, assets = build([{"title": "T", "body": ["words"]}], pictures=[blip_png(PNG)])
    assert not assets and not ctx.skipped, (assets, dict(ctx.skipped))
    assert "![](" not in out, out


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
