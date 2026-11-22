"""pdf test fixtures: real pdf bytes with true xref offsets, built in memory.

A helper module, not a test file. It puts `scripts/` on sys.path itself, so a caller that
loads it by file path can import `reader`.

    simple_pdf([text_ops("Hi")])                       a one-page pdf
    make_pdf({1: {...}, 2: ...}, xref="stream", objstm=True)
    make_pdf(objects, encrypt=EncryptSpec(4))          # the standard security handler
"""
import hashlib
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from reader.containers.crypto import aes_cbc_encrypt, aes_ecb_encrypt, rc4  # noqa: E402


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


def _stream_bytes(stream, enc=None, num=0):
    data = stream.data
    dictionary = dict(stream.dictionary)
    if stream.flate:
        data = zlib.compress(data)
        dictionary["Filter"] = Name("FlateDecode")
    if enc is not None:
        dictionary = enc.value(dictionary, num)
        if not enc.clear_stream(dictionary):
            data = enc.data(num, data, enc.stream_cipher)
    dictionary.setdefault("Length", len(data))
    return pdf_obj(dictionary) + b"\nstream\n" + data + b"\nendstream"


def _body(value, enc=None, num=0):
    if isinstance(value, Stream):
        return _stream_bytes(value, enc, num)
    return pdf_obj(enc.value(value, num) if enc is not None else value)


_PAD = bytes([
    0x28, 0xBF, 0x4E, 0x5E, 0x4E, 0x75, 0x8A, 0x41, 0x64, 0x00, 0x4E, 0x56, 0xFF, 0xFA, 0x01, 0x08,
    0x2E, 0x2E, 0x00, 0xB6, 0xD0, 0x68, 0x3E, 0x80, 0x2F, 0x0C, 0xA9, 0xFE, 0x64, 0x53, 0x69, 0x7A])
_ID0 = b"phro-fixture-id-0"
_PERMS = 0xFFFFF0C0 - 2**32  # a signed 32-bit /P


