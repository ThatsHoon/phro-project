"""The reader package skeleton, its limits and the safe zip container.

Run: python -I -X utf8 tests/test_reader_zipx.py
"""
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import limits  # noqa: E402
from reader.containers import zipx  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader_fix import bad_name_zip, make_zip  # noqa: E402


def tmp():
    return Path(tempfile.mkdtemp())


def raises_kind(fn, kind):
    try:
        fn()
    except ReadError as e:
        assert e.kind == kind, (e.kind, kind)
        return
    raise AssertionError("ReadError not raised")


def test_formats_cover_scope():
    assert len(reader.FORMATS) == 54 and not hasattr(reader, "SKILL_EXTS")
    assert reader.ext_of("C:/x/A.HWPX") == "hwpx"
    assert reader.ext_of("C:/x/noext") == ""
    assert reader.BUILT <= set(reader.FORMATS)


def test_read_and_case_insensitive_lookup():
    p = make_zip(tmp() / "a.zip", {"mimetype": "x", "Contents/Section0.xml": "<a/>"})
    with zipx.Package(p) as pkg:
        _check_lookup(pkg)


def _check_lookup(pkg):
    assert pkg.has("contents/section0.xml")
    assert pkg.read("contents/section0.xml") == b"<a/>"
    assert pkg.xml("missing.xml") is None


def test_xml_refuses_doctype():
    bad = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aa">]><x>&a;</x>'
    raises_kind(lambda: zipx.parse_xml(bad), "damaged")
    raises_kind(lambda: zipx.parse_xml(b"<x"), "damaged")
    u16 = "<!DOCTYPE x [<!ENTITY a 'b'>]><x/>".encode("utf-16")
    raises_kind(lambda: zipx.parse_xml(u16), "damaged")
    raises_kind(lambda: zipx.parse_xml("<!DOCTYPE x><x/>".encode("utf-16-le")), "damaged")
    raises_kind(lambda: zipx.parse_xml("<!DOCTYPE x><x/>".encode("utf-32")), "damaged")
    late = b"<?xml version='1.0'?><!--" + b"a" * 5000 + b"--><!DOCTYPE x><x/>"
    raises_kind(lambda: zipx.parse_xml(late), "damaged")
    assert zipx.local(zipx.parse_xml(b"<x/>").tag) == "x"


def test_not_a_zip_is_damaged():
    p = tmp() / "n.zip"
    p.write_bytes(b"hello")
    raises_kind(lambda: zipx.Package(p), "damaged")


def test_name_flagged_utf8_that_is_not_is_damaged():
    """zipfile raises a UnicodeDecodeError for a name flagged UTF-8 that is not, from the central directory when
    the package opens or from a local header when its member is read; both are a damaged package."""
    raises_kind(lambda: zipx.Package(bad_name_zip()), "damaged")
    with zipx.Package(bad_name_zip(central=0)) as pkg:  # the central name is read as cp437, the local one as UTF-8
        assert len(pkg.names) == 1, pkg.names
        raises_kind(lambda: pkg.read(pkg.names[0]), "damaged")


def test_ratio_bomb_is_too_large():
    p = make_zip(tmp() / "b.zip", {"big.bin": b"\0" * (3 * 2**20)}, stored=())
    old = limits.ZIP_RATIO
    limits.ZIP_RATIO = 10
    try:
        with zipx.Package(p) as pkg:
            raises_kind(lambda: pkg.read("big.bin"), "too_large")
    finally:
        limits.ZIP_RATIO = old


def test_encrypted_member():
    p = tmp() / "e.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("s.xml", b"<a/>")
        z.getinfo("s.xml").flag_bits |= 1  # writestr resets it, so mark the central entry
    with zipx.Package(p) as pkg:
        raises_kind(lambda: pkg.read("s.xml"), "encrypted")


def test_total_counts_each_member_once():
    p = make_zip(tmp() / "t.zip", {"a": b"x" * 600, "b": b"y" * 600}, stored=())
    old = limits.ZIP_TOTAL
    limits.ZIP_TOTAL = 1000
    try:
        with zipx.Package(p) as pkg:
            pkg.read("a")
            pkg.read("a")
            raises_kind(lambda: pkg.read("b"), "too_large")
    finally:
        limits.ZIP_TOTAL = old


