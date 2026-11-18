"""EMF+ records (MS-EMFPLUS) inside an EMF's GDICOMMENT records, for metafile._Emf.

`Plus` keeps the EMF+ state (the 64-slot object table, world, container and page transforms, the clip) and draws into
its host, the EMF parser, through `_add_path`, `_add_run`, `_sync_clip` and `_nested`. Every count, length and object
id read from a record is checked against the record first. World coordinates go through the world transform, the
container transforms, then the page unit and scale, which the header's dpi turns into frame units (0.01 mm).

The dual rule lives in `active` and `gdi`: once an EMF+ header is seen the EMF+ records are followed and the host skips
its EMF drawing records, except after EmfPlusGetDC, until the next EMF+ drawing record.
"""
import math
import struct
import unicodedata
from dataclasses import replace

from . import limits
from .images import bmp_to_png, sniff
from .vector import Bitmap, Brush, Clip, Path, Pen, Run, arc_beziers

IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)  # (m11, m12, m21, m22, dx, dy): x' = x m11 + y m21 + dx, y' = x m12 + y m22 + dy
SLOTS = 64
_ASCENT = 0.8  # metafile._ASCENT: the baseline below the top of a line, in ems
_LEADING = 1.2  # line advance of a DrawString line, in ems


def mul(a, b):
    """The transform that applies a, then b."""
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5])


def good(m) -> bool:
    """Finite with a non-zero determinant; other matrices are replaced by the identity."""
    return all(map(math.isfinite, m)) and m[0] * m[3] - m[1] * m[2] != 0


def apply(m, x, y):
    return x * m[0] + y * m[2] + m[4], x * m[1] + y * m[3] + m[5]


def angle_of(m) -> float:
    """The baseline's angle in degrees, counterclockwise as seen (y grows down); 0 when the transform turns nothing."""
    a, c = m[0], m[1]
    if abs(c) <= 1e-9 * abs(a):
        return 0.0
    return math.degrees(math.atan2(-c, a))


# clip combine modes (EmfPlusCombineMode); EMF's RGN_AND..RGN_COPY map onto them
REPLACE, INTERSECT, UNION, XOR, EXCLUDE, COMPLEMENT = range(6)


def combine(cur, new, mode):
    """Clip boxes (x0, y0, x1, y1), None for no clip. Intersect and replace are exact; union and xor become the
    union's box, exclude keeps the current clip and complement takes the new box, so the clip never cuts away
    something the true region shows."""
    if mode == REPLACE or mode == COMPLEMENT:
        return new
    if mode == INTERSECT:
        if cur is None:
            return new
        if new is None:
            return cur
        x0, y0, x1, y1 = max(cur[0], new[0]), max(cur[1], new[1]), min(cur[2], new[2]), min(cur[3], new[3])
        return (x0, y0, max(x0, x1), max(y0, y1))
    if mode in (UNION, XOR):
        if cur is None or new is None:
            return None
        return (min(cur[0], new[0]), min(cur[1], new[1]), max(cur[2], new[2]), max(cur[3], new[3]))
    return cur


def _box_of(pts):
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _argb(v: int):
    """An EmfPlusARGB as 0xRRGGBB, None when fully transparent."""
    return None if v >> 24 == 0 else v & 0xFFFFFF


