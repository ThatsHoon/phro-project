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


def test_reread_big_stream():
    big = bytes(i % 253 for i in range(5000))
    p, _ = build({"Big": big, "Small": b"abc"})
    with Compound(p) as c:
        for name in ("Small", "Small", "Big", "Big", "big", "BIG"):
            assert c.read(name) == (b"abc" if name == "Small" else big), name


def test_fat_count_and_difat_are_bounded():
    p, info = build({"X": bytes(5000)})
    good = p.read_bytes()
    nsec = len(good) // 512 - 1
    patch(p, 0x2C, "<I", nsec + 1)  # more FAT sectors than the file has sectors
    e = raises(ReadError, Compound, p)
    assert e.kind == "damaged" and e.detail == "compound file header"
    p.write_bytes(good)
    patch(p, 0x2C, "<I", 0)
    assert raises(ReadError, Compound, p).detail == "compound file header"
    # a DIFAT chain of non-free sectors: the FAT must stay within the physical sector count
    p.write_bytes(good)
    chain = info["chains"]["X"]
    for i in range(3):
        nxt = chain[i + 1] if i < 2 else 0xFFFFFFFE
        patch(p, (chain[i] + 1) * 512, "<128I", *([info["fat"][0]] * 127 + [nxt]))
    patch(p, 0x44, "<II", chain[0], 3)
    patch(p, 0x2C, "<I", nsec)
    with Compound(p) as c:
        assert len(c._fat) <= nsec
        assert c.exists("X")


def test_shared_looping_chain_is_fast():
    import time
    streams = {"Filler": bytes(2000000)}
    for i in range(5000):
        streams[f"S{i}"] = b"x"
    p, info = build(streams)
    chain = info["chains"]["Filler"]
    patch(p, fat_slot(info, chain[-1]), "<I", chain[0])  # the chain loops
    b = bytearray(p.read_bytes())
    for k in range(2, 5002):  # every other entry claims the looping chain
        struct.pack_into("<I", b, entry_off(info, k) + 116, chain[0])
        struct.pack_into("<I", b, entry_off(info, k) + 120, 5000)
    p.write_bytes(bytes(b))
    t0 = time.monotonic()
    with Compound(p) as c:
        for k in range(5000):
            raises(CfbError, c.read, f"S{k}")
        raises(CfbError, c.read, "Filler")
    assert time.monotonic() - t0 < 2.0


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


def test_visio_stream_routes():
    # cfb_route: a compound file holding a VisioDocument stream belongs to vsd, whichever reader opened it
    from reader.formats import cfb_route
    from reader_fix import cfb_bytes
    with Compound(cfb_bytes({"VisioDocument": b"Visio (TM) Drawing\r\n"})[0]) as cf:
        assert cfb_route.route(cf, "doc", "no WordDocument stream") == "vsd"
        assert cfb_route.route(cf, "ppt", "no PowerPoint Document stream") == "vsd"
        assert cfb_route.route(cf, "vsd", "no VisioDocument stream") is None
    with Compound(cfb_bytes({"Other": b"x"})[0]) as cf:
        e = raises(ReadError, cfb_route.route, cf, "vsd", "no VisioDocument stream")
        assert e.kind == "damaged" and e.detail == "no VisioDocument stream"


def test_quill_routes():
    # cfb_route: a .doc compound holding "Quill/QuillSub/CONTENTS" -> "pub"; pub's own stream is Contents, so a
    # Quill stream without one is damaged for pub (never routed back to pub itself)
    from reader.formats import cfb_route
    from reader_fix import cfb_bytes
    with Compound(cfb_bytes({"Quill/QuillSub/CONTENTS": b"CHNKINK "})[0]) as cf:
        assert cfb_route.route(cf, "doc", "no WordDocument stream") == "pub"
        assert cfb_route.route(cf, "vsd", "no VisioDocument stream") == "pub"
        e = raises(ReadError, cfb_route.route, cf, "pub", "no Contents stream")
        assert e.kind == "damaged" and e.detail == "no Contents stream"
    with Compound(cfb_bytes({"Contents": b"\xE8\xAC\x2C\x00", "Quill/QuillSub/CONTENTS": b"CHNKINK "})[0]) as cf:
        assert cfb_route.route(cf, "pub", "no Contents stream") is None
        assert cfb_route.route(cf, "doc", "no WordDocument stream") == "pub"
    with Compound(cfb_bytes({"WordDocument": bytes(64)})[0]) as cf:
        assert cfb_route.route(cf, "pub", "no Contents stream") == "doc"