def test_refused_member_keeps_the_total_budget():
    p = make_zip(tmp() / "t.zip", {"a": b"x" * 600, "b": b"y" * 600, "c": b"z" * 300}, stored=())
    old = limits.ZIP_TOTAL
    limits.ZIP_TOTAL = 1000
    try:
        with zipx.Package(p) as pkg:
            pkg.read("a")
            try:
                pkg.read("b")
                raise AssertionError("b read")
            except ReadError as e:
                assert e.kind == "too_large" and e.detail.startswith(limits.BIG_TOTAL + ":"), e
            assert pkg.read("c") == b"z" * 300
    finally:
        limits.ZIP_TOTAL = old


def test_too_large_details_name_the_limit():
    p = make_zip(tmp() / "b.zip", {"big.bin": b"\0" * (3 * 2**20)}, stored=())
    with zipx.Package(p) as pkg:
        try:
            pkg.read("big.bin")
            raise AssertionError("read")
        except ReadError as e:
            assert e.kind == "too_large" and e.detail == limits.BIG_PART + ": big.bin", e
    big = tmp() / "x.hwpx"
    with open(big, "wb") as f:
        f.truncate(limits.MAX_INPUT + 1)
    try:
        reader.precheck(big)
        raise AssertionError("precheck passed")
    except ReadError as e:
        assert e.kind == "too_large" and e.detail.startswith(limits.BIG_INPUT + ":"), e


def test_case_duplicate_names_are_damaged():
    p = make_zip(tmp() / "c.zip", {"A.xml": "<a/>", "a.XML": "<b/>"}, stored=())
    raises_kind(lambda: zipx.Package(p), "damaged")


def test_rels_resolve():
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="t/doc" Target="word/document.xml"/></Relationships>')
    drels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             '<Relationship Id="rId2" Type="t/img" Target="media/a%20b.png"/>'
             '<Relationship Id="rId3" Type="t/link" Target="http://x.test/" TargetMode="External"/>'
             '</Relationships>')
    p = make_zip(tmp() / "r.zip", {"_rels/.rels": rels, "word/_rels/document.xml.rels": drels,
                                   "word/document.xml": "<d/>"}, stored=())
    with zipx.Package(p) as pkg:
        _check_rels(pkg)


def _check_rels(pkg):
    assert pkg.rels("")["rId1"].target == "word/document.xml"
    assert pkg.rels("word/document.xml")["rId2"].target == "word/media/a b.png"
    assert pkg.rels("word/document.xml")["rId3"].external
    assert pkg.rels("nothing/here.xml") == {}
    assert zipx.resolve("a/b.xml", "/c/d.xml") == "c/d.xml"
    assert zipx.resolve("a/b.xml", "../c.xml") == "c.xml"


def test_local_name_helpers():
    el = zipx.parse_xml(b'<p:a xmlns:p="u1" xmlns:r="u2"><p:b r:id="7"/><c><x:b xmlns:x="u3"/></c></p:a>')
    b = zipx.first(el, "b")
    assert zipx.attr(b, "id") == "7" and zipx.attr(b, "zz", "d") == "d"
    assert len(zipx.kids(el, "b")) == 1 and len(zipx.walk(el, "b")) == 2
    assert zipx.first(el, "nope") is None


def test_parse_xml_ns_finds_nested_declarations():
    pad = "<!-- " + "x" * 70_000 + " -->"
    data = (f'<a xmlns:p="urn:one">{pad}<b><c xmlns:q="urn:two" xmlns:p="urn:three"/></b></a>').encode()
    root, ns = zipx.parse_xml_ns(data)
    assert zipx.local(root.tag) == "a" and ns == {"p": {"urn:one", "urn:three"}, "q": {"urn:two"}}
    for bad in (b'<!DOCTYPE a><a/>', '<!DOCTYPE a><a/>'.encode("utf-16"), b'<a><!ENTITY x "y"></a>'):
        try:
            zipx.parse_xml_ns(bad)
            assert False
        except ReadError as e:
            assert e.kind == "damaged"
    try:
        zipx.parse_xml_ns(b"<a>")
        assert False
    except ReadError as e:
        assert e.kind == "damaged"