def _average(colors):
    cs = [c for c in colors if c >> 24]
    if not cs:
        return None
    n = len(cs)
    r, g, b = (sum(c >> s & 255 for c in cs) // n for s in (16, 8, 0))
    return r << 16 | g << 8 | b


def _brush(d, off: int):
    """An EmfPlusBrush at `off` (version, type, data) as a color: solid as is, hatch its foreground, linear and path
    gradients their average; a texture has none."""
    _ver, typ = struct.unpack_from("<II", d, off)
    o = off + 8
    if typ == 0:
        return _argb(struct.unpack_from("<I", d, o)[0])
    if typ == 1:
        return _argb(struct.unpack_from("<III", d, o)[1])
    if typ == 4:  # flags, wrap, rect, start color, end color
        return _average(struct.unpack_from("<II", d, o + 24))
    if typ == 3:  # flags, wrap, center color, center point, count, surrounding colors
        center, = struct.unpack_from("<I", d, o + 8)
        n, = struct.unpack_from("<I", d, o + 20)
        if o + 24 + 4 * n > len(d):
            raise ValueError
        around = struct.unpack_from("<%dI" % n, d, o + 24)
        mean = _average(around)
        return _average([center] + ([0xFF000000 | mean] if mean is not None else []))
    return None


def _int_r(d, off):
    """An EmfPlusInteger7 (one byte, top bit clear) or EmfPlusInteger15 (two bytes, top bit set), and the offset
    after it."""
    b = d[off]
    if b & 0x80:
        v = (b & 0x7F) << 8 | d[off + 1]
        return (v - 0x8000 if v & 0x4000 else v), off + 2
    return (b - 0x80 if b & 0x40 else b), off + 1


def _read_points(d, off, n, flags, budget):
    """`n` points at `off`: relative EmfPlusPointR with 0x0800, 16-bit with 0x4000, else floats. (points, end)."""
    if n < 0:
        raise ValueError
    rel, small = flags & 0x0800, flags & 0x4000
    if off + n * (2 if rel else 4 if small else 8) > len(d):
        raise ValueError
    budget.points(n)
    if rel:
        out, x, y = [], 0, 0
        for _ in range(n):
            dx, off = _int_r(d, off)
            dy, off = _int_r(d, off)
            x, y = x + dx, y + dy
            out.append((float(x), float(y)))
        return out, off
    if small:
        v = struct.unpack_from("<%dh" % (2 * n), d, off)
        return [(float(v[2 * i]), float(v[2 * i + 1])) for i in range(n)], off + 4 * n
    v = struct.unpack_from("<%df" % (2 * n), d, off)
    return [(v[2 * i], v[2 * i + 1]) for i in range(n)], off + 8 * n


def _read_rects(d, off, n, flags):
    if n < 0 or off + n * (8 if flags & 0x4000 else 16) > len(d):
        raise ValueError
    fmt = "<%dh" if flags & 0x4000 else "<%df"
    v = struct.unpack_from(fmt % (4 * n), d, off)
    return [tuple(float(q) for q in v[4 * i:4 * i + 4]) for i in range(n)]


def _rect_segs(x, y, w, h):
    return [("M", x, y), ("L", x + w, y), ("L", x + w, y + h), ("L", x, y + h), ("Z",)]


def _ellipse_segs(x, y, w, h):
    cx, cy, rx, ry = x + w / 2, y + h / 2, abs(w) / 2, abs(h) / 2
    return [("M", cx + rx, cy)] + arc_beziers(cx, cy, rx, ry, 0, 360) + [("Z",)]


def _arc_segs(x, y, w, h, start, sweep, kind):
    """GDI+ angles: degrees clockwise from the x axis as seen, which is arc_beziers' direction with y down."""
    cx, cy, rx, ry = x + w / 2, y + h / 2, abs(w) / 2, abs(h) / 2
    arcs = arc_beziers(cx, cy, rx, ry, start, sweep)
    if not arcs:
        return []
    a = math.radians(start)
    segs = [("M", cx + rx * math.cos(a), cy + ry * math.sin(a))] + arcs
    if kind == "pie":
        segs += [("L", cx, cy), ("Z",)]
    return segs


def _cardinal(pts, tension, closed, offset=0, nseg=None):
    """A cardinal spline as Béziers: the control points of segment i leave P[i] and reach P[i+1] along the chords of
    their neighbours, scaled by tension / 3 (0.5 is Catmull-Rom)."""
    n = len(pts)
    if n < 2 or not math.isfinite(tension):
        return []
    t = tension / 3.0

    def at(i):
        return pts[i % n] if closed else pts[min(max(i, 0), n - 1)]
    if closed:
        idx = range(n)
    else:
        last = n - 1 if nseg is None else min(n - 1, offset + max(nseg, 0))
        idx = range(min(max(offset, 0), n - 1), last)
    segs = []
    for i in idx:
        p0, p1, p2, p3 = at(i - 1), at(i), at(i + 1), at(i + 2)
        if not segs:
            segs.append(("M",) + p1)
        segs.append(("C", p1[0] + t * (p2[0] - p0[0]), p1[1] + t * (p2[1] - p0[1]),
                     p2[0] - t * (p3[0] - p1[0]), p2[1] - t * (p3[1] - p1[1]), p2[0], p2[1]))
    if closed and segs:
        segs.append(("Z",))
    return segs


def _path(d, budget):
    """An EmfPlusPath object (version, count, flags, points, types) as segments in world units."""
    _ver, n, pflags = struct.unpack_from("<IiI", d, 0)
    pts, off = _read_points(d, 12, n, pflags, budget)
    if pflags & 0x1000:  # run-length point types: a count byte (low 6 bits), then the type
        types = bytearray()
        while len(types) < n:
            run, t = d[off] & 0x3F, d[off + 1]
            off += 2
            types += bytes((t,)) * min(run, n - len(types))
    else:
        if off + n > len(d):
            raise ValueError
        types = d[off:off + n]
    segs, i = [], 0
    while i < n:
        t = types[i] & 7
        if t == 0 or not segs:
            segs.append(("M",) + pts[i])
            last = i
            i += 1
        elif t == 3 and i + 2 < n:
            segs.append(("C",) + pts[i] + pts[i + 1] + pts[i + 2])
            last = i + 2
            i += 3
        else:
            segs.append(("L",) + pts[i])
            last = i
            i += 1
        if types[last] & 0x80:
            segs.append(("Z",))
    return segs


_EMPTY = "empty"


def _region_node(d, off, depth, budget):
    """A region node as a world box, None (infinite) or _EMPTY, and the offset after it."""
    if depth > limits.MAX_DEPTH:
        raise ValueError
    typ, = struct.unpack_from("<I", d, off)
    off += 4
    if typ == 0x10000000:
        x, y, w, h = struct.unpack_from("<4f", d, off)
        return (min(x, x + w), min(y, y + h), max(x, x + w), max(y, y + h)), off + 16
    if typ == 0x10000001:
        size, = struct.unpack_from("<I", d, off)
        if off + 4 + size > len(d):
            raise ValueError
        pts = [q for s in _path(d[off + 4:off + 4 + size], budget) for q in zip(s[1::2], s[2::2])]
        return (_box_of(pts) if pts else _EMPTY), off + 4 + size
    if typ == 0x10000002:
        return _EMPTY, off
    if typ == 0x10000003:
        return None, off
    if not 1 <= typ <= 5:
        raise ValueError
    left, off = _region_node(d, off, depth + 1, budget)
    right, off = _region_node(d, off, depth + 1, budget)
    if typ == 1:  # and
        if left == _EMPTY or right == _EMPTY:
            return _EMPTY, off
        return combine(left, right, INTERSECT), off
    if typ in (2, 3):  # or, xor
        if left == _EMPTY:
            return right, off
        if right == _EMPTY:
            return left, off
        return combine(left, right, UNION), off
    return (left if typ == 4 else right), off  # exclude keeps the left, complement the right


def _font(d):
    _ver, em, unit, style, _r, n = struct.unpack_from("<IfIiII", d, 0)
    if 24 + 2 * n > len(d):
        raise ValueError
    face = d[24:24 + 2 * n].decode("utf-16-le", "replace").split("\0")[0]
    return ("font", em, unit, style, face)


def _pen(d):
    """An EmfPlusPen: its unit and width, the optional fields its flags name (skipped), then its brush."""
    _ver, _typ, flags, unit = struct.unpack_from("<IIII", d, 0)
    width, = struct.unpack_from("<f", d, 16)
    o = 20
    for bit, size in ((0x01, 24), (0x02, 4), (0x04, 4), (0x08, 4), (0x10, 4), (0x20, 4), (0x40, 4), (0x80, 4)):
        if flags & bit:
            o += size
    if flags & 0x100:  # dash lengths
        o += 4 + 4 * struct.unpack_from("<I", d, o)[0]
    if flags & 0x200:
        o += 4
    if flags & 0x400:  # compound line
        o += 4 + 4 * struct.unpack_from("<I", d, o)[0]
    for bit in (0x800, 0x1000):  # custom caps: a size, then the cap
        if flags & bit:
            o += 4 + struct.unpack_from("<I", d, o)[0]
    try:
        color = _brush(d, o)
    except (struct.error, IndexError, ValueError):
        color = 0
    return ("pen", color, width, unit)


def _image(d):
    _ver, typ = struct.unpack_from("<Ii", d, 0)
    if typ == 1:  # bitmap: width, height, stride, pixel format, type (0 pixels, 1 compressed)
        w, h, stride, fmt, btype = struct.unpack_from("<iiiIi", d, 8)
        raw = d[28:]
        if btype == 1:
            kind = sniff(raw)
            if kind in ("png", "jpg"):
                return ("image", kind, raw)
            if kind == "bmp":
                png = bmp_to_png(raw)
                return ("image", "png", png) if png else None
            return None
        bpp = {0x00021808: 24, 0x00022009: 32, 0x0026200A: 32, 0x000E200B: 32}.get(fmt)
        row = w * (bpp or 0) // 8
        if not bpp or w <= 0 or h <= 0 or stride < row or stride * h > len(raw):
            return None
        pad = -row % 4
        rows = b"".join(raw[i * stride:i * stride + row] + b"\0" * pad for i in range(h))
        head = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, bpp, 0, len(rows), 0, 0, 0, 0)
        png = bmp_to_png(struct.pack("<2sIHHI", b"BM", 54 + len(rows), 0, 0, 54) + head + rows)
        return ("image", "png", png) if png else None
    if typ == 2:  # metafile: type (WMF, placeable WMF, EMF, EMF+ only, EMF+ dual), size, bytes
        mtype, size = struct.unpack_from("<iI", d, 8)
        if not 1 <= mtype <= 5 or 16 + size > len(d):
            return None
        return ("image", "meta", d[16:16 + size], [])  # the list caches the parsed drawing
    return None


