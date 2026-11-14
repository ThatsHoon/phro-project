"""pdf test fixtures: real pdf bytes with true xref offsets, built in memory.

A helper module, not a test file. It puts `scripts/` on sys.path itself, so a caller that
loads it by file path can import `reader`.

    simple_pdf([text_ops("Hi")])                       a one-page pdf
    make_pdf({1: {...}, 2: ...}, xref="stream", objstm=True)
"""
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class Name(str):
    """A pdf name: written as `/Name`."""


class Ref:
    """An indirect reference: written as `num gen R`."""

    def __init__(self, num, gen=0):
        self.num = num
        self.gen = gen

    def __repr__(self):
        return f"Ref({self.num}, {self.gen})"


class Stream:
    """A stream object: a dictionary and its data. flate=True compresses the data and sets
    /Filter /FlateDecode. /Length is the written length unless the dictionary names one."""

    def __init__(self, dictionary, data, flate=False):
        self.dictionary = dict(dictionary)
        self.data = data
        self.flate = flate


def _num(value):
    if isinstance(value, float):
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text if text not in ("", "-") else "0"
    return str(value)


def _name(text):
    out = bytearray(b"/")
    for byte in str(text).encode("utf-8"):
        if byte <= 0x20 or byte >= 0x7F or byte in b"()<>[]{}/%#":
            out += b"#%02X" % byte
        else:
            out.append(byte)
    return bytes(out)


def _literal(text):
    try:
        raw = text.encode("ascii")
    except UnicodeEncodeError:
        raw = b"\xfe\xff" + text.encode("utf-16-be")
    out = bytearray(b"(")
    for byte in raw:
        if byte in b"\\()":
            out += b"\\"
        out.append(byte)
    return bytes(out + b")")


def pdf_obj(value):
    """Serialize a value: dict, list, Name, Ref, str (a literal), bytes (hex), int, float,
    bool and None."""
    if value is None:
        return b"null"
    if isinstance(value, bool):
        return b"true" if value else b"false"
    if isinstance(value, Name):
        return _name(value)
    if isinstance(value, Ref):
        return b"%d %d R" % (value.num, value.gen)
    if isinstance(value, (int, float)):
        return _num(value).encode("ascii")
    if isinstance(value, str):
        return _literal(value)
    if isinstance(value, (bytes, bytearray)):
        return b"<" + bytes(value).hex().upper().encode("ascii") + b">"
    if isinstance(value, dict):
        return b"<< " + b" ".join(_name(k) + b" " + pdf_obj(v) for k, v in value.items()) + b" >>"
    if isinstance(value, (list, tuple)):
        return b"[" + b" ".join(pdf_obj(v) for v in value) + b"]"
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _stream_bytes(stream):
    data = stream.data
    dictionary = dict(stream.dictionary)
    if stream.flate:
        data = zlib.compress(data)
        dictionary["Filter"] = Name("FlateDecode")
    dictionary.setdefault("Length", len(data))
    return pdf_obj(dictionary) + b"\nstream\n" + data + b"\nendstream"


def _body(value):
    return _stream_bytes(value) if isinstance(value, Stream) else pdf_obj(value)