def _stream_cells(n):
    rows, per = 200, n // 200
    body = "".join(
        f'<row r="{r}">' + "".join(f'<c r="C{i}R{r}" s="{i % 7}"/>' for i in range(per)) + "</row>"
        for r in range(1, rows + 1))
    return make_zip(None, {"s.xml": f"<sheetData>{body}</sheetData>"})


def _scan(n):
    import time
    data = _stream_cells(n)
    t0, cells = time.perf_counter(), 0
    with zipx.Package(data) as pkg, pkg.open("s.xml") as f:
        for _, el in zipx.iterparse(f):
            if el.tag == "c":
                el.get("r"), el.get("s"), el.get("t")
                cells += 1
            el.clear()
    return cells, time.perf_counter() - t0


def test_open_streams_member():
    data = b"<a>" + b"hello" * 1000 + b"</a>"
    with zipx.Package(make_zip(None, {"x.xml": data})) as pkg, pkg.open("x.xml") as f:
        assert f.read() == data


def test_open_checks_like_read():
    raw = make_zip(None, {"s.xml": b"<a/>"})
    p = tmp() / "e.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("s.xml", b"<a/>")
        z.getinfo("s.xml").flag_bits |= 1
    with zipx.Package(p) as pkg:
        raises_kind(lambda: pkg.open("s.xml"), "encrypted")
    old = limits.ZIP_RATIO_FLOOR
    limits.ZIP_RATIO_FLOOR = 0
    try:
        with zipx.Package(make_zip(None, {"z.bin": bytes(4 * 2**20)})) as pkg:
            raises_kind(lambda: pkg.open("z.bin"), "too_large")
    finally:
        limits.ZIP_RATIO_FLOOR = old
    with zipx.Package(raw) as pkg:
        try:
            pkg.open("nope")
            assert False
        except KeyError:
            pass


def test_open_charges_total_once():
    a, b = b"a" * 100, b"b" * 100
    old = limits.ZIP_TOTAL
    limits.ZIP_TOTAL = 100
    try:
        with zipx.Package(make_zip(None, {"a.xml": a, "b.xml": b})) as pkg:
            assert pkg.read("a.xml") == a
            with pkg.open("a.xml") as f:
                assert f.read() == a
            raises_kind(lambda: pkg.open("b.xml"), "too_large")
    finally:
        limits.ZIP_TOTAL = old


def test_open_damaged_stream():
    import random
    rnd = random.Random(1)
    data = bytes(rnd.randrange(256) for _ in range(4000))
    raw = bytearray(make_zip(None, {"d.bin": data}))
    start = 30 + len("d.bin") + 100
    raw[start:start + 20] = bytes(20)
    with zipx.Package(bytes(raw)) as pkg, pkg.open("d.bin") as f:
        raises_kind(lambda: f.read(), "damaged")


def _it(data, **kw):
    import io
    return list(zipx.iterparse(io.BytesIO(data), **kw))


def test_iterparse_events():
    got = [(e, zipx.local(el.tag)) for e, el in _it(b"<a><r>1</r><r>2</r></a>", events=("start", "end"))]
    assert got == [("start", "a"), ("start", "r"), ("end", "r"), ("start", "r"), ("end", "r"), ("end", "a")]


def test_iterparse_doctype():
    raises_kind(lambda: _it(b'<!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>'), "damaged")


def test_iterparse_doctype_utf16():
    text = '<!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>'
    raises_kind(lambda: _it(text.encode("utf-16")), "damaged")
    raises_kind(lambda: _it(text.encode("utf-32-be")), "damaged")


def test_doctype_across_chunks():
    data = b"<?xml ve?>  <!DOCTYPE a><a/>"
    assert data.index(b"<!DOCTYPE") == 12
    raises_kind(lambda: _it(data, chunk=16), "damaged")


def test_doctype_text_after_root():
    got = _it(b"<a>" + b"x" * 64 + b"<![CDATA[<!DOCTYPE]]></a>", chunk=16)
    assert [(e, zipx.local(el.tag)) for e, el in got] == [("end", "a")]


def test_iterparse_bad_xml():
    try:
        _it(b"<a><b></a>")
        assert False
    except ReadError as e:
        assert e.kind == "damaged" and e.detail.startswith("XML parse error"), e.detail