class EncryptSpec:
    """The standard security handler for make_pdf(encrypt=...).

    revision 2 (RC4 40-bit), 3 (RC4 128-bit), 4 (V4: AESV2 when aes, else V2) or 6 (AESV3);
    `user` and `owner` are the passwords; metadata=False writes /EncryptMetadata false and
    leaves /Type /Metadata streams clear; `stream_filter="Identity"` (revision 4 or 6) leaves
    every stream clear; `filter` is the /Filter name, and a handler other than Standard
    encrypts nothing.
    """

    def __init__(self, revision, user=b"", owner=b"owner", aes=True, metadata=True,
                 filter="Standard", stream_filter="StdCF"):
        if revision not in (2, 3, 4, 6):
            raise ValueError("revision must be 2, 3, 4 or 6")
        self.revision, self.user, self.owner = revision, bytes(user), bytes(owner)
        self.aes, self.metadata, self.filter = aes, metadata, filter
        self.stream_filter = stream_filter
        self.id0 = _ID0
        self.cipher = "aesv3" if revision == 6 else "aesv2" if revision == 4 and aes else "rc4"
        self.stream_cipher = "identity" if stream_filter == "Identity" else self.cipher
        self.bits = 40 if revision == 2 else 128
        self.key = self._file_key()

    # -- algorithms 2-5 (R2-R4) and 8-10 (R6) --

    def _file_key(self):
        n = self.bits // 8
        if self.revision == 6:
            self.file_key = bytes(range(1, 33))
            self._r6_entries()
            return self.file_key
        pad = lambda pw: (pw + _PAD)[:32]  # noqa: E731
        key = hashlib.md5(pad(self.owner)).digest()
        if self.revision >= 3:
            for _ in range(50):
                key = hashlib.md5(key).digest()
        okey = key[:n]
        o = rc4(okey, pad(self.user))
        if self.revision >= 3:
            for i in range(1, 20):
                o = rc4(bytes(b ^ i for b in okey), o)
        self.o = o
        md = hashlib.md5(pad(self.user) + o + struct.pack("<i", _PERMS) + self.id0)
        if self.revision >= 4 and not self.metadata:
            md.update(b"\xff\xff\xff\xff")
        key = md.digest()
        if self.revision >= 3:
            for _ in range(50):
                key = hashlib.md5(key[:n]).digest()
        key = key[:n]
        if self.revision == 2:
            self.u = rc4(key, _PAD)
        else:
            u = rc4(key, hashlib.md5(_PAD + self.id0).digest())
            for i in range(1, 20):
                u = rc4(bytes(b ^ i for b in key), u)
            self.u = u + b"\0" * 16
        return key

    @staticmethod
    def _hash2b(password, salt, udata):
        k = hashlib.sha256(password + salt + udata).digest()
        i = 0
        while True:
            e = aes_cbc_encrypt(k[:16], k[16:32], (password + k + udata) * 64)
            k = (hashlib.sha256, hashlib.sha384,
                 hashlib.sha512)[int.from_bytes(e[:16], "big") % 3](e).digest()
            i += 1
            if i >= 64 and i >= e[-1] + 32:
                return k[:32]

    def _r6_entries(self):
        user, owner = self.user[:127], self.owner[:127]
        uv, uk, ov, ok = b"uvalidat", b"ukeysalt", b"ovalidat", b"okeysalt"
        self.u = self._hash2b(user, uv, b"") + uv + uk
        self.ue = aes_cbc_encrypt(self._hash2b(user, uk, b""), b"\0" * 16, self.file_key)
        self.o = self._hash2b(owner, ov, self.u) + ov + ok
        self.oe = aes_cbc_encrypt(self._hash2b(owner, ok, self.u), b"\0" * 16, self.file_key)
        perms = (struct.pack("<i", _PERMS) + b"\xff\xff\xff\xff"
                 + (b"T" if self.metadata else b"F") + b"adb" + b"perm")
        self.perms = aes_ecb_encrypt(self.file_key, perms)

    # -- the /Encrypt dictionary --

    def dictionary(self):
        d = {"Filter": Name(self.filter), "P": _PERMS, "R": self.revision}
        if self.revision == 2:
            d.update(V=1, Length=40)
        elif self.revision == 3:
            d.update(V=2, Length=128)
        elif self.revision == 4:
            cfm = "AESV2" if self.aes else "V2"
            d.update(V=4, Length=128, StmF=Name(self.stream_filter), StrF=Name("StdCF"),
                     CF={"StdCF": {"CFM": Name(cfm), "AuthEvent": Name("DocOpen"), "Length": 16}})
        else:
            d.update(V=5, Length=256, StmF=Name(self.stream_filter), StrF=Name("StdCF"),
                     CF={"StdCF": {"CFM": Name("AESV3"), "AuthEvent": Name("DocOpen"),
                                   "Length": 32}},
                     OE=self.oe, UE=self.ue, Perms=self.perms)
        if not self.metadata:
            d["EncryptMetadata"] = False
        d.update(O=self.o, U=self.u)
        return d

    # -- encrypting objects (algorithm 1) --

    def data(self, num, data, cipher, gen=0):
        if cipher == "identity" or not data or self.filter != "Standard":
            return data
        if cipher == "aesv3":
            key = self.key
        else:
            md = hashlib.md5(self.key + struct.pack("<I", num)[:3] + struct.pack("<I", gen)[:2]
                             + (b"sAlT" if cipher == "aesv2" else b""))
            key = md.digest()[:min(len(self.key) + 5, 16)]
        if cipher == "rc4":
            return rc4(key, data)
        iv = hashlib.md5(b"iv" + data + struct.pack("<I", num)).digest()
        pad = 16 - len(data) % 16
        return iv + aes_cbc_encrypt(key, iv, data + bytes([pad]) * pad)

    def value(self, value, num):
        """`value` with every string encrypted for object `num` (names and references stay)."""
        if isinstance(value, (Name, Ref)):
            return value
        if isinstance(value, str):
            try:
                raw = value.encode("ascii")
            except UnicodeEncodeError:
                raw = b"\xfe\xff" + value.encode("utf-16-be")
            return self.data(num, raw, self.cipher)
        if isinstance(value, (bytes, bytearray)):
            return self.data(num, bytes(value), self.cipher)
        if isinstance(value, dict):
            return {k: self.value(v, num) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(v, num) for v in value]
        return value

    def clear_stream(self, dictionary):
        if not self.metadata and str(dictionary.get("Type", "")) == "Metadata":
            return True
        filters, parms = dictionary.get("Filter"), dictionary.get("DecodeParms")
        for i, name in enumerate(filters if isinstance(filters, list) else []):
            if str(name) == "Crypt":  # the stream's own crypt filter, Identity here
                parm = parms[i] if isinstance(parms, list) and i < len(parms) else {}
                return str((parm or {}).get("Name", "Identity")) == "Identity"
        return False


