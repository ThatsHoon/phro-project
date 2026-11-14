"""ppt default-password decryption: RC4 CryptoAPI persist objects and Pictures records; any other password or an AES session stays encrypted.

Run: python -I -X utf8 tests/test_reader_ppt_crypt.py
"""
import struct
import sys
import tempfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import limits, markdown  # noqa: E402
from reader.containers.officecrypt import PPT_DEFAULT  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context  # noqa: E402
from reader.containers.cfb import Compound  # noqa: E402
from reader_fix import blip_png, make_cfb, make_ppt  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"P" * 24
SLIDES = [{"title": "Quarterly plan", "body": ["first point", "second point"]},
          {"title": "Second slide", "body": ["more text"]}]
NOTES = {1: "speaker words"}


def build(slides=SLIDES, **kw):
    """Build a .ppt, parse and render it: (markdown, ctx, {asset name: bytes}, make_ppt info)."""
    d = Path(tempfile.mkdtemp())
    path = d / "a.ppt"
    info = make_ppt(path, slides, **kw)
    ctx = Context()
    doc = reader.parse(path, ctx)
    out = markdown.render(doc, d / "assets", False, ctx.skipped)
    assets = {p.name: p.read_bytes() for p in (d / "assets").glob("*")} if (d / "assets").exists() else {}
    return out, ctx, assets, info


def denied(slides=SLIDES, **kw):
    try:
        build(slides, **kw)
    except ReadError as e:
        return e.kind
    raise AssertionError("expected a ReadError")


def build_patched(slides, patch, **kw):
    """Like build, but `patch(info, streams)` edits the stream bytes ({name: bytearray}) before parsing."""
    d = Path(tempfile.mkdtemp())
    path = d / "a.ppt"
    info = make_ppt(path, slides, **kw)
    with Compound(path) as cf:
        streams = {n: bytearray(cf.read(n)) for n in ("PowerPoint Document", "Current User", "Pictures")}
    patch(info, streams)
    make_cfb(path, streams)
    ctx = Context()
    doc = reader.parse(path, ctx)
    out = markdown.render(doc, d / "assets", False, ctx.skipped)
    assets = {p.name: p.read_bytes() for p in (d / "assets").glob("*")} if (d / "assets").exists() else {}
    return out, ctx, assets


THREE = [{"title": f"Slide {n}", "body": [f"text {n}"]} for n in (1, 2, 3)]


def test_default_password_reads():
    out, ctx, _, info = build(notes=NOTES, encrypt=PPT_DEFAULT)
    # the fixture really encrypted: no title is readable in the stream
    assert "Quarterly plan".encode("utf-16le") not in info["doc_stream"], "fixture left the text clear"
    for text in ("Quarterly plan", "first point", "second point", "Second slide", "more text", "speaker words"):
        assert text in out, (text, out)
    assert not ctx.skipped, dict(ctx.skipped)


def test_user_password_denied():
    assert denied(encrypt="secret") == "encrypted"


def test_encrypted_picture():
    slide = {"shapes": [{"text": "caption", "box": (0, 10, 400, 50)},
                        {"kind": "picture", "pib": 1, "box": (0, 200, 400, 300)}]}
    out, ctx, assets, info = build([slide], pictures=[blip_png(PNG)], encrypt=PPT_DEFAULT)
    assert assets == {"img-001.png": PNG}, (assets, out)
    assert "caption" in out and "![](assets/img-001.png)" in out and not ctx.skipped, (out, dict(ctx.skipped))


def test_aes_session_denied():
    aes = struct.pack("<HHI", 4, 4, 0) + bytes(60)
    assert denied(encrypt=PPT_DEFAULT, encrypt_header=aes) == "encrypted"


def test_decrypt_cap():
    slides = [{"title": "T", "body": ["word " * 400]}]
    build(slides, encrypt=PPT_DEFAULT)  # fits the default cap
    with mock.patch.object(limits, "DECRYPT_BYTES", 1000):
        assert denied(slides, encrypt=PPT_DEFAULT) == "too_large"


def test_bad_persist_offset_loses_one_slide():
    def patch(info, streams):
        struct.pack_into("<I", streams["PowerPoint Document"], info["slots"][info["slides"][1]], 10**7)
    out, ctx, _ = build_patched(THREE, patch, encrypt=PPT_DEFAULT)
    assert "Slide 1" in out and "Slide 3" in out and "Slide 2" not in out, out
    assert "(could not read: 1 slide)" in out, out


def test_overlapping_persist_entry_loses_one_slide():
    def patch(info, streams):
        at = info["offsets"][info["slides"][2]] + 8
        struct.pack_into("<I", streams["PowerPoint Document"], info["slots"][info["slides"][1]], at)
    out, ctx, _ = build_patched(THREE, patch, encrypt=PPT_DEFAULT)
    assert "Slide 1" in out and "Slide 3" in out and "Slide 2" not in out, out
    assert "(could not read: 1 slide)" in out, out


def test_bad_blip_keeps_earlier_pictures():
    first = len(blip_png(PNG))

    def patch(info, streams):
        streams["Pictures"][first + 2] ^= 0xFF  # the second record's type, enciphered
    shapes = [{"kind": "picture", "pib": 1, "box": (0, 10, 400, 50)},
              {"kind": "picture", "pib": 2, "box": (0, 200, 400, 300)}]
    out, ctx, assets = build_patched([{"shapes": shapes}], patch, pictures=[blip_png(PNG), blip_png(PNG + b"Q")],
                                     encrypt=PPT_DEFAULT)
    assert assets == {"img-001.png": PNG}, (assets, out)
    assert "(could not read: 1 image)" in out, out


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