def test_bad_encoding_declaration_is_damaged():
    # mutation case xlsx 27: expat raises LookupError for an unknown encoding name and ValueError (UnicodeError
    # among them) for a multi-byte or unusable one; each is a damaged part, in all three parse paths
    for enc in ("UTFe8", "rot13", "shift_jis", "utf-7", "idna"):
        data = f'<?xml version="1.0" encoding="{enc}"?><a>b</a>'.encode()
        raises_kind(lambda: zipx.parse_xml(data), "damaged")
        raises_kind(lambda: zipx.parse_xml_ns(data), "damaged")
        raises_kind(lambda: _it(data), "damaged")


def test_iterparse_fast():
    cells, secs = _scan(200_000)
    assert cells == 200_000 and secs < 2.0, (cells, secs)


def measure(n=2_000_000):
    cells, secs = _scan(n)
    print(f"SCAN {cells} {secs:.2f}")


def test_package_method():
    data = make_zip(None, {"mimetype": "x" * 50, "a.txt": "y" * 500})
    with zipx.Package(data) as pkg:
        assert pkg.method("mimetype") == 0 and pkg.method("a.txt") == 8
        assert pkg._total == 0
        assert pkg.read("mimetype") == b"x" * 50 and pkg.read("a.txt") == b"y" * 500
        try:
            pkg.method("nope")
        except KeyError:
            pass
        else:
            raise AssertionError("missing member")