def make_pdf(objects, *, root=1, xref="table", objstm=False, trailer=None,
             header=b"%PDF-1.7", junk=b"", shift=0, encrypt=None):
    """Serialize `objects` ({number: value}) to a pdf.

    xref: "table" (classic), "stream" (a cross-reference stream) or "none" (no xref and no
    trailer, so a reader must scan). Offsets are true and count from the header (readers try
    that first; ISO 32000 counts from byte 0, which differs only when `junk` precedes the
    header) unless `shift` moves every xref entry by that many bytes. `junk` goes before
    the header. With objstm=True every object other than streams and the root goes into one
    object stream; that needs xref="stream". With encrypt=EncryptSpec(...) every string and
    stream is encrypted (objects inside the object stream are not: the stream is), /Encrypt
    and /ID go in the trailer, and the xref stream stays clear.
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
    extra = dict(trailer or {})
    if encrypt is not None:
        enc_num = next_num
        next_num += 1
        extra.update(Encrypt=Ref(enc_num), ID=[encrypt.id0, encrypt.id0])
        offsets[enc_num] = len(out)  # the /Encrypt dictionary is written in the clear
        out += b"%d 0 obj\n" % enc_num + pdf_obj(encrypt.dictionary()) + b"\nendobj\n"
    for num in sorted(objects):
        if num in packed:
            continue
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + _body(objects[num], encrypt, num) + b"\nendobj\n"
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
        out += b"%d 0 obj\n" % stm_num + _stream_bytes(stm, encrypt, stm_num) + b"\nendobj\n"
    size = next_num
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
               page_extra=None, extra_objects=None, **kw):
    """A pdf with one page per content stream in `pages`.

    Object 1 is the catalog, 2 the page tree (it holds /Resources and /MediaBox for the pages
    to inherit), 3 the Helvetica font F1; then each page and its content stream. page_extra is
    a dict merged into every page, or a list of dicts, one per page. extra_objects is a dict
    of further objects (font_type0 gives some, from number 100 up).
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
    objects.update(extra_objects or {})
    objects[2] = {"Type": Name("Pages"), "Kids": kids, "Count": len(kids),
                  "Resources": resources, "MediaBox": list(media)}
    return make_pdf(objects, root=1, **kw)


def _hex_utf16(text):
    return text.encode("utf-16-be").hex().upper()


