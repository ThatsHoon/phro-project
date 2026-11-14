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
from reader_fix import blip_png, make_ppt  # noqa: E402

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


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