def _format(d):
    _ver, flags, _lang, align, line = struct.unpack_from("<5I", d, 0)
    return ("format", align, line, flags)


# record types (MS-EMFPLUS 2.1.1.1)
HEADER, EOF, GETDC, OBJECT = 0x4001, 0x4002, 0x4004, 0x4008
_DRAW = set(range(0x4009, 0x401D)) | {0x4036, 0x4037}
_QUIET = {0x4003, 0x4005, 0x4006, 0x4007, 0x4009, 0x401D, 0x401E, 0x401F, 0x4020, 0x4021, 0x4022, 0x4023, 0x4024,
          0x4038, 0x4039, 0x403A}
_ERRORS = (struct.error, IndexError, ValueError, ZeroDivisionError, OverflowError, UnicodeDecodeError)


class Plus:
    def __init__(self, host):
        self.h = host
        self.active = False  # an EMF+ header was seen: EMF+ records are followed
        self.gdi = False     # after GetDC: EMF records are followed until the next EMF+ drawing record
        self.dpi = (96.0, 96.0)
        self.video = True  # the reference device is a display: UnitDisplay is a pixel, else 1/100 inch
        self.objs: dict = {}
        self.pending: dict = {}  # id -> [type, total size, bytearray] of a continued object
        self.world = IDENT
        self.base = IDENT  # the containers' transforms, applied after the world transform
        self.unit = 1
        self.scale = 1.0
        self.clip = None
        self.stack: list = []

    # ---- the record loop -----------------------------------------------------------------------------------------

    def feed(self, buf: bytes):
        pos, n = 0, len(buf)
        while pos + 12 <= n:
            typ, flags, size, dsize = struct.unpack_from("<HHII", buf, pos)
            if size < 12 or pos + size > n or dsize > size - 12:
                self.h.damaged = True
                return
            self.h.budget.tick()
            data = buf[pos + 12:pos + 12 + dsize]
            pos += size
            if typ == HEADER:
                self._header(data)
                continue
            if not self.active:
                continue
            if typ in _DRAW and self.gdi:
                self.gdi = False
                self.h._sync_clip()
            f = _HANDLERS.get(typ)
            try:
                if f is not None:
                    f(self, flags, data)
                elif typ not in _QUIET and typ != EOF:
                    self.h.skipped += 1
            except _ERRORS:
                self.h.damaged = True
                self.h.skipped += 1

    def _header(self, d):
        if len(d) >= 16:
            flags, dx, dy = struct.unpack_from("<III", d, 4)
            self.video = bool(flags & 1)
            self.dpi = (float(dx) if 0 < dx < 100_000 else 96.0, float(dy) if 0 < dy < 100_000 else 96.0)
        self.active = True

    def _getdc(self, flags, d):
        self.gdi = True
        self.h._sync_clip()

    # ---- transforms ----------------------------------------------------------------------------------------------

    def _unit(self, u):
        """Frame units (0.01 mm) per unit of UnitType `u`, per axis; world and pixel are pixels, and so is display
        on a video reference device (1/100 inch on a printer)."""
        f = {3: 2540 / 72, 4: 2540.0, 5: 2540 / 300, 6: 100.0}.get(u)
        if u == 1 and not self.video:
            f = 25.4
        return (f, f) if f else (2540 / self.dpi[0], 2540 / self.dpi[1])

    def _wb(self):
        return mul(self.world, self.base)

    def _m(self):
        """World units to frame units."""
        ux, uy = self._unit(self.unit)
        return mul(self._wb(), (self.scale * ux, 0.0, 0.0, self.scale * uy, 0.0, 0.0))

    def _set_world(self, m):
        self.world = m if good(m) else IDENT

    def _apply_world(self, flags, m):
        """Pre-multiplies (the new transform first) unless the A flag (0x2000) asks to post-multiply."""
        if not good(m):
            return
        self._set_world(mul(self.world, m) if flags & 0x2000 else mul(m, self.world))

    def _world_set(self, flags, d):
        self._set_world(struct.unpack_from("<6f", d))

    def _world_reset(self, flags, d):
        self.world = IDENT

    def _world_mul(self, flags, d):
        self._apply_world(flags, struct.unpack_from("<6f", d))

    def _world_translate(self, flags, d):
        x, y = struct.unpack_from("<ff", d)
        self._apply_world(flags, (1.0, 0.0, 0.0, 1.0, x, y))

    def _world_scale(self, flags, d):
        x, y = struct.unpack_from("<ff", d)
        self._apply_world(flags, (x, 0.0, 0.0, y, 0.0, 0.0))

    def _world_rotate(self, flags, d):
        a = math.radians(struct.unpack_from("<f", d)[0])
        if math.isfinite(a):
            self._apply_world(flags, (math.cos(a), math.sin(a), -math.sin(a), math.cos(a), 0.0, 0.0))

    def _page(self, flags, d):
        s, = struct.unpack_from("<f", d)
        self.unit = flags & 0xFF
        self.scale = s if math.isfinite(s) and 0 < s < 1e9 else 1.0

    def _push(self, idx) -> bool:
        if len(self.stack) >= limits.MAX_DEPTH:
            self.h.skipped += 1
            return False
        self.stack.append((idx, self.world, self.base, self.unit, self.scale, self.clip))
        return True

    def _pop(self, flags, d):
        idx, = struct.unpack_from("<I", d)
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == idx:
                _, self.world, self.base, self.unit, self.scale, self.clip = self.stack[i]
                del self.stack[i:]
                self.h._sync_clip()
                return

    def _save(self, flags, d):
        self._push(struct.unpack_from("<I", d)[0])

    def _container(self, flags, d):
        """BeginContainer: the source rectangle maps onto the destination, inside the current transforms."""
        dx, dy, dw, dh, sx, sy, sw, sh, idx = struct.unpack_from("<8fI", d)
        if not self._push(idx):
            return
        kx, ky = (dw / sw if sw else 1.0), (dh / sh if sh else 1.0)
        c = (kx, 0.0, 0.0, ky, dx - sx * kx, dy - sy * ky)
        if not good(c):
            c = IDENT
        self.base = mul(mul(c, self.world), self.base)
        self.world = IDENT

    def _container_plain(self, flags, d):
        if self._push(struct.unpack_from("<I", d)[0]):
            self.base = mul(self.world, self.base)
            self.world = IDENT

    # ---- clipping ------------------------------------------------------------------------------------------------

    def _clip_to(self, box, mode):
        self.clip = combine(self.clip, box, mode)
        self.h._sync_clip()

    def _frame_box(self, pts):
        m = self._m()
        return _box_of([apply(m, x, y) for x, y in pts])

    def _clip_reset(self, flags, d):
        self._clip_to(None, REPLACE)

    def _clip_rect(self, flags, d):
        x, y, w, h = struct.unpack_from("<4f", d)
        self._clip_to(self._frame_box([(x, y), (x + w, y), (x, y + h), (x + w, y + h)]), flags >> 8 & 0xF)

    def _clip_path(self, flags, d):
        o = self._obj(flags & 0xFF, "path")
        if o is not None:
            pts = [q for s in o[1] for q in zip(s[1::2], s[2::2])]
            if pts:
                self._clip_to(self._frame_box(pts), flags >> 8 & 0xF)

    def _clip_region(self, flags, d):
        o = self._obj(flags & 0xFF, "region")
        if o is not None:
            box = o[1]
            if box == _EMPTY:
                m = self._m()
                box = (m[4], m[5], m[4], m[5])
            elif box is not None:
                box = self._frame_box([(box[0], box[1]), (box[2], box[1]), (box[0], box[3]), (box[2], box[3])])
            self._clip_to(box, flags >> 8 & 0xF)

    def _clip_offset(self, flags, d):
        x, y = struct.unpack_from("<ff", d)
        if self.clip is not None:
            m = self._m()
            vx, vy = x * m[0] + y * m[2], x * m[1] + y * m[3]
            c = self.clip
            self._clip_to((c[0] + vx, c[1] + vy, c[2] + vx, c[3] + vy), REPLACE)

    # ---- objects -------------------------------------------------------------------------------------------------

    def _obj(self, oid, kind):
        o = self.objs.get(oid) if oid < SLOTS else None
        return o if o is not None and o[0] == kind else None

    def _object(self, flags, d):
        oid, otype = flags & 0xFF, flags >> 8 & 0x7F
        if oid >= SLOTS:
            self.h.skipped += 1
            return
        if flags & 0x8000:  # continued: TotalObjectSize, then this part
            total, = struct.unpack_from("<I", d)
            p = self.pending.get(oid)
            if p is None or p[0] != otype:
                p = self.pending[oid] = [otype, total, bytearray()]
            if len(p[2]) + len(d) - 4 > limits.IMAGE_BYTES:
                del self.pending[oid]
                self.h.skipped += 1
                return
            p[2] += d[4:]
            if len(p[2]) < p[1]:
                return
            d = bytes(self.pending.pop(oid)[2])
        else:
            p = self.pending.pop(oid, None)
            if p is not None and p[0] == otype:
                if len(p[2]) + len(d) > limits.IMAGE_BYTES:
                    self.h.skipped += 1
                    return
                d = bytes(p[2]) + d
        self.objs.pop(oid, None)
        try:
            obj = self._parse_object(otype, d)
        except _ERRORS:
            obj = None
        if obj is None:
            self.h.skipped += 1
            return
        self.objs[oid] = obj

    def _parse_object(self, otype, d):
        if otype == 1:
            return ("brush", _brush(d, 0))
        if otype == 2:
            return _pen(d)
        if otype == 3:
            return ("path", _path(d, self.h.budget))
        if otype == 4:
            return ("region", _region_node(d, 8, 0, self.h.budget)[0])
        if otype == 5:
            return _image(d)
        if otype == 6:
            return _font(d)
        if otype == 7:
            return _format(d)
        return ("other",)

    # ---- drawing -------------------------------------------------------------------------------------------------

    def _color(self, flags, brush_id):
        """The S flag (0x8000) puts an ARGB color in the brush id's place."""
        if flags & 0x8000:
            return _argb(brush_id)
        o = self._obj(brush_id, "brush")
        return o[1] if o is not None else None

    def _map(self, segs, m=None):
        m = m or self._m()
        out = []
        for s in segs:
            v = []
            for i in range(1, len(s), 2):
                v += apply(m, s[i], s[i + 1])
            out.append((s[0],) + tuple(v))
        return out

    def _fill(self, segs, color, even_odd=True):
        if color is not None and segs:
            self.h._add_path(self._map(segs), None, Brush(color), even_odd)

    def _stroke(self, segs, pen_id):
        o = self._obj(pen_id, "pen")
        if o is None or o[1] is None or not segs:
            return
        _, color, width, unit = o
        m = self._m()
        if unit == 0:  # world units: the whole transform scales the pen
            w = width * math.sqrt(abs(m[0] * m[3] - m[1] * m[2]))
        else:
            wb = self._wb()
            w = width * self._unit(unit)[0] * math.sqrt(abs(wb[0] * wb[3] - wb[1] * wb[2]))
        w = max(w, self.h.dpi / 96.0) if math.isfinite(w) else self.h.dpi / 96.0
        self.h._add_path(self._map(segs, m), Pen(color, w), None, False)

    def _fill_rects(self, flags, d):
        brush, n = struct.unpack_from("<II", d)
        rects = _read_rects(d, 8, n, flags)
        self.h.budget.points(4 * n)
        self._fill([s for r in rects for s in _rect_segs(*r)], self._color(flags, brush))

    def _draw_rects(self, flags, d):
        n, = struct.unpack_from("<I", d)
        rects = _read_rects(d, 4, n, flags)
        self.h.budget.points(4 * n)
        self._stroke([s for r in rects for s in _rect_segs(*r)], flags & 0xFF)

    def _poly_segs(self, pts, closed):
        if len(pts) < 2:
            return []
        segs = [("M",) + pts[0]] + [("L",) + p for p in pts[1:]]
        return segs + [("Z",)] if closed else segs

    def _fill_polygon(self, flags, d):
        brush, n = struct.unpack_from("<II", d)
        pts, _ = _read_points(d, 8, n, flags, self.h.budget)
        self._fill(self._poly_segs(pts, True), self._color(flags, brush))

    def _draw_lines(self, flags, d):
        n, = struct.unpack_from("<I", d)
        pts, _ = _read_points(d, 4, n, flags, self.h.budget)
        self._stroke(self._poly_segs(pts, bool(flags & 0x2000)), flags & 0xFF)

    def _rect_at(self, flags, d, off):
        return _read_rects(d, off, 1, flags)[0]

    def _fill_ellipse(self, flags, d):
        brush, = struct.unpack_from("<I", d)
        self.h.budget.points(13)
        self._fill(_ellipse_segs(*self._rect_at(flags, d, 4)), self._color(flags, brush))

    def _draw_ellipse(self, flags, d):
        self.h.budget.points(13)
        self._stroke(_ellipse_segs(*self._rect_at(flags, d, 0)), flags & 0xFF)

    def _fill_pie(self, flags, d):
        brush, start, sweep = struct.unpack_from("<Iff", d)
        self.h.budget.points(16)
        self._fill(_arc_segs(*self._rect_at(flags, d, 12), start, sweep, "pie"), self._color(flags, brush))

    def _draw_pie(self, flags, d, kind="pie"):
        start, sweep = struct.unpack_from("<ff", d)
        self.h.budget.points(16)
        self._stroke(_arc_segs(*self._rect_at(flags, d, 8), start, sweep, kind), flags & 0xFF)

    def _draw_arc(self, flags, d):
        self._draw_pie(flags, d, "arc")

    def _fill_region(self, flags, d):
        brush, = struct.unpack_from("<I", d)
        o = self._obj(flags & 0xFF, "region")
        if o is not None and o[1] not in (None, _EMPTY):
            x0, y0, x1, y1 = o[1]
            self._fill(_rect_segs(x0, y0, x1 - x0, y1 - y0), self._color(flags, brush))

    def _fill_path(self, flags, d):
        brush, = struct.unpack_from("<I", d)
        o = self._obj(flags & 0xFF, "path")
        if o is not None:
            self._fill(o[1], self._color(flags, brush))

    def _draw_path(self, flags, d):
        pen, = struct.unpack_from("<I", d)
        o = self._obj(flags & 0xFF, "path")
        if o is not None:
            self._stroke(o[1], pen)

    def _fill_closed_curve(self, flags, d):
        brush, tension, n = struct.unpack_from("<IfI", d)
        pts, _ = _read_points(d, 12, n, flags, self.h.budget)
        self._fill(_cardinal(pts, tension, True), self._color(flags, brush), not flags & 0x2000)

    def _draw_closed_curve(self, flags, d):
        tension, n = struct.unpack_from("<fI", d)
        pts, _ = _read_points(d, 8, n, flags, self.h.budget)
        self._stroke(_cardinal(pts, tension, True), flags & 0xFF)

    def _draw_curve(self, flags, d):
        tension, offset, nseg, n = struct.unpack_from("<fIII", d)
        pts, _ = _read_points(d, 16, n, flags & 0x4000, self.h.budget)
        self._stroke(_cardinal(pts, tension, False, offset, nseg), flags & 0xFF)

    def _draw_beziers(self, flags, d):
        n, = struct.unpack_from("<I", d)
        pts, _ = _read_points(d, 4, n, flags, self.h.budget)
        if pts:
            segs = [("M",) + pts[0]] + [("C",) + pts[i] + pts[i + 1] + pts[i + 2] for i in range(1, len(pts) - 2, 3)]
            self._stroke(segs if len(segs) > 1 else [], flags & 0xFF)

    # ---- images --------------------------------------------------------------------------------------------------

    def _draw_image(self, flags, d):
        o = self._obj(flags & 0xFF, "image")
        x, y, w, h = self._rect_at(flags, d, 24)
        if o is not None:
            self._place(o, (x, y), (x + w, y), (x, y + h))

    def _draw_image_points(self, flags, d):
        o = self._obj(flags & 0xFF, "image")
        n, = struct.unpack_from("<I", d, 24)
        if n != 3:
            raise ValueError
        pts, _ = _read_points(d, 28, 3, flags, self.h.budget)
        if o is not None:
            self._place(o, *pts)

    def _place(self, o, p0, p1, p2):
        """An image into the parallelogram whose corners are p0 (top left), p1 (top right) and p2 (bottom left)."""
        m = self._m()
        q0, q1, q2 = apply(m, *p0), apply(m, *p1), apply(m, *p2)
        if o[1] != "meta":
            q3 = (q1[0] + q2[0] - q0[0], q1[1] + q2[1] - q0[1])
            box = _box_of([q0, q1, q2, q3])
            if all(map(math.isfinite, box)):
                self.h.items.append(Bitmap(o[2], o[1], box))
            else:
                self.h.skipped += 1
            return
        if not o[3]:
            o[3].append(self.h._nested(o[2]))
        nd = o[3][0]
        if nd is None:
            return
        fx0, fy0, fx1, fy1 = nd.frame
        a = None
        if fx1 != fx0 and fy1 != fy0:
            # nested frame units -> this frame: (u, v) in [0, 1] across the frame span the parallelogram
            ax, ay = (q1[0] - q0[0]) / (fx1 - fx0), (q1[1] - q0[1]) / (fx1 - fx0)
            bx, by = (q2[0] - q0[0]) / (fy1 - fy0), (q2[1] - q0[1]) / (fy1 - fy0)
            a = (ax, ay, bx, by, q0[0] - fx0 * ax - fy0 * bx, q0[1] - fx0 * ay - fy0 * by)
        if a is None or not good(a):  # a picture or a destination with no area
            self.h.skipped += 1
            return
        self._put_drawing(nd, a)

    def _put_drawing(self, nd, a):
        sx, sy = math.hypot(a[0], a[1]), math.hypot(a[2], a[3])
        turn = angle_of(a)
        clipped = False
        for it in nd.items:
            if isinstance(it, Path):
                pen = Pen(it.pen.color, it.pen.width * math.sqrt(sx * sy)) if it.pen is not None else None
                self.h._add_path(self._map(it.segs, a), pen, it.brush, it.even_odd)
            elif isinstance(it, Run):
                x, y = apply(a, it.x, it.y)
                self.h._add_run(replace(it, x=x, y=y, height=it.height * sy, angle=it.angle + turn,
                                        width=None if it.width is None else it.width * sx))
            elif isinstance(it, Bitmap):
                x0, y0, x1, y1 = it.box
                box = _box_of([apply(a, x0, y0), apply(a, x1, y0), apply(a, x0, y1), apply(a, x1, y1)])
                if all(map(math.isfinite, box)):
                    self.h.items.append(Bitmap(it.data, it.kind, box))
            elif isinstance(it, Clip):
                clipped = True
                if it.segs is None:
                    self.h._force_clip()
                else:
                    segs = self._map(it.segs, a)
                    if all(map(math.isfinite, (v for s in segs for v in s[1:]))):
                        self.h.items.append(Clip(segs))
        if clipped:
            self.h._force_clip()

    # ---- text ----------------------------------------------------------------------------------------------------

    def _em(self, font, m):
        """The font's em in frame units and in the units its positions are given in (before m)."""
        _, em, unit, _style, _face = font
        down = math.hypot(m[2], m[3])
        if unit == 0:
            frame = em * down
        else:
            wb = self._wb()
            frame = em * self._unit(unit)[1] * math.hypot(wb[2], wb[3])
        return frame, (frame / down if down else 0.0)

    def _run(self, font, text, glyphs, x, y, em, angle, color, width):
        _, _, _, style, face = font
        self.h._add_run(Run(text, x, y, face, em, 700 if style & 1 else 400, bool(style & 2), angle,
                            0 if color is None else color, glyphs, width))

    def _draw_string(self, flags, d):
        brush, fmt_id, n = struct.unpack_from("<III", d)
        x, y, w, h = struct.unpack_from("<4f", d, 12)
        if 28 + 2 * n > len(d):
            raise ValueError
        font = self._obj(flags & 0xFF, "font")
        if font is None:
            self.h.skipped += 1
            return
        text = self.h._wide(d[28:28 + 2 * n], font[4])
        if text is None:
            self.h.skipped += 1
            return
        fmt = self._obj(fmt_id, "format") if fmt_id < SLOTS else None
        align, line_align = (fmt[1], fmt[2]) if fmt else (0, 0)
        m = self._m()
        em, em_w = self._em(font, m)
        if not (math.isfinite(em) and em > 0 and em_w > 0):
            self.h.skipped += 1
            return
        parts = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        tall = len(parts) * _LEADING * em_w
        top = y if h <= 0 or line_align == 0 else y + (h - tall) / 2 if line_align == 1 else y + h - tall
        turn = angle_of(m)
        along = math.hypot(m[0], m[1])
        color = self._color(flags, brush)
        for i, s in enumerate(parts):
            if not s.strip():
                continue
            est = em_w * sum(1.0 if _wide_char(c) else 0.5 for c in s)
            tw = min(est, w) if w > 0 else est
            if align == 1:
                lx = x + (w - tw) / 2 if w > 0 else x - tw / 2
            elif align == 2:
                lx = x + w - tw if w > 0 else x - tw
            else:
                lx = x
            fx, fy = apply(m, lx, top + i * _LEADING * em_w + _ASCENT * em_w)
            width = tw * along if w > 0 and est > w else None  # text that wraps fills its box
            self._run(font, s, None, fx, fy, em, turn, color, width)

    def _draw_driver_string(self, flags, d):
        brush, opts, has_m, n = struct.unpack_from("<IIII", d)
        if 16 + 10 * n > len(d):
            raise ValueError
        self.h.budget.points(n)
        glyphs = struct.unpack_from("<%dH" % n, d, 16)
        pos = struct.unpack_from("<%df" % (2 * n), d, 16 + 2 * n)
        font = self._obj(flags & 0xFF, "font")
        if font is None:
            self.h.skipped += 1
            return
        if not n:
            return
        m = self._m()
        if has_m and 16 + 10 * n + 24 <= len(d):
            t = struct.unpack_from("<6f", d, 16 + 10 * n)
            if good(t):
                m = mul(t, m)
        em, em_p = self._em(font, m)
        if not (math.isfinite(em) and em > 0):
            self.h.skipped += 1
            return
        # glyphs on one baseline, left to right, are one run; a new baseline or a step back starts another
        if opts & 4:  # realized advance: only the first position is given
            groups = [(0, n)]
        else:
            groups, start = [], 0
            for i in range(1, n):
                if abs(pos[2 * i + 1] - pos[2 * i - 1]) > 0.5 * em_p or pos[2 * i] < pos[2 * i - 2]:
                    groups.append((start, i))
                    start = i
            groups.append((start, n))
        turn = angle_of(m)
        color = self._color(flags, brush)
        for g0, g1 in groups:
            part = glyphs[g0:g1]
            if opts & 1:  # cmap lookup: the glyphs are UTF-16 code units
                text = self.h._wide(struct.pack("<%dH" % len(part), *part), font[4])
                if text is None:
                    self.h.skipped += 1
                    continue
                if not text:
                    continue
                gl = None
            else:
                text, gl = "", tuple(part)
            fx, fy = apply(m, pos[2 * g0], pos[2 * g0 + 1])
            width = None
            if g1 - g0 > 1 and not opts & 4:
                lx, ly = apply(m, pos[2 * g1 - 2], pos[2 * g1 - 1])
                span = math.hypot(lx - fx, ly - fy)
                width = span * (g1 - g0) / (g1 - g0 - 1) if span > 0 else None
            self._run(font, text, gl, fx, fy, em, turn, color, width)