def font_type0(name="F2", *, tounicode, ordering="Korea1", embedded_cmap=None, vertical=False):
    """The objects of an Identity-H (or Identity-V) Type0 font, as {number: object}.

    The Type0 font is object 100 (`name` is its BaseFont), its CIDFontType2 descendant 101 with
    CIDSystemInfo Adobe/`ordering`, the FontDescriptor 102, the ToUnicode stream 103 (when
    `tounicode`, a {code: text} dict, is not None, written as bfchar entries) and the
    embedded font stream 104 (when `embedded_cmap`, a {gid: code} dict, is not None, built by
    truetype_cmap_font). Pass extra_objects=font_type0(...) to simple_pdf with
    resources={"Font": {"F2": Ref(100)}}; tests edit the returned dicts to vary the font.
    """
    objects = {
        100: {"Type": Name("Font"), "Subtype": Name("Type0"), "BaseFont": Name(name),
              "Encoding": Name("Identity-V" if vertical else "Identity-H"),
              "DescendantFonts": [Ref(101)]},
        101: {"Type": Name("Font"), "Subtype": Name("CIDFontType2"), "BaseFont": Name(name),
              "CIDSystemInfo": {"Registry": b"Adobe", "Ordering": ordering.encode("ascii"),
                                "Supplement": 0},
              "DW": 1000},
    }
    if embedded_cmap is not None:
        objects[101]["FontDescriptor"] = Ref(102)
        objects[102] = {"Type": Name("FontDescriptor"), "FontName": Name(name), "Flags": 4,
                        "FontFile2": Ref(104)}
        objects[104] = Stream({}, truetype_cmap_font(embedded_cmap), flate=True)
    if tounicode is not None:
        entries = "".join("<%04X> <%s>\n" % (code, _hex_utf16(text))
                          for code, text in sorted(tounicode.items()))
        cmap = ("/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
                "1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
                "%d beginbfchar\n%sendbfchar\nendcmap end end\n" % (len(tounicode), entries))
        objects[100]["ToUnicode"] = Ref(103)
        objects[103] = Stream({}, cmap.encode("ascii"))
    return objects


def truetype_cmap_font(gid_to_code):
    """A minimal sfnt with a `head` table and a Unicode (3, 1) format 4 `cmap` that maps each
    code in `gid_to_code.values()` to its glyph id (one segment per glyph)."""
    pairs = sorted((code, gid) for gid, code in gid_to_code.items())
    segments = [(code, code, (gid - code) & 0xFFFF) for code, gid in pairs] + [(0xFFFF, 0xFFFF, 1)]
    count = len(segments)
    sub = struct.pack(">HHHHHHH", 4, 16 + 8 * count, 0, 2 * count, 0, 0, 0)
    sub += b"".join(struct.pack(">H", end) for _s, end, _d in segments) + b"\0\0"
    sub += b"".join(struct.pack(">H", start) for start, _e, _d in segments)
    sub += b"".join(struct.pack(">H", delta) for _s, _e, delta in segments)
    sub += b"\0\0" * count
    cmap = struct.pack(">HHHHI", 0, 1, 3, 1, 12) + sub
    head = bytes(18) + struct.pack(">H", 1000) + bytes(34)
    tables = [(b"cmap", cmap), (b"head", head)]
    out = bytearray(struct.pack(">IHHHH", 0x00010000, len(tables), 0, 0, 0))
    offset = 12 + 16 * len(tables)
    for tag, body in tables:
        out += tag + struct.pack(">III", 0, offset, len(body))
        offset += (len(body) + 3) // 4 * 4
    for _tag, body in tables:
        out += body + bytes(-len(body) % 4)
    return bytes(out)


def ocg_catalog(off, on=()):
    """Catalog entries for optional content: `OCProperties` listing the groups with the given
    object numbers, those in `off` switched off in the default configuration and those in `on`
    on. Pass it as simple_pdf(catalog=...); the tests define the group objects themselves."""
    return {"OCProperties": {"OCGs": [Ref(n) for n in (*off, *on)],
                             "D": {"BaseState": Name("ON"), "ON": [Ref(n) for n in on],
                                   "OFF": [Ref(n) for n in off]}}}


def form_xobject(content, *, oc=None, matrix=None):
    """A Form XObject stream with a 612 x 792 /BBox and no /Resources of its own (it uses the
    caller's). `oc` is the object number (or Ref) of a group or membership dictionary."""
    d = {"Type": Name("XObject"), "Subtype": Name("Form"), "BBox": [0, 0, 612, 792]}
    if matrix is not None:
        d["Matrix"] = list(matrix)
    if oc is not None:
        d["OC"] = oc if isinstance(oc, Ref) else Ref(oc)
    return Stream(d, content)


