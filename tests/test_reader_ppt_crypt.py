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
from reader.formats import ppt_crypt, ppt_records  # noqa: E402
from reader_fix import blip_bitmap, blip_metafile, blip_png, make_cfb, make_ppt  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"P" * 24
SLIDES = [{"title": "Quarterly plan", "body": ["first point", "second point"]},
          {"title": "Second slide", "body": ["more text"]}]
NOTES = {1: "speaker words"}
JPEG = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + bytes(28)
EMF = struct.pack("<II", 1, 88) + bytes(32) + b" EMF" + bytes(44)


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


def streams_of(**kw):
    """make_ppt's (info, Persist, PowerPoint Document, Pictures) for an encrypted file."""
    d = Path(tempfile.mkdtemp())
    path = d / "a.ppt"
    info = make_ppt(path, kw.pop("slides", THREE), **kw)
    with Compound(path) as cf:
        doc, cu, pics = cf.read("PowerPoint Document"), cf.read("Current User"), cf.read("Pictures")
    return info, ppt_records.Persist(doc, cu, Context()), doc, pics


def test_overlapping_entry_is_missing_after_unlock():
    info, persist, doc, pics = streams_of(encrypt=PPT_DEFAULT)
    # slide 2's entry points into slide 3's body: where it lands is another object's plaintext once unlocked
    persist.objects[info["slides"][1]] = info["offsets"][info["slides"][2]] + 8
    persist.doc = ppt_crypt.unlock(doc, persist, pics)[0]  # as ppt.Presentation does
    assert persist.obj(info["slides"][1]) is None, persist.obj(info["slides"][1])
    assert persist.obj(info["slides"][2])[0] == ppt_records.SLIDE_CONTAINER


def test_out_of_stream_entry_is_missing_after_unlock():
    info, persist, doc, pics = streams_of(encrypt=PPT_DEFAULT)
    persist.objects[info["slides"][1]] = len(doc) - 4  # the header does not fit
    persist.doc = ppt_crypt.unlock(doc, persist, pics)[0]
    assert persist.obj(info["slides"][1]) is None


def test_older_edits_are_encrypted_too():
    out, ctx, _, info = build(THREE, encrypt=PPT_DEFAULT, edits=3)
    old, new = info["slide1_offsets"]
    stream = info["doc_stream"]
    assert old != new and struct.unpack_from("<H", stream, old + 2)[0] != 0x03EE, "older copy left clear"
    assert "Slide 1" not in stream.decode("latin-1") and "Slide 1".encode("utf-16le") not in stream
    assert "Slide 1" in out and "Slide 3" in out and not ctx.skipped, (out, dict(ctx.skipped))


def test_missing_crypt_ref_denied():
    def patch(info, streams):
        struct.pack_into("<I", streams["PowerPoint Document"], info["user_edits"][-1] + 8 + 28, 0)
    try:
        build_patched(THREE, patch, encrypt=PPT_DEFAULT)
    except ReadError as e:
        assert e.kind == "encrypted", e
    else:
        raise AssertionError("expected a ReadError")


def test_two_uid_and_metafile_blips():
    pictures = [blip_bitmap(0xF01D, 0x46B, JPEG), blip_metafile(0xF01A, 0x3D5, EMF), blip_png(PNG)]
    shapes = [{"kind": "picture", "pib": k, "box": (0, 100 * k, 400, 100 * k + 50)} for k in (1, 2, 3)]
    out, ctx, assets, _ = build([{"shapes": shapes}], pictures=pictures, encrypt=PPT_DEFAULT)
    assert sorted(assets.values(), key=len) == sorted([JPEG, EMF, PNG], key=len), (assets, out)
    assert not ctx.skipped, dict(ctx.skipped)


def test_clear_records_stay_byte_identical():
    info, persist, doc, pics = streams_of(encrypt=PPT_DEFAULT, edits=2)
    out, _ = ppt_crypt.unlock(doc, persist, pics)
    assert len(out) == len(doc) and out != doc
    spans = [(at, 8 + struct.unpack_from("<I", doc, at + 4)[0])
             for at in info["user_edits"] + info["directories"] + [info["offsets"][info["crypt"]]]]
    for at, size in spans:
        assert out[at:at + size] == doc[at:at + size], at
    assert out[:8] != doc[:8] and out[info["offsets"][info["slides"][0]]:][:2] != doc[info["offsets"][info["slides"][0]]:][:2]


def test_object_cap_is_too_large():
    # 100,000 persist objects is the cap; dochan fails on a file that large, this reader names the cause
    info, persist, doc, pics = streams_of(encrypt=PPT_DEFAULT)
    with mock.patch.object(ppt_crypt, "MAX_OBJECTS", len(persist.objects) - 1):
        try:
            ppt_crypt.unlock(doc, persist, pics)
        except ReadError as e:
            assert e.kind == "too_large", e
        else:
            raise AssertionError("expected a ReadError")
    assert ppt_crypt.unlock(doc, persist, pics) is not None


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
