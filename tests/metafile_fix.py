"""In-memory WMF builders for the metafile tests (EMF, EMF+, PICT and WPG builders join them in later tasks)."""
import struct


def _pad(s: bytes) -> bytes:
    return s + b"\0" * (len(s) & 1)


def _colorref(rgb: int) -> bytes:
    return bytes((rgb >> 16 & 0xFF, rgb >> 8 & 0xFF, rgb & 0xFF, 0))


def wmf_rec(fn: int, *words) -> bytes:
    """One record: its size in words, the function, then the parameters. An int is a 16-bit word, bytes go in as they are."""
    body = b"".join(w if isinstance(w, bytes) else struct.pack("<H", w & 0xFFFF) for w in words)
    body = _pad(body)
    return struct.pack("<IH", 3 + len(body) // 2, fn) + body


def wmf(records: list[bytes], placeable=(0, 0, 1440, 1440, 1440)) -> bytes:
    """A WMF file. `placeable` is left, top, right, bottom and units per inch; None leaves the placeable header out."""
    recs = b"".join(records) + wmf_rec(0)
    out = struct.pack("<HHHIHIH", 1, 9, 0x0300, (18 + len(recs)) // 2, 16, 0, 0) + recs
    if placeable is not None:
        left, top, right, bottom, inch = placeable
        pre = struct.pack("<IHhhhhH", 0x9AC6CDD7, 0, left, top, right, bottom, inch) + b"\0\0\0\0"
        check = 0
        for (w,) in struct.iter_unpack("<H", pre):
            check ^= w
        out = pre + struct.pack("<H", check) + out
    return out


def wmf_font(height, charset=0, face="Arial", escapement=0, weight=400) -> bytes:
    """META_CREATEFONTINDIRECT; the face is written as cp1252 and padded to 32 bytes."""
    name = face.encode("cp1252")[:31].ljust(32, b"\0")
    return wmf_rec(0x02FB, struct.pack("<hhhhhBBBBBBBB", height, 0, escapement, 0, weight, 0, 0, 0, charset, 0, 0, 0, 0),
                   name)


def wmf_pen(rgb=0, width=1, style=0) -> bytes:
    return wmf_rec(0x02FA, style, width, 0, _colorref(rgb))


def wmf_brush(rgb, style=0) -> bytes:
    return wmf_rec(0x02FC, style, _colorref(rgb), 0)


def wmf_select(i: int) -> bytes:
    return wmf_rec(0x012D, i)


def wmf_text(x, y, s: bytes) -> bytes:
    """META_TEXTOUT: the length, the string padded to a word, then y and x."""
    return wmf_rec(0x0521, len(s), _pad(s), y, x)


def wmf_exttext(x, y, s: bytes, options=0, dx=None) -> bytes:
    """META_EXTTEXTOUT; with ETO_GLYPH_INDEX (0x10) `s` holds 2 bytes a glyph and the length is the glyph count."""
    n = len(s) // 2 if options & 0x10 else len(s)
    words = [y, x, n, options]
    if options & 6:
        words.append(struct.pack("<4h", 0, 0, 0, 0))
    words.append(_pad(s))
    if dx is not None:
        words.append(struct.pack("<%dh" % len(dx), *dx))
    return wmf_rec(0x0A32, *words)


def wmf_window(org, ext) -> list[bytes]:
    """MM_ANISOTROPIC with the window origin and extent."""
    return [wmf_rec(0x0103, 8), wmf_rec(0x020B, org[1], org[0]), wmf_rec(0x020C, ext[1], ext[0])]


def wmf_rect(l, t, r, b) -> bytes:
    return wmf_rec(0x041B, b, r, t, l)


def wmf_polygon(points) -> bytes:
    return wmf_rec(0x0324, len(points), *[v for p in points for v in p])


def wmf_bitmap16(w, h, bpp, bits: bytes, widthbytes=None) -> bytes:
    """A Bitmap16 object: type, width, height, widthbytes, planes, bits per pixel, then the bits."""
    if widthbytes is None:
        widthbytes = (w * bpp + 15) // 16 * 2
    return struct.pack("<hhhhBB", 0, w, h, widthbytes, 1, bpp) + bits


def wmf_bitblt(x, y, w, h, rop=0x00CC0020, bitmap: bytes | None = None) -> bytes:
    """META_BITBLT; without a bitmap the record holds the raster op, the rectangles and a reserved word."""
    words = [rop & 0xFFFF, rop >> 16, 0, 0, h, w, y, x]
    words.append(bitmap if bitmap is not None else 0)
    return wmf_rec(0x0922, *words)


def wmf_stretchblt(x, y, w, h, rop=0x00CC0020, bitmap: bytes | None = None) -> bytes:
    words = [rop & 0xFFFF, rop >> 16, h, w, 0, 0, h, w, y, x]
    words.append(bitmap if bitmap is not None else 0)
    return wmf_rec(0x0B23, *words)


# ---- EMF (MS-EMF) ----------------------------------------------------------------------------------------------------
# The header's reference device makes one device pixel one frame unit (0.01 mm): szlDevice 1000 x 1000 pixels over
# szlMillimeters 10 x 10 (and szlMicrometers 10000 x 10000), so MM_TEXT logical coordinates are frame units.

ETO_GLYPH_INDEX, ETO_NO_RECT, ETO_SMALL_CHARS, ETO_PDY = 0x10, 0x100, 0x200, 0x2000


def emf_rec(typ: int, body: bytes = b"") -> bytes:
    """One EMF record: type, size (the 8-byte head included), the body padded to 4 bytes."""
    body = body + b"\0" * (-len(body) % 4)
    return struct.pack("<II", typ, 8 + len(body)) + body


def emf(records: list[bytes], frame=(0, 0, 2540, 2540)) -> bytes:
    """An EMF file: the 108-byte header (rclFrame `frame` in 0.01 mm), the records, then EMR_EOF."""
    l, t, r, b = frame
    recs = b"".join(records) + emf_rec(14, struct.pack("<III", 0, 16, 20))
    head = struct.pack("<II4i4iIIIIHHIII2i2iIII2i", 1, 108, l, t, r, b, l, t, r, b, 0x464D4520, 0x10000,
                       108 + len(recs), 0, 0, 0, 0, 0, 0, 1000, 1000, 10, 10, 0, 0, 0, 10000, 10000)
    return head + recs


def emf_select(index: int) -> bytes:
    return emf_rec(37, struct.pack("<I", index))


def emf_font(height, face="Arial", charset=0, escapement=0, weight=400, index=1) -> bytes:
    """EMR_EXTCREATEFONTINDIRECTW (a 92-byte LogFont) plus EMR_SELECTOBJECT."""
    lf = struct.pack("<5i8B", height, 0, escapement, 0, weight, 0, 0, 0, charset, 0, 0, 0, 0)
    name = face.encode("utf-16-le")[:62].ljust(64, b"\0")
    return emf_rec(82, struct.pack("<I", index) + lf + name) + emf_select(index)


def emf_brush(rgb, style=0, index=2) -> bytes:
    """EMR_CREATEBRUSHINDIRECT plus EMR_SELECTOBJECT."""
    return emf_rec(39, struct.pack("<II", index, style) + _colorref(rgb) + struct.pack("<I", 0)) + emf_select(index)


def emf_pen(rgb=0, width=1, style=0, index=3) -> bytes:
    return emf_rec(38, struct.pack("<IIii", index, style, width, 0) + _colorref(rgb)) + emf_select(index)


def _emr_text(typ, x, y, raw: bytes, n: int, options=0, dx=None, keep_rect=False) -> bytes:
    """EMR_EXTTEXTOUTA/W: bounds, graphics mode and scales, then the EmrText and its string and dx at the end. The
    rectangle is left out under ETO_NO_RECT as MS-EMF says, unless `keep_rect`, as Windows writes it."""
    head = struct.pack("<4iIff", 0, 0, 0, 0, 1, 0.0, 0.0)
    rect = b"" if options & ETO_NO_RECT and not keep_rect else struct.pack("<4i", 0, 0, 0, 0)
    at = 8 + len(head) + 20 + len(rect) + 4
    raw = raw + b"\0" * (-len(raw) % 4)
    off_dx = at + len(raw) if dx is not None else 0
    text = struct.pack("<iiIII", x, y, n, at, options) + rect + struct.pack("<I", off_dx)
    tail = struct.pack("<%di" % len(dx), *dx) if dx is not None else b""
    return emf_rec(typ, head + text + raw + tail)


def emf_textw(x, y, s: str, options=0, dx=None, keep_rect=False) -> bytes:
    raw = s.encode("utf-16-le")
    return _emr_text(84, x, y, raw, len(raw) // 2, options, dx, keep_rect)


def emf_texta(x, y, s: bytes, options=0, dx=None) -> bytes:
    return _emr_text(83, x, y, s, len(s), options, dx)


def emf_glyphs(x, y, glyphs, face) -> bytes:
    """A font of `face`, then EMR_EXTTEXTOUTW with ETO_GLYPH_INDEX: the string is 16-bit glyph numbers."""
    raw = struct.pack("<%dH" % len(glyphs), *glyphs)
    return emf_font(-100, face) + _emr_text(84, x, y, raw, len(glyphs), ETO_GLYPH_INDEX)


def emf_smalltext(x, y, s, options=0) -> bytes:
    """EMR_SMALLTEXTOUT: bytes are written 8-bit with ETO_SMALL_CHARS, a str as UTF-16."""
    if isinstance(s, bytes):
        options |= ETO_SMALL_CHARS
        raw, n = s, len(s)
    else:
        raw = s.encode("utf-16-le")
        n = len(raw) // 2
    bounds = b"" if options & ETO_NO_RECT else struct.pack("<4i", 0, 0, 0, 0)
    return emf_rec(108, struct.pack("<iiIIIff", x, y, n, options, 1, 0.0, 0.0) + bounds + raw)


def emf_polytextw(items) -> bytes:
    """EMR_POLYTEXTOUTW: (x, y, str) items, each a 40-byte EmrText, their strings after the array."""
    at = 8 + 16 + 12 + 4 + 40 * len(items)
    texts, strings = b"", b""
    for x, y, s in items:
        raw = s.encode("utf-16-le")
        raw += b"\0" * (-len(raw) % 4)
        texts += struct.pack("<iiIII4iI", x, y, len(s), at + len(strings), 0, 0, 0, 0, 0, 0)
        strings += raw
    return emf_rec(97, struct.pack("<4iIffI", 0, 0, 0, 0, 1, 0.0, 0.0, len(items)) + texts + strings)


def emf_world(m11, m12, m21, m22, dx, dy) -> bytes:
    """EMR_SETWORLDTRANSFORM."""
    return emf_rec(35, struct.pack("<6f", m11, m12, m21, m22, dx, dy))


def emf_modify_world(m11, m12, m21, m22, dx, dy, mode) -> bytes:
    """EMR_MODIFYWORLDTRANSFORM: mode 1 identity, 2 left multiply, 3 right multiply, 4 set."""
    return emf_rec(36, struct.pack("<6fI", m11, m12, m21, m22, dx, dy, mode))


def emf_poly(typ, points, wide=True) -> bytes:
    """A poly record (POLYGON 3, POLYLINE 4, POLYBEZIER 2, ...; the 16-bit forms with wide=False): bounds, count,
    points."""
    fmt = "<%di" % (2 * len(points)) if wide else "<%dh" % (2 * len(points))
    pts = struct.pack(fmt, *[v for p in points for v in p])
    return emf_rec(typ, struct.pack("<4iI", 0, 0, 0, 0, len(points)) + pts)


def emf_rect(l, t, r, b) -> bytes:
    return emf_rec(43, struct.pack("<4i", l, t, r, b))


def emf_dib(x, y, w, h, dib: bytes, rop=0x00CC0020) -> bytes:
    """EMR_STRETCHDIBITS of a packed DIB whose header is the first 40 bytes."""
    bmi, bits = dib[:40], dib[40:]
    at = 8 + 72
    body = struct.pack("<4i6i4IIIii", 0, 0, 0, 0, x, y, 0, 0, w, h, at, len(bmi), at + len(bmi), len(bits), 0, rop,
                       w, h)
    return emf_rec(81, body + bmi + bits)


# ---- EMF+ (MS-EMFPLUS) -----------------------------------------------------------------------------------------------

PLUS_VERSION = 0xDBC01002


def plus_rec(typ: int, flags: int = 0, data: bytes = b"") -> bytes:
    """One EMF+ record: type, flags, size (padding included), data size (the data alone), then the padded data."""
    pad = b"\0" * (-len(data) % 4)
    return struct.pack("<HHII", typ, flags, 12 + len(data) + len(pad), len(data)) + data + pad


def emfplus(records: list[bytes]) -> bytes:
    """EMF+ records inside one EMR_GDICOMMENT: DataSize, the "EMF+" identifier, the records."""
    data = b"EMF+" + b"".join(records)
    return emf_rec(70, struct.pack("<I", len(data)) + data)


def plus_header(dual=True, dpi=96, video=True) -> bytes:
    """EmfPlusHeader; `video` sets the flag that says the reference device is a display, not a printer."""
    return plus_rec(0x4001, 1 if dual else 0, struct.pack("<IIII", PLUS_VERSION, 1 if video else 0, dpi, dpi))


def plus_object(oid: int, otype: int, data: bytes, cont: int | None = None) -> bytes:
    """EmfPlusObject; with `cont` the continue flag is set and `cont` is the TotalObjectSize."""
    flags = oid | otype << 8
    if cont is not None:
        return plus_rec(0x4008, flags | 0x8000, struct.pack("<I", cont) + data)
    return plus_rec(0x4008, flags, data)


def plus_font(oid, size, face, unit=2, style=0) -> bytes:
    """An EmfPlusFont object: em size in `unit` (2 pixels), style, the family name."""
    name = face.encode("utf-16-le")
    return plus_object(oid, 6, struct.pack("<IfIiII", PLUS_VERSION, size, unit, style, 0, len(face)) + name)


def plus_solid(oid, argb) -> bytes:
    return plus_object(oid, 1, struct.pack("<III", PLUS_VERSION, 0, argb))


def plus_string(font_id, x, y, s: str, w=0.0, h=0.0, argb=0xFF000000, fmt=0xFFFFFFFF) -> bytes:
    """EmfPlusDrawString with a solid color (the S flag) in the layout rectangle (x, y, w, h)."""
    raw = s.encode("utf-16-le")
    return plus_rec(0x401C, font_id | 0x8000, struct.pack("<III4f", argb, fmt, len(raw) // 2, x, y, w, h) + raw)


def plus_driver(font_id, s_or_glyphs, cmap=True, x=0.0, y=0.0, advance=10.0, argb=0xFF000000, positions=None):
    """EmfPlusDrawDriverString: a str is written as UTF-16 code units (with cmap), a list as glyph numbers; each
    glyph sits `advance` after the one before on the baseline y, unless `positions` gives them."""
    if isinstance(s_or_glyphs, str):
        raw = s_or_glyphs.encode("utf-16-le")
        glyphs = list(struct.unpack("<%dH" % (len(raw) // 2), raw))
    else:
        glyphs = list(s_or_glyphs)
    n = len(glyphs)
    pos = positions if positions is not None else [(x + i * advance, y) for i in range(n)]
    data = struct.pack("<IIII", argb, 1 if cmap else 0, 0, n) + struct.pack("<%dH" % n, *glyphs)
    data += struct.pack("<%df" % (2 * n), *[v for p in pos for v in p])
    return plus_rec(0x4036, font_id | 0x8000, data)


def plus_getdc() -> bytes:
    return plus_rec(0x4004)


def plus_fill_rect(x, y, w, h, argb=0xFF808080) -> bytes:
    """EmfPlusFillRects of one rectangle in a solid color."""
    return plus_rec(0x400A, 0x8000, struct.pack("<II4f", argb, 1, x, y, w, h))


def plus_world(m11, m12, m21, m22, dx, dy) -> bytes:
    return plus_rec(0x402A, 0, struct.pack("<6f", m11, m12, m21, m22, dx, dy))


def plus_image_metafile(oid, data: bytes, mtype=3) -> bytes:
    """An EmfPlusImage object holding a metafile (type 3 EMF)."""
    return plus_object(oid, 5, struct.pack("<IiiI", PLUS_VERSION, 2, mtype, len(data)) + data)


def plus_image_bitmap(oid, data: bytes) -> bytes:
    """An EmfPlusImage object holding a compressed bitmap (PNG, JPEG, ...)."""
    return plus_object(oid, 5, struct.pack("<IiiiiIi", PLUS_VERSION, 1, 1, 1, 0, 0, 1) + data)


def plus_draw_image(oid, x, y, w, h) -> bytes:
    """EmfPlusDrawImage of image `oid` into the rectangle (x, y, w, h)."""
    return plus_rec(0x401A, oid, struct.pack("<Ii4f4f", 0xFFFFFFFF, 2, 0, 0, 1, 1, x, y, w, h))


# ---- TrueType fonts -----------------------------------------------------------------------------------------------

def _cmap4(cmap: dict, ranged: bool) -> bytes:
    """A format 4 subtable for the BMP part of cmap. One segment per code point; with `ranged` each segment reads its
    glyph through idRangeOffset and the glyph id array instead of idDelta."""
    items = sorted((c, g) for c, g in cmap.items() if c <= 0xFFFF and c != 0xFFFF)
    segs = [(c, c, g) for c, g in items] + [(0xFFFF, 0xFFFF, 0)]
    n = len(segs)
    ends = b"".join(struct.pack(">H", e) for _, e, _ in segs)
    starts = b"".join(struct.pack(">H", s) for s, _, _ in segs)
    if ranged:
        deltas = b"\0\0" * n
        # idRangeOffset[i] points at glyph array slot i: (n - i) words from its own entry, then i words in
        ros = b"".join(struct.pack(">H", 2 * (n - i) + 2 * i) for i in range(n))
        garr = b"".join(struct.pack(">H", g) for _, _, g in segs)
    else:
        deltas = b"".join(struct.pack(">H", (g - s) & 0xFFFF if s != 0xFFFF else 1) for s, _, g in segs)
        ros = b"\0\0" * n
        garr = b""
    body = struct.pack(">HHHH", n * 2, 0, 0, 0) + ends + b"\0\0" + starts + deltas + ros + garr
    return struct.pack(">HHH", 4, 6 + len(body), 0) + body


def _cmap12(cmap: dict) -> bytes:
    groups = b"".join(struct.pack(">III", c, c, g) for c, g in sorted(cmap.items()))
    return struct.pack(">HHIII", 12, 0, 16 + len(groups), 0, len(cmap)) + groups


def _name_table(family: str, full: str, extra) -> bytes:
    recs = [(3, 1, 0x409, 1, family.encode("utf-16-be")), (3, 1, 0x409, 4, full.encode("utf-16-be"))]
    for plat, enc, nid, s in extra:
        recs.append((plat, enc, 0x409 if plat == 3 else 0, nid, s.encode("mac_roman" if plat == 1 else "utf-16-be")))
    head = struct.pack(">HHH", 0, len(recs), 6 + 12 * len(recs))
    strings = b""
    out = b""
    for plat, enc, lang, nid, raw in recs:
        out += struct.pack(">HHHHHH", plat, enc, lang, nid, len(raw), len(strings))
        strings += raw
    return head + out + strings


def ttf(family: str, cmap: dict[int, int], full: str | None = None, extra_names=(), ranged=False, base=0) -> bytes:
    """A minimal TrueType font: table directory, `name` (IDs 1 and 4, platform 3) and `cmap` (format 4, and
    format 12 when a code point is above 0xFFFF). `extra_names` adds (platform, encoding, id, text) records.
    `base` is where the font starts inside a collection (table offsets are from the start of the file)."""
    subs = [(3, 1, _cmap4(cmap, ranged))]
    if any(c > 0xFFFF for c in cmap):
        subs.append((3, 10, _cmap12(cmap)))
    off = 4 + 8 * len(subs)
    head = struct.pack(">HH", 0, len(subs))
    body = b""
    for plat, enc, data in subs:
        head += struct.pack(">HHI", plat, enc, off + len(body))
        body += data
    cm = head + body
    nm = _name_table(family, full or family, extra_names)
    start = base + 12 + 16 * 2
    pad = lambda b: b + b"\0" * (-len(b) % 4)  # noqa: E731
    out = struct.pack(">IHHHH", 0x00010000, 2, 0, 0, 0)
    out += b"cmap" + struct.pack(">III", 0, start, len(cm))
    out += b"name" + struct.pack(">III", 0, start + len(pad(cm)), len(nm))
    return out + pad(cm) + pad(nm)


def ttc(specs: list[tuple]) -> bytes:
    """A TrueType collection from (family, cmap) pairs."""
    head = 12 + 4 * len(specs)
    bases, fonts = [], []
    pos = head
    for fam, cm in specs:
        bases.append(pos)
        f = ttf(fam, cm, base=pos)
        f += b"\0" * (-len(f) % 4)
        fonts.append(f)
        pos += len(f)
    return b"ttcf" + struct.pack(">II", 0x00010000, len(specs)) + b"".join(struct.pack(">I", b) for b in bases) \
        + b"".join(fonts)