def rule_ops(x0, y0, x1, y1, w=0.5):
    """A stroked line from (x0, y0) to (x1, y1) with line width `w`."""
    return b"%s w %s %s m %s %s l S" % tuple(_num(v).encode() for v in (w, x0, y0, x1, y1))


def bar_chart():
    """A bar chart: a filled plot area, six bars, six grid rules and short labels inside it, with a paragraph above and
    below. Seven filled paths, under FIGURE_PATHS, but they and the rules draw 34 pieces."""
    plot = b"0.9 g 100 280 400 220 re f "
    bars = b"".join(b"0.2 g %d 300 30 %d re f " % (130 + 60 * i, 40 + 25 * i) for i in range(6))
    grid = b" ".join(rule_ops(100, 300 + 40 * k, 500, 300 + 40 * k) for k in range(6))
    labels = b" ".join(text_ops(str(40 * k), x=104, y=302 + 40 * k, size=7) for k in range(5))
    labels += b" " + b" ".join(text_ops("Q%d" % (i + 1), x=135 + 60 * i, y=285, size=7) for i in range(6))
    return (text_ops("Sales rose in every quarter of the year, as the chart below shows.", y=700) + b" "
            + plot + bars + b"0 g " + grid + b" " + labels + b" "
            + text_ops("The next paragraph follows the chart and is body text.", y=240))


# -- annotations, form fields, outlines and images --

def embedded_file(num, name, data=b"abc", kind="text/plain", **spec):
    """{num: a file specification named `name`, num + 1: its embedded file of `data`}, the file's
    /Subtype `kind` and /Params /Size its length; `spec` adds keys to the specification."""
    stream = Stream({"Type": Name("EmbeddedFile"), "Subtype": Name(kind), "Params": {"Size": len(data)}}, data)
    return {num: {"Type": Name("Filespec"), "F": name, "UF": name, "EF": {"F": Ref(num + 1)}, **spec},
            num + 1: stream}


def nav_pdf():
    """Three pages labelled i, ii, 1 (/PageLabels); page 1's outline has "Intro", matching its first line, and
    "Second part", matching none, its destination's top (/XYZ 650) between its two body lines; page 2's outline
    item "Appendix" is a /Fit view."""
    def views(objects):
        objects[302]["Dest"] = [Ref(4), Name("XYZ"), 0, 650, 0]

    items = outline_items([(1, "Intro", 0), (1, "Second part", 0), (1, "Appendix", 1)])
    views(items)
    labels = {"Nums": [0, {"S": Name("r")}, 2, {"S": Name("D")}]}
    page1 = b" ".join([text_ops("Intro", y=720), text_ops("Body line one.", y=700), text_ops("Body line two.", y=600)])
    return simple_pdf([page1, text_ops("Page two text.", y=700), text_ops("Page three text.", y=700)],
                      catalog={"Outlines": Ref(300), "PageLabels": labels}, extra_objects=items)


NAV_HEADS = ["## Page 1: p. i", "### Intro", "### Second part", "## Page 2: p. ii", "### Appendix", "## Page 3: p. 1"]


def link_annot(rect, uri):
    """A /Link annotation whose action is a /URI."""
    return {"Type": Name("Annot"), "Subtype": Name("Link"), "Rect": list(rect),
            "A": {"S": Name("URI"), "URI": uri}}


def text_annot(rect, contents, *, author=None, subtype="Text", irt=None, flags=0):
    """A markup annotation with /Contents; `irt` is the parent's Ref (a reply), `flags` is /F."""
    d = {"Type": Name("Annot"), "Subtype": Name(subtype), "Rect": list(rect), "Contents": contents}
    if author is not None:
        d["T"] = author
    if irt is not None:
        d["IRT"] = irt
    if flags:
        d["F"] = flags
    return d


def popup(parent):
    """The /Popup annotation of `parent` (a Ref)."""
    return {"Type": Name("Annot"), "Subtype": Name("Popup"), "Rect": [100, 100, 200, 200],
            "Parent": parent}