def test_msproject_progid():
    # cfb_route.progid: the ANSI ProgID of \x01CompObj; a ProgID starting MSProject. routes to mpp after the stream list
    from reader.formats import cfb_route
    from reader_fix import cfb_bytes, comp_obj
    prog = comp_obj(b"MSProject.MPP9", b"Microsoft Project")
    with Compound(cfb_bytes({"\x01CompObj": prog})[0]) as cf:
        assert cfb_route.progid(cf) == "MSProject.MPP9"
        assert cfb_route.route(cf, "doc", "no WordDocument stream") == "mpp"
        assert cfb_route.route(cf, "xls", "no Workbook stream") == "mpp"
        assert cfb_route.route(cf, "mpp", "no MSProject CompObj") is None
    with Compound(cfb_bytes({"\x01CompObj": prog, "VisioDocument": b"x"})[0]) as cf:
        assert cfb_route.route(cf, "doc", "no WordDocument stream") == "vsd"  # the stream list comes first
    with Compound(cfb_bytes({"Other": b"x"})[0]) as cf:
        assert cfb_route.progid(cf) == ""
    with Compound(cfb_bytes({"\x01CompObj": comp_obj(b"Word.Document.8")})[0]) as cf:
        assert cfb_route.progid(cf) == "Word.Document.8"
        e = raises(ReadError, cfb_route.route, cf, "doc", "no WordDocument stream")
        assert e.kind == "damaged" and e.detail == "no WordDocument stream"
    # a clipboard format as a marker and id (0xFFFFFFFF / 0xFFFFFFFE) or as a string before the ProgID
    head = struct.pack("<II", 0xFFFE0001, 0xA03).ljust(28, b"\xff")

    def ansi(s):
        return struct.pack("<I", len(s) + 1) + s + b"\0"
    for clip in (struct.pack("<II", 0xFFFFFFFF, 3), struct.pack("<II", 0xFFFFFFFE, 3), ansi(b"MSProject.MPP9")):
        with Compound(cfb_bytes({"\x01CompObj": head + ansi(b"Microsoft Project") + clip
                                 + ansi(b"MSProject.Project.9")})[0]) as cf:
            assert cfb_route.progid(cf) == "MSProject.Project.9"
    # hostile: lengths past the end, a missing terminator, nothing after the header, a stream of one byte
    for data in (head + struct.pack("<I", 0xFFFFFFF0), head + ansi(b"U") + bytes(4) + struct.pack("<I", 0x7FFFFFFF),
                 head + ansi(b"U") + struct.pack("<I", 0xFFFFFF00) + b"MSProject.", head, b"\x01",
                 head + ansi(b"U") + struct.pack("<I", 0xFFFFFFFF)):
        with Compound(cfb_bytes({"\x01CompObj": data})[0]) as cf:
            assert cfb_route.progid(cf) == "", data[28:]
    with Compound(cfb_bytes({"\x01CompObj": head + ansi(b"U") + bytes(4) + struct.pack("<I", 40) + b"MSProject.X"})[0]) as cf:
        assert cfb_route.progid(cf) == "MSProject.X"  # a short last string: what is there, up to its end
    # a CompObj over 4,096 bytes: only the head may be read, so it is not read at all (as doc_objects and
    # xls_embedded); one of exactly 4,096 bytes is read
    big = prog + bytes(4097 - len(prog))
    exact = prog + bytes(4096 - len(prog))
    with Compound(cfb_bytes({"\x01CompObj": exact})[0]) as cf:
        assert cfb_route.progid(cf) == "MSProject.MPP9"
    with Compound(cfb_bytes({"\x01CompObj": big})[0]) as cf:
        reads = []
        real = cf.read
        cf.read = lambda name: reads.append(name) or real(name)
        assert cfb_route.progid(cf) == "" and reads == []
        e = raises(ReadError, cfb_route.route, cf, "doc", "no WordDocument stream")
        assert e.kind == "damaged"
    # a CompObj whose chain cannot be read: no ProgID

    class Unreadable:
        def exists(self, name):
            return True

        def size(self, name):
            return 64

        def read(self, name):
            raise CfbError("short chain")
    assert cfb_route.progid(Unreadable()) == ""
    # a .doc compound file holding only the MSProject CompObj is handed to mpp (unsupported without the engine)
    d = tmp()
    p = d / "plan.doc"
    p.write_bytes(cfb_bytes({"\x01CompObj": prog})[0])
    e = raises(ReadError, reader.parse, p, Context())
    assert e.kind == "unsupported" and e.detail == "Microsoft Project files are read only through MPXJ", e


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