def test_gunzip():
    import gzip
    from reader.containers import flate
    assert flate.gunzip(gzip.compress(b"x" * 1000), flate.Budget(), 10_000) == b"x" * 1000
    try:
        flate.gunzip(gzip.compress(b"x" * 1000), flate.Budget(), 100)
    except ReadError as e:
        assert e.kind == "too_large" and e.detail.startswith("part:"), e.detail
    else:
        raise AssertionError("limit")
    good = gzip.compress(bytes(range(256)) * 40)
    try:
        flate.gunzip(good[:10] + bytes([good[10] ^ 0xFF]) + b"\xff" * 40, flate.Budget(), 100_000)
    except ValueError as e:
        assert not isinstance(e, flate.Truncated)
    else:
        raise AssertionError("corrupt")
    try:
        flate.gunzip(good[:len(good) // 2], flate.Budget(), 100_000)
    except flate.Truncated:
        pass
    else:
        raise AssertionError("cut")
    b = flate.Budget()
    flate.gunzip(gzip.compress(b"x" * 1000), b, 10_000)
    assert b.used == 1000


def _detail(fn):
    try:
        fn()
    except ReadError as e:
        assert e.kind == "too_large", e.kind
        return e.detail
    raise AssertionError("ReadError not raised")


def test_failed_inflate_charged():
    # an inflate that fails (past its cap or `limit`, or a corrupt stream) charges the bytes it inflated: small bombs
    # each stopped at the 1 MiB ratio floor spend the document's Budget instead of inflating that much for free
    import gzip
    import random
    import zlib
    from reader.containers import flate
    mib, chunk = 2**20, flate._CHUNK
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    bomb = c.compress(bytes(2 * mib)) + c.flush()  # about 2 KiB that would inflate to 2 MiB
    b = flate.Budget(3 * mib)
    assert _detail(lambda: flate.inflate(bomb, b)).startswith("part:")
    assert mib < b.used <= mib + chunk, b.used
    details = [_detail(lambda: flate.inflate(bomb, b)) for _ in range(3)]
    assert details[0].startswith("part:") and details[2].startswith("total:"), details
    # once a failure has passed the total, a later stream fails before it inflates anything
    spent = b.used
    assert spent > b.total
    assert _detail(lambda: flate.inflate(bomb, b)).startswith("total:") and b.used == spent
    # the caller's limit and gunzip's
    b = flate.Budget()
    assert _detail(lambda: flate.inflate(bomb, b, 1000)).startswith("part:") and 1000 < b.used <= chunk, b.used
    b = flate.Budget()
    assert _detail(lambda: flate.gunzip(gzip.compress(bytes(100_000)), b, 100)).startswith("part:")
    assert 100 < b.used <= chunk, b.used
    # a corrupt stream: the deflate blocks before the bad one are inflated and charged, and the call that fails is
    # charged a whole chunk, the most it can have inflated before zlib dropped its output
    text = bytes(random.Random(5).choice(b"abcdefgh ") for _ in range(200_000))
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    bad = c.compress(text) + c.flush(zlib.Z_FULL_FLUSH) + b"\xff" * 16  # block type 3 does not exist
    b = flate.Budget()
    try:
        flate.inflate(bad, b)
    except ReadError:
        raise
    except ValueError as e:
        assert not isinstance(e, flate.Truncated), e
    else:
        raise AssertionError("corrupt")
    assert len(text) <= b.used <= len(text) + chunk, b.used


def test_corrupt_streams_charged():
    # (Task 25 review) an 85-byte stream of 60 KiB of zeros, a flush and a block of type 3 inflates its 60 KiB inside
    # the one call that fails, whose output zlib drops: each such stream is charged a chunk, so they spend the total
    import gzip
    import zlib
    from reader.containers import flate
    chunk = flate._CHUNK
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    bad = c.compress(bytes(60 * 1024)) + c.flush(zlib.Z_FULL_FLUSH) + b"\xff" * 4
    assert len(bad) < 100, len(bad)
    b = flate.Budget(2 * 2**20)
    kinds = []
    for _ in range(40):
        try:
            flate.inflate(bad, b)
        except ReadError as e:
            kinds.append(e.detail.split(":")[0])
        except ValueError:
            kinds.append("bad")
    assert kinds[0] == "bad" and kinds[-1] == "total" and kinds.count("bad") <= 2 * 2**20 // chunk, kinds
    # gunzip's stream too
    b = flate.Budget()
    gz = gzip.compress(b"")[:10] + bad
    try:
        flate.gunzip(gz, b, 10**6)
    except ValueError:
        pass
    assert b.used >= chunk, b.used
    # a well-formed stream is charged its bytes exactly, in either framing: the failed raw reading of a zlib stream
    # and the zlib reading of a raw one stop at the header, so they cost nothing
    text = bytes(range(256)) * 400
    for packed, fn in ((zlib.compress(text), flate.inflate), (zlib.compress(text, wbits=-15), flate.inflate),
                       (gzip.compress(text), lambda d, bud: flate.gunzip(d, bud, 10**6))):
        for level in (1, 9):
            b = flate.Budget()
            assert fn(packed, b) == text and b.used == len(text), (b.used, level)
    for wbits in (9, 10, 11, 12, 13, 14, 15):  # every zlib window size and level a writer may use
        for level in (0, 1, 6, 9):
            co = zlib.compressobj(level, zlib.DEFLATED, wbits)
            b = flate.Budget()
            assert flate.inflate(co.compress(text) + co.flush(), b) == text and b.used == len(text), (wbits, level)


def test_framing_probe_exemption():
    # (Task 25 re-review) a valid zlib header whose next four bytes pass the stored-length check is read by the raw
    # probe as a stored block and the blocks after it: 78 9C, then LEN 0x009C and NLEN 0xFF63, 156 stored bytes, 60
    # KiB of zeros coded, a flush and a block of type 3. The raw probe inflates the 60 KiB and fails: it is charged.
    # (The stored bytes start 9C 00, so the zlib reading sees a stored block of 0xFF63 bytes and ends Truncated.)
    import zlib
    from reader.containers import flate
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    body = c.compress(bytes(60 * 1024)) + c.flush(zlib.Z_FULL_FLUSH) + b"\xff" * 4
    bad = b"\x78\x9c\x00\x63\xff" + b"\x9c\x00" + bytes(154) + body
    assert len(bad) < 300, len(bad)
    b = flate.Budget(2 * 2**20)
    kinds = []
    for _ in range(40):
        try:
            flate.inflate(bad, b)
        except ReadError as e:
            kinds.append(e.detail.split(":")[0])
        except ValueError:
            kinds.append("bad")
    assert kinds[-1] == "total", (kinds, b.used)
    # the zlib reading of half a raw stream stops at the header: only the raw reading's output is charged
    text = bytes(range(256)) * 400
    raw = zlib.compress(text, wbits=-15)
    half = raw[:len(raw) // 2]
    assert not (half[0] & 0x0F == 8 and (half[0] << 8 | half[1]) % 31 == 0)  # no zlib header by chance
    b = flate.Budget()
    try:
        flate.inflate(half, b)
    except flate.Truncated as e:
        assert 0 < len(e.data) and b.used == len(e.data), (b.used, len(e.data))
    else:
        raise AssertionError("cut")


def main():
    if sys.argv[1:] == ["measure"]:
        measure()
        return
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