def make_pdf(objects, *, root=1, xref="table", objstm=False, trailer=None,
             header=b"%PDF-1.7", junk=b"", shift=0):
    """Serialize `objects` ({number: value}) to a pdf.

    xref: "table" (classic), "stream" (a cross-reference stream) or "none" (no xref and no
    trailer, so a reader must scan). Offsets are true and count from the header (readers try
    that first; ISO 32000 counts from byte 0, which differs only when `junk` precedes the
    header) unless `shift` moves every xref entry by that many bytes. `junk` goes before
    the header. With objstm=True every object other than streams and the root goes into one
    object stream; that needs xref="stream".
    """
    if objstm and xref != "stream":
        raise ValueError("objstm=True needs xref='stream'")
    objects = dict(objects)
    next_num = max(objects, default=0) + 1
    packed = []
    if objstm:
        packed = [n for n in sorted(objects) if n != root and not isinstance(objects[n], Stream)]
    out = bytearray(header + b"\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for num in sorted(objects):
        if num in packed:
            continue
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + _body(objects[num]) + b"\nendobj\n"
    compressed = {}
    if packed:
        stm_num = next_num
        next_num += 1
        bodies, head, pos = [], [], 0
        for index, num in enumerate(packed):
            body = pdf_obj(objects[num])
            head.append(b"%d %d" % (num, pos))
            bodies.append(body)
            pos += len(body) + 1
            compressed[num] = (stm_num, index)
        head_bytes = b" ".join(head) + b"\n"
        stm = Stream({"Type": Name("ObjStm"), "N": len(packed), "First": len(head_bytes)},
                     head_bytes + b"\n".join(bodies), flate=True)
        offsets[stm_num] = len(out)
        out += b"%d 0 obj\n" % stm_num + _stream_bytes(stm) + b"\nendobj\n"
    size = next_num
    extra = dict(trailer or {})
    if xref == "table":
        start = len(out)
        entries = [b"0000000000 65535 f \n"]
        for num in range(1, size):
            if num in offsets:
                entries.append(b"%010d 00000 n \n" % (offsets[num] + shift))
            else:
                entries.append(b"0000000000 00000 f \n")
        out += b"xref\n0 %d\n" % size + b"".join(entries)
        tr = {"Size": size, "Root": Ref(root)}
        tr.update(extra)
        out += b"trailer\n" + pdf_obj(tr) + b"\nstartxref\n%d\n%%%%EOF\n" % start
    elif xref == "stream":
        xnum = size
        size += 1
        start = len(out)
        rows = [b"\x00" + (0).to_bytes(4, "big") + (65535).to_bytes(2, "big")]
        for num in range(1, size):
            if num in compressed:
                stm_num, index = compressed[num]
                rows.append(b"\x02" + stm_num.to_bytes(4, "big") + index.to_bytes(2, "big"))
            elif num in offsets:
                rows.append(b"\x01" + (offsets[num] + shift).to_bytes(4, "big") + b"\x00\x00")
            elif num == xnum:
                rows.append(b"\x01" + start.to_bytes(4, "big") + b"\x00\x00")
            else:
                rows.append(b"\x00" + (0).to_bytes(4, "big") + b"\x00\x00")
        tr = {"Type": Name("XRef"), "Size": size, "Root": Ref(root), "W": [1, 4, 2],
              "Index": [0, size]}
        tr.update(extra)
        xs = Stream(tr, b"".join(rows), flate=True)
        out += b"%d 0 obj\n" % xnum + _stream_bytes(xs) + b"\nendobj\n"
        out += b"startxref\n%d\n%%%%EOF\n" % start
    elif xref == "none":
        out += b"%%EOF\n"
    else:
        raise ValueError(f"unknown xref kind {xref!r}")
    return bytes(junk) + bytes(out)


def text_ops(text, x=72, y=720, size=12, font="F1", extra=b""):
    """`BT /F1 12 Tf 72 720 Td (text) Tj ET`; `extra` operators go after Tf."""
    if isinstance(text, str):
        text = text.encode("latin-1", "replace")
    escaped = bytes(text).replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
    tail = (b" " + extra) if extra else b""
    return (b"BT /%s %s Tf%s %s %s Td (%s) Tj ET"
            % (font.encode("ascii"), _num(size).encode(), tail, _num(x).encode(),
               _num(y).encode(), escaped))


def simple_pdf(pages, *, resources=None, media=(0, 0, 612, 792), catalog=None,
               page_extra=None, **kw):
    """A pdf with one page per content stream in `pages`.

    Object 1 is the catalog, 2 the page tree (it holds /Resources and /MediaBox for the pages
    to inherit), 3 the Helvetica font F1; then each page and its content stream. page_extra is
    a dict merged into every page, or a list of dicts, one per page.
    """
    if resources is None:
        resources = {"Font": {"F1": Ref(3)}}
    objects = {
        1: dict({"Type": Name("Catalog"), "Pages": Ref(2)}, **(catalog or {})),
        3: {"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name("Helvetica"),
            "Encoding": Name("WinAnsiEncoding")},
    }
    kids = []
    for i, content in enumerate(pages):
        page_num, content_num = 4 + 2 * i, 5 + 2 * i
        page = {"Type": Name("Page"), "Parent": Ref(2), "Contents": Ref(content_num)}
        if isinstance(page_extra, dict):
            page.update(page_extra)
        elif page_extra:
            page.update(page_extra[i])
        objects[page_num] = page
        objects[content_num] = Stream({}, content)
        kids.append(Ref(page_num))
    objects[2] = {"Type": Name("Pages"), "Kids": kids, "Count": len(kids),
                  "Resources": resources, "MediaBox": list(media)}
    return make_pdf(objects, root=1, **kw)