def widget(rect, page_ref, **field):
    """A /Widget annotation that is also a form field: `field` holds T, TU, V, FT, Ff and so on.
    page_ref None leaves out /P."""
    d = {"Type": Name("Annot"), "Subtype": Name("Widget"), "Rect": list(rect)}
    if page_ref is not None:
        d["P"] = page_ref
    d.update(field)
    return d


def acroform(fields, *, first=200):
    """The objects of an AcroForm: {number: object}. Object `first` is the AcroForm dictionary
    (use catalog={"AcroForm": Ref(200)}); `fields` follow from first + 1 in depth-first order.
    A field dict's /Kids may hold dicts, which become objects of their own with /Parent set; a
    Ref stays as it is. Pass the objects as extra_objects."""
    objects = {}
    counter = [first]

    def add(field, parent):
        counter[0] += 1
        num = counter[0]
        field = dict(field)
        if parent is not None:
            field["Parent"] = Ref(parent)
        objects[num] = field
        kids = field.get("Kids")
        if kids:
            field["Kids"] = [Ref(add(k, num)) if isinstance(k, dict) else k for k in kids]
        return num

    refs = [Ref(add(f, None)) for f in fields]
    objects[first] = {"Fields": refs}
    return objects


def outline_items(items, *, first=300):
    """The objects of an outline: {number: object}. Object `first` is the /Outlines root and the
    items follow in order. `items` is [(depth from 1, title, 0-based page)] and each item's
    /Dest points at the page object simple_pdf makes (4 + 2 * page)."""
    objects = {first: {"Type": Name("Outlines")}}
    parents = {0: first}
    last = {}
    for i, (depth, title, page) in enumerate(items):
        num = first + 1 + i
        parent = parents[depth - 1]
        objects[num] = {"Title": title, "Parent": Ref(parent),
                        "Dest": [Ref(4 + 2 * page), Name("Fit")]}
        if parent in last:
            objects[last[parent]]["Next"] = Ref(num)
            objects[num]["Prev"] = Ref(last[parent])
        else:
            objects[parent]["First"] = Ref(num)
        last[parent] = num
        objects[parent]["Last"] = Ref(num)
        parents[depth] = num
        for deeper in [d for d in parents if d > depth]:
            del parents[deeper]
    return objects