def _wide_char(c) -> bool:
    return unicodedata.east_asian_width(c) in "WF"


_HANDLERS = {
    GETDC: Plus._getdc, OBJECT: Plus._object,
    0x400A: Plus._fill_rects, 0x400B: Plus._draw_rects, 0x400C: Plus._fill_polygon, 0x400D: Plus._draw_lines,
    0x400E: Plus._fill_ellipse, 0x400F: Plus._draw_ellipse, 0x4010: Plus._fill_pie, 0x4011: Plus._draw_pie,
    0x4012: Plus._draw_arc, 0x4013: Plus._fill_region, 0x4014: Plus._fill_path, 0x4015: Plus._draw_path,
    0x4016: Plus._fill_closed_curve, 0x4017: Plus._draw_closed_curve, 0x4018: Plus._draw_curve,
    0x4019: Plus._draw_beziers, 0x401A: Plus._draw_image, 0x401B: Plus._draw_image_points,
    0x401C: Plus._draw_string, 0x4036: Plus._draw_driver_string,
    0x4025: Plus._save, 0x4026: Plus._pop, 0x4027: Plus._container, 0x4028: Plus._container_plain, 0x4029: Plus._pop,
    0x402A: Plus._world_set, 0x402B: Plus._world_reset, 0x402C: Plus._world_mul, 0x402D: Plus._world_translate,
    0x402E: Plus._world_scale, 0x402F: Plus._world_rotate, 0x4030: Plus._page,
    0x4031: Plus._clip_reset, 0x4032: Plus._clip_rect, 0x4033: Plus._clip_path, 0x4034: Plus._clip_region,
    0x4035: Plus._clip_offset,
}
