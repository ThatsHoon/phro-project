"""Compound file container: roundtrip, hostile chains and directories, the OOXML encrypted check.

Run: python -I -X utf8 tests/test_reader_cfb.py
"""
import struct
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import limits  # noqa: E402
from reader.containers.cfb import Compound, CfbError, is_cfb  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context  # noqa: E402
from reader_fix import make_cfb  # noqa: E402

STREAMS = {
    "FileHeader": bytes(range(256)),
    "BodyText/Section0": bytes(i * 7 % 251 for i in range(5000)),
    "BodyText/Section1": b"ten bytes!",
    "Empty": b"",
}


def tmp():
    return Path(tempfile.mkdtemp())


def build(streams=None, ss=512):
    p = tmp() / "f.cfb"
    info = make_cfb(p, STREAMS if streams is None else streams, ss)
    return p, info


def patch(p, off, fmt, *vals):
    b = bytearray(p.read_bytes())
    struct.pack_into(fmt, b, off, *vals)
    p.write_bytes(bytes(b))


def entry_off(info, k, ss=512):
    """File offset of directory entry k."""
    return (info["dir"][k * 128 // ss] + 1) * ss + k * 128 % ss


def fat_slot(info, sid, ss=512):
    """File offset of the FAT link of sector sid."""
    per = ss // 4
    return (info["fat"][sid // per] + 1) * ss + sid % per * 4


def raises(exc, fn, *a):
    try:
        fn(*a)
    except exc as e:
        return e
    raise AssertionError(f"{exc.__name__} not raised")


def check_roundtrip(ss):
    p, _ = build(ss=ss)
    with Compound(p) as c:
        assert set(c.names()) == set(STREAMS)
        for name, data in STREAMS.items():
            assert c.read(name) == data, name
            assert c.size(name) == len(data)


def test_roundtrip():
    check_roundtrip(512)


def test_v4_sectors():
    check_roundtrip(4096)


def test_case_insensitive():
    p, _ = build()
    with Compound(p) as c:
        assert c.exists("bodytext/SECTION0") and not c.exists("bodytext")
        assert c.read("bodytext/SECTION0") == STREAMS["BodyText/Section0"]
        assert "BodyText/Section0" in c.names()


def test_missing_stream():
    p, _ = build()
    with Compound(p) as c:
        raises(KeyError, c.read, "Nope")
        assert not c.exists("Nope")


def test_not_cfb():
    for blob in (b"PK\x03\x04" + b"\x00" * 600, b"hello" * 200, b""):
        e = raises(ReadError, Compound, blob)
        assert e.kind == "damaged" and e.detail == "not a compound file"
    assert not is_cfb(b"PK\x03\x04") and is_cfb(bytes.fromhex("d0cf11e0a1b11ae1") + b"x")


def test_damaged_header_and_directory():
    p, info = build()
    good = p.read_bytes()
    patch(p, 0x1E, "<H", 7)
    e = raises(ReadError, Compound, p)
    assert e.kind == "damaged" and e.detail == "compound file header"
    p.write_bytes(good)
    patch(p, 0x30, "<I", 10**6)  # the directory starts outside the file
    e = raises(ReadError, Compound, p)
    assert e.kind == "damaged" and e.detail == "compound file directory"
    p.write_bytes(good)
    patch(p, 0x4C, "<I", 10**6)  # a FAT sector outside the file
    assert raises(ReadError, Compound, p).detail == "compound file header"


def test_fat_cycle():
    big = {"A": bytes(5000)}
    p, info = build(big)
    chain = info["chains"]["A"]
    patch(p, fat_slot(info, chain[-1]), "<I", chain[0])
    with Compound(p) as c:
        raises(CfbError, c.read, "A")


def test_chain_past_end():
    p, info = build({"A": bytes(5000)})
    patch(p, fat_slot(info, info["chains"]["A"][2]), "<I", 10**6)
    with Compound(p) as c:
        raises(CfbError, c.read, "A")


def test_cross_link():
    p, info = build({"A": bytes(5000), "B": bytes(5000)})
    # entry 2 is B: start it at A's first sector, so both chains cover the same sectors
    patch(p, entry_off(info, 2) + 116, "<I", info["chains"]["A"][0])
    with Compound(p) as c:
        assert c.read("A") == bytes(5000)
        raises(CfbError, c.read, "B")


def test_directory_loop():
    p, info = build()
    patch(p, entry_off(info, 1) + 72, "<I", 1)  # entry 1 is its own right sibling
    with Compound(p) as c:
        names = c.names()
        assert names.count("FileHeader") == 1 and len(names) == len(set(names))


def test_stream_over_limit():
    p, info = build({"A": bytes(5000)})
    patch(p, entry_off(info, 1) + 120, "<I", limits.CFB_STREAM + 1)
    with Compound(p) as c:
        e = raises(ReadError, c.read, "A")
        assert e.kind == "too_large" and e.detail.startswith("part:")


def test_nested_bytes():
    p, _ = build()
    with Compound(p.read_bytes()) as c:
        for name, data in STREAMS.items():
            assert c.read(name) == data


def test_ooxml_encrypted_entry_anywhere():
    streams = {"Filler": bytes(70000)}  # regular sectors first, so the directory starts past 64 KiB
    for i in range(40):
        streams[f"S{i:02d}"] = b"x"
    streams["EncryptionInfo"] = b"info"
    streams["EncryptedPackage"] = b"data"
    p, info = build(streams)
    p = p.rename(p.with_suffix(".docm"))
    raw = p.read_bytes()
    at = raw.find("EncryptedPackage".encode("utf-16-le"))
    assert at > 65536, at
    assert at > (info["dir"][0] + 1) * 512 + 512  # and not in the first directory sector
    e = raises(ReadError, reader.parse, p, Context())
    assert e.kind == "encrypted"


def test_ooxml_cfb_without_encryption_is_damaged():
    p, _ = build()
    p = p.rename(p.with_suffix(".docm"))
    assert raises(ReadError, reader.parse, p, Context()).kind == "damaged"
    p.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 600)  # magic, but not a compound file
    assert raises(ReadError, reader.parse, p, Context()).kind == "damaged"


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