def image_xobject(w, h, *, cs="DeviceGray", bpc=8, data=None, filter="FlateDecode",
                  palette=None, parms=None):
    """An image XObject stream. `cs` is a colour space name, or "Indexed" (over DeviceRGB, with
    `palette` bytes), or a ready list. `data` defaults to zero samples. filter "FlateDecode"
    compresses the data, None leaves it as is, any other name only labels it (DCTDecode,
    JPXDecode, CCITTFaxDecode, JBIG2Decode). `parms` becomes /DecodeParms."""
    if cs == "Indexed":
        space = [Name("Indexed"), Name("DeviceRGB"), len(palette) // 3 - 1, bytes(palette)]
    elif isinstance(cs, str):
        space = Name(cs)
    else:
        space = cs
    comps = {"DeviceGray": 1, "DeviceRGB": 3, "DeviceCMYK": 4}.get(cs, 1) if isinstance(cs, str) else 1
    if data is None:
        data = bytes(((w * comps * bpc + 7) // 8) * h)
    d = {"Type": Name("XObject"), "Subtype": Name("Image"), "Width": w, "Height": h,
         "ColorSpace": space, "BitsPerComponent": bpc}
    if parms is not None:
        d["DecodeParms"] = parms
    if filter == "FlateDecode":
        return Stream(d, data, flate=True)
    if filter is not None:
        d["Filter"] = Name(filter)
    return Stream(d, data)


# An 8 x 8 baseline grey JPEG (JFIF, one DQT, SOF0, the standard DHT tables, SOS, EOI), made once
# with an encoder and kept as bytes: the tests only need a stream that starts ff d8 ff.
JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300100b0c0e0c0a100e0d0e1211101318281a1816"
    "16183123251d283a333d3c3933383740485c4e404457453738506d51575f626768673e4d71797064785c6567"
    "63ffc0000b080008000801011100ffc4001f0000010501010101010100000000000000000102030405060708"
    "090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114"
    "328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748"
    "494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2"
    "a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9"
    "eaf1f2f3f4f5f6f7f8f9faffda0008010100003f002bffd9")
# JPEG at 64 x 64, at least OCR_MIN_EDGE: its frame header's size set, and 64 blocks of DC difference 0 then end of
# block (bits 00 1010 each, no byte padding between them), a flat grey that GDI+ decodes
JPEG64 = (JPEG[:JPEG.index(b"\xff\xc0") + 5] + bytes([0, 64, 0, 64])
          + JPEG[JPEG.index(b"\xff\xc0") + 9:JPEG.index(b"\xff\xda") + 10] + bytes.fromhex("28a28a") * 16 + b"\xff\xd9")


def _marked(tag, mcid, ops, actual=None):
    props = b"<< /MCID %d%s >>" % (mcid, b"" if actual is None else b" /ActualText (" + actual + b")")
    return b"/" + tag + b" " + props + b" BDC " + ops + b" EMC "


def struct_tree(elements, owners, **root):
    """A structure tree (ISO 32000-1 14.7) as objects 600 up: the root at 600, its kids the
    elements whose parent is 600, its /ParentTree at 699 giving page /StructParents 0 the
    elements `owners` lists by MCID (None for an id no element owns). `elements` is
    {num: (type, parent num, /K, optional extra entries)}."""
    out = {600: dict({"Type": Name("StructTreeRoot"), "ParentTree": Ref(699),
                      "K": [Ref(n) for n, e in elements.items() if e[1] == 600]}, **root),
           699: {"Nums": [0, [None if n is None else Ref(n) for n in owners]]}}
    for n, (kind, parent, kids, *more) in elements.items():
        out[n] = dict({"Type": Name("StructElem"), "S": Name(kind), "P": Ref(parent), "Pg": Ref(4), "K": kids},
                      **(more[0] if more else {}))
    return out


def tagged_pdf(marked=True, ruled=False):
    """One tagged page (ISO 32000-1 14.8): an /Artifact running head; a heading of the custom type
    Heading1, which /RoleMap maps to H1; a paragraph over two lines; a list of two items with Lbl
    and LBody; a table with a TH row and a TD row; a Figure with /Alt drawing an image; a
    paragraph whose marked content has /ActualText; and one whose structure element has it.
    `marked` False drops /MarkInfo: the file is then not tagged and reads by its layout. `ruled`
    draws the table's grid."""
    def line(text, x, y, size=12):
        return b"BT /F1 %d Tf %d %d Td (%s) Tj ET" % (size, x, y, text)

    ops = b" ".join([
        b"/Artifact BMC " + line(b"Running head of the report", 72, 760, 9) + b" EMC",
        _marked(b"Heading1", 0, line(b"Annual report", 72, 720, 18)),
        _marked(b"P", 1, line(b"Body text that runs", 72, 690) + b" " + line(b"over two lines.", 72, 676)),
        _marked(b"Lbl", 2, line(b"1.", 72, 650)), _marked(b"LBody", 3, line(b"First item", 90, 650)),
        _marked(b"Lbl", 4, line(b"2.", 72, 636)), _marked(b"LBody", 5, line(b"Second item", 90, 636)),
        _marked(b"TH", 6, line(b"Name", 72, 600)), _marked(b"TH", 7, line(b"Score", 200, 600)),
        _marked(b"TD", 8, line(b"Kim", 72, 586)), _marked(b"TD", 9, line(b"90", 200, 586)),
        _marked(b"Figure", 10, b"q 100 0 0 50 72 500 cm /Im1 Do Q"),
        _marked(b"P", 11, line(b"N o t e", 72, 470), actual=b"Note text"),
        _marked(b"P", 12, line(b"x-y", 72, 450)),
    ] + ([b"0.5 w 66 612 m 260 612 l 66 596 m 260 596 l 66 582 m 260 582 l "
          b"66 582 m 66 612 l 130 582 m 130 612 l 190 582 m 190 612 l 260 582 m 260 612 l S"] if ruled else []))

    tree = struct_tree({
        601: ("Document", 600, [Ref(602), Ref(603), Ref(604), Ref(610), Ref(620), Ref(621), Ref(622)]),
        602: ("Heading1", 601, 0), 603: ("P", 601, 1),
        604: ("L", 601, [Ref(605), Ref(606)]),
        605: ("LI", 604, [Ref(607), Ref(608)]), 607: ("Lbl", 605, 2), 608: ("LBody", 605, 3),
        606: ("LI", 604, [Ref(609), Ref(611)]), 609: ("Lbl", 606, 4), 611: ("LBody", 606, 5),
        610: ("Table", 601, [Ref(612), Ref(613)]),
        612: ("TR", 610, [Ref(614), Ref(615)]), 614: ("TH", 612, 6), 615: ("TH", 612, 7),
        613: ("TR", 610, [Ref(616), Ref(617)]), 616: ("TD", 613, 8), 617: ("TD", 613, 9),
        620: ("Figure", 601, [{"Type": Name("MCR"), "MCID": 10, "Pg": Ref(4)}], {"Alt": "A bar chart of scores"}),
        621: ("P", 601, 11), 622: ("P", 601, 12, {"ActualText": "x minus y"}),
    }, [602, 603, 607, 608, 609, 611, 614, 615, 616, 617, 620, 621, 622], RoleMap={"Heading1": Name("H1")})
    picture = image_xobject(32, 32, data=bytes((x * 8 + y) % 256 for x in range(32) for y in range(32)))
    catalog = {"StructTreeRoot": Ref(600)}
    if marked:
        catalog["MarkInfo"] = {"Marked": True}
    return simple_pdf([ops], resources={"Font": {"F1": Ref(3)}, "XObject": {"Im1": Ref(30)}},
                      catalog=catalog, page_extra={"StructParents": 0},
                      extra_objects={**tree, 30: picture})


TAGGED_MD = ["Header: Running head of the report", "## Page 1", "### Annual report", "Body text that runs over two lines.", "1. First item",
             "2. Second item", "|Name|Score|", "|Kim|90|", "![A bar chart of scores](assets/img-001.png)",
             "Note text", "x minus y"]


def fake_venv(dir, *, build=7000):
    """A fake READY pdf venv, laid out like the real wheel: an interpreter file, pyvenv.cfg with an existing home,
    ONE pypdfium2 dist-info whose RECORD lists a pypdfium2 file, a pypdfium2_raw file and a ../../Scripts file
    with their digests, and ready.json (current requirements hash, 5.15.2, build, the RECORD digest)."""
    import base64
    import json
    from reader import pyenv
    d = Path(dir)
    d.mkdir(parents=True, exist_ok=True)
    py = pyenv.interpreter(d)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_bytes(b"")
    (d / "pyvenv.cfg").write_text(f"home = {d}\nversion = 3.12.0\n", encoding="utf-8")
    site = d / "Lib" / "site-packages"
    lines = []
    for rel in ("pypdfium2/__init__.py", "pypdfium2_raw/__init__.py", "../../Scripts/pypdfium2-fake.exe"):
        f = site / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        data = f"# {rel}\n".encode()
        f.write_bytes(data)
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        lines.append(f"{rel},sha256={digest},{len(data)}\n")
    info = site / "pypdfium2-5.15.2.dist-info"
    info.mkdir(exist_ok=True)
    lines.append("pypdfium2-5.15.2.dist-info/RECORD,,\n")
    rec = info / "RECORD"
    rec.write_bytes("".join(lines).encode())
    ready = {"requirements": pyenv.requirements_hash(), "python": {"version": "3.12.0", "home": str(d)},
             "pypdfium2": "5.15.2", "pdfium": build,
             "records": {"pypdfium2": hashlib.sha256(rec.read_bytes()).hexdigest()}}
    (d / "ready.json").write_text(json.dumps(ready), encoding="utf-8")
    return d
