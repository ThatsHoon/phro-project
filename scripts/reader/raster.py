"""Figures (WMF, EMF, PICT, WPG, TIFF) to a PNG and their text: the one call Assets makes.

A picture is parsed into its drawing list, its text lines are taken from it, and a backend draws the pixels. On
Windows that is GDI+ through ctypes: WMF and EMF bytes go to it as they are, PICT and WPG as the EMF emf_write writes
from the drawing, TIFF as a bitmap. GDI+ reads memory streams only; nothing is written to disk. ctypes is imported
inside the GDI+ functions alone, so importing this module stays light.
"""
import math
import os
import struct
import sys
import time
from dataclasses import dataclass, field

from . import emf_write, fonts, images, limits
from .limits import ReadError
from .vector import text_lines

KINDS = ("wmf", "emf", "pict", "wpg", "tiff")
DEADLINE_ENV = "PHRO_READER_DEADLINE"

_PLACEABLE_KEY = 0x9AC6CDD7
_TWIPS = 1440          # units per inch of the placeable header a bare WMF is given
_BARE_EDGE = 512       # the long side, in pixels, of a bare WMF the parser gave no frame for
_ARGB32 = 0x26200A     # PixelFormat32bppARGB
_PAGE_DIM = "{7462DC86-6180-4C7E-8E3F-EE7333A7A483}"  # FrameDimensionPage
_PNG_CLSID = "{557CF406-1A04-11D3-9A73-0000F81EF32E}"  # the PNG encoder


@dataclass
class Figure:
    png: bytes | None
    lines: list = field(default_factory=list)
    warnings: set = field(default_factory=set)


_backend = None


def backend() -> str:
    """"gdiplus" on Windows when gdiplus.dll loads and starts, else "none"; worked out once a process."""
    global _backend
    if _backend is None:
        _backend = "gdiplus" if sys.platform == "win32" and _gdip() is not None else "none"
    return _backend


def target_size(w_px: int, h_px: int) -> tuple[int, int]:
    """The PNG size for a picture of w_px x h_px: the long side up to FIGURE_EDGE, at most twice the picture, the
    pixel count under images._MAX_PIXELS, the aspect kept and each side at least 1."""
    w = max(1, int(w_px)) if math.isfinite(w_px) else 1
    h = max(1, int(h_px)) if math.isfinite(h_px) else 1
    k = min(2.0, max(1, limits.FIGURE_EDGE) / max(w, h))
    tw, th = max(1, round(w * k)), max(1, round(h * k))
    cap = images._MAX_PIXELS
    if tw * th > cap:
        q = math.sqrt(cap / (tw * th))
        tw, th = max(1, int(tw * q)), max(1, int(th * q))
        if tw * th > cap:
            if tw >= th:
                tw = max(1, cap // th)
            else:
                th = max(1, cap // tw)
    return tw, th


def _env_deadline() -> float | None:
    v = os.environ.get(DEADLINE_ENV)
    if not v:
        return None
    try:
        t = float(v)
    except ValueError:
        return None
    return t if math.isfinite(t) else None


def _parse(kind: str, data: bytes, codepage: str, mono):
    if kind in ("wmf", "emf"):
        from . import metafile
        return metafile.parse(data, codepage, mono)
    if kind == "pict":
        from . import pict
        return pict.parse(data, mono)
    from . import wpg
    return wpg.parse(data, mono)


def convert(kind: str, data: bytes, *, codepage: str = "", page: int = 0, deadline: float | None = None) -> Figure:
    """The PNG (or None) and text lines of one picture, with the document warnings it raises. `deadline` is a
    wall-clock time (time.time()); None reads PHRO_READER_DEADLINE. Never raises."""
    fig = Figure(None, [], set())
    if kind not in KINDS or not isinstance(data, (bytes, bytearray, memoryview)):
        return fig
    data = bytes(data)
    if deadline is None:
        deadline = _env_deadline()
    mono = time.monotonic() + (deadline - time.time()) if deadline is not None else None
    d = None
    refused = False  # the parser said the bytes are not the format: they are never handed to GDI+
    if kind != "tiff":
        fonts.UNMAPPED = False
        try:
            d = _parse(kind, data, codepage, mono)
        except ReadError as e:
            refused = e.kind == "damaged"
        except Exception:  # noqa: BLE001 - a picture never makes a document fail
            d = None
        if d is not None:
            if d.damaged:
                fig.warnings.add("figure damaged")
            try:
                lines, cut = text_lines(d, fonts.resolve)
            except Exception:  # noqa: BLE001
                lines, cut = [], False
            fig.lines = lines
            if cut:
                fig.warnings.add("figure text cut")
            if fonts.UNMAPPED:
                fig.warnings.add("figure glyphs unmapped")
        fonts.UNMAPPED = False
    if deadline is not None and deadline - time.time() < limits.FIGURE_RESERVE:
        fig.warnings.add("figures not rendered: time")
        return fig
    if refused or backend() != "gdiplus":
        return fig
    try:
        fig.png = _render_gdiplus(kind, data, d, page)
    except Exception:  # noqa: BLE001 - any failure leaves the picture unconverted
        fig.png = None
    return fig


def _meta_kind(data: bytes) -> str | None:
    """"placeable", "wmf" (no placeable header) or "emf" by the header, else None."""
    from . import metafile
    if data[:4] == struct.pack("<I", _PLACEABLE_KEY):
        return "placeable" if metafile._is_plain_wmf(data[22:]) else None
    if metafile._is_plain_wmf(data):
        return "wmf"
    if metafile._is_emf(data):
        return "emf"
    return None


def _placeable(data: bytes, frame) -> bytes:
    """A bare WMF with a placeable header in front: the box is the parser's frame, else 512 px square, at 1440
    units per inch; the checksum is the XOR of the header's first ten words."""
    box = None
    if frame is not None and len(frame) == 4 and all(isinstance(v, (int, float)) and math.isfinite(v) for v in frame):
        x0, y0 = min(frame[0], frame[2]), min(frame[1], frame[3])
        x1, y1 = max(frame[0], frame[2]), max(frame[1], frame[3])

        def c16(v):
            return int(max(-32768, min(32767, round(v))))
        x0, y0, x1, y1 = c16(x0), c16(y0), c16(x1), c16(y1)
        if x1 <= x0:
            x0, x1 = (x0, x0 + 1) if x0 < 32767 else (x0 - 1, x0)
        if y1 <= y0:
            y0, y1 = (y0, y0 + 1) if y0 < 32767 else (y0 - 1, y0)
        box = (x0, y0, x1, y1)
    if box is None:
        box = (0, 0, _BARE_EDGE, _BARE_EDGE)
    head = struct.pack("<IH4hHI", _PLACEABLE_KEY, 0, *box, _TWIPS, 0)
    check = 0
    for (w,) in struct.iter_unpack("<H", head):
        check ^= w
    return head + struct.pack("<H", check) + data


# ---- TIFF pages ------------------------------------------------------------------------------------------------------

def _tiff_ifds(data: bytes) -> int:
    """Pages of a TIFF by walking its IFD chain, at most TIFF_PAGES, stopping at a loop or past the end."""
    if data[:4] == b"II*\x00":
        e = "<"
    elif data[:4] == b"MM\x00*":
        e = ">"
    else:
        return 0
    if len(data) < 8:
        return 0
    off = struct.unpack_from(e + "I", data, 4)[0]
    seen, n = set(), 0
    while off and n < limits.TIFF_PAGES and off not in seen and off + 2 <= len(data):
        seen.add(off)
        nxt = off + 2 + 12 * struct.unpack_from(e + "H", data, off)[0]
        if nxt + 4 > len(data):
            break
        n += 1
        off = struct.unpack_from(e + "I", data, nxt)[0]
    return n


def tiff_pages(data: bytes) -> int:
    """Pages of a TIFF, at most TIFF_PAGES: GDI+'s count when it is there, else the IFD chain; 0 when not a TIFF."""
    data = bytes(data)
    if images.sniff(data) != "tiff":
        return 0
    if backend() == "gdiplus":
        try:
            n = _gdip_frames(data)
            if n is not None:
                return min(n, limits.TIFF_PAGES)
        except Exception:  # noqa: BLE001
            pass
    try:
        return _tiff_ifds(data)
    except struct.error:
        return 0


# ---- GDI+ ------------------------------------------------------------------------------------------------------------

class _Fail(Exception):
    pass


_lib = None
_UNLOADED = object()


def _gdip():
    """GDI+ loaded, its functions typed and started once a process (shut down at exit); None when it cannot be."""
    global _lib
    if _lib is None:
        _lib = _UNLOADED
        try:
            _lib = _load()
        except Exception:  # noqa: BLE001 - no GDI+: the backend is "none"
            _lib = _UNLOADED
    return None if _lib is _UNLOADED else _lib


def _load():
    import atexit
    import ctypes
    import uuid
    from ctypes import POINTER, Structure, c_int, c_size_t, c_uint, c_uint32, c_ubyte, c_uint16, c_void_p

    class GUID(Structure):
        _fields_ = [("Data1", c_uint32), ("Data2", c_uint16), ("Data3", c_uint16), ("Data4", c_ubyte * 8)]

    class StartupInput(Structure):
        _fields_ = [("GdiplusVersion", c_uint32), ("DebugEventCallback", c_void_p),
                    ("SuppressBackgroundThread", c_int), ("SuppressExternalCodecs", c_int)]

    class Rect(Structure):
        _fields_ = [("X", c_int), ("Y", c_int), ("Width", c_int), ("Height", c_int)]

    class BitmapData(Structure):
        _fields_ = [("Width", c_uint), ("Height", c_uint), ("Stride", c_int), ("PixelFormat", c_int),
                    ("Scan0", c_void_p), ("Reserved", c_size_t)]

    gdip = ctypes.WinDLL("gdiplus")
    shlwapi = ctypes.WinDLL("shlwapi")
    P, PP, PU, PG, I, U = c_void_p, POINTER(c_void_p), POINTER(c_uint), POINTER(GUID), c_int, c_uint
    sigs = {
        "GdiplusStartup": (I, [POINTER(c_size_t), POINTER(StartupInput), P]),
        "GdiplusShutdown": (None, [c_size_t]),
        "GdipCreateMetafileFromStream": (I, [P, PP]),
        "GdipCreateBitmapFromStream": (I, [P, PP]),
        "GdipImageGetFrameCount": (I, [P, PG, PU]),
        "GdipImageSelectActiveFrame": (I, [P, PG, U]),
        "GdipGetImageWidth": (I, [P, PU]),
        "GdipGetImageHeight": (I, [P, PU]),
        "GdipCreateBitmapFromScan0": (I, [I, I, I, I, P, PP]),
        "GdipGetImageGraphicsContext": (I, [P, PP]),
        "GdipGraphicsClear": (I, [P, c_uint32]),
        "GdipSetSmoothingMode": (I, [P, I]),
        "GdipSetTextRenderingHint": (I, [P, I]),
        "GdipSetInterpolationMode": (I, [P, I]),
        "GdipDrawImageRectI": (I, [P, P, I, I, I, I]),
        "GdipSaveImageToStream": (I, [P, P, PG, P]),
        "GdipBitmapLockBits": (I, [P, POINTER(Rect), U, I, POINTER(BitmapData)]),
        "GdipBitmapUnlockBits": (I, [P, POINTER(BitmapData)]),
        "GdipDeleteGraphics": (I, [P]),
        "GdipDisposeImage": (I, [P]),
    }
    lib = type("GdiPlus", (), {})()
    for name, (res, args) in sigs.items():
        fn = getattr(gdip, name)
        fn.restype, fn.argtypes = res, args
        setattr(lib, name, fn)
    mem = shlwapi.SHCreateMemStream
    mem.restype, mem.argtypes = c_void_p, [ctypes.c_char_p, c_uint]
    lib.SHCreateMemStream = mem
    lib.ct, lib.GUID, lib.Rect, lib.BitmapData = ctypes, GUID, Rect, BitmapData
    lib.page_dim = GUID.from_buffer_copy(uuid.UUID(_PAGE_DIM).bytes_le)
    lib.png_clsid = GUID.from_buffer_copy(uuid.UUID(_PNG_CLSID).bytes_le)

    token = c_size_t(0)
    si = StartupInput(1, None, 0, 0)
    if lib.GdiplusStartup(ctypes.byref(token), ctypes.byref(si), None) != 0:
        raise _Fail("GdiplusStartup")
    atexit.register(lib.GdiplusShutdown, token.value)
    return lib


def _ok(status):
    if status != 0:
        raise _Fail(status)


def _vfunc(g, obj, index, restype, *argtypes):
    """The COM method at `index` of obj's vtable, typed by an explicit WINFUNCTYPE prototype."""
    ct = g.ct
    vtbl = ct.cast(ct.c_void_p(obj), ct.POINTER(ct.POINTER(ct.c_void_p))).contents
    return ct.WINFUNCTYPE(restype, ct.c_void_p, *argtypes)(vtbl[index])


def _release(g, stream):
    if stream:
        _vfunc(g, stream, 2, g.ct.c_ulong)(stream)  # IUnknown::Release


def _stream(g, data: bytes):
    s = g.SHCreateMemStream(data if data else None, len(data))
    if not s:
        raise _Fail("SHCreateMemStream")
    return s


def _stream_bytes(g, stream) -> bytes:
    """The whole content of an IStream, through Stat, Seek and Read."""
    ct = g.ct

    class STATSTG(ct.Structure):
        _fields_ = [("pwcsName", ct.c_void_p), ("type", ct.c_ulong), ("cbSize", ct.c_ulonglong),
                    ("mtime", ct.c_ulonglong), ("ctime", ct.c_ulonglong), ("atime", ct.c_ulonglong),
                    ("grfMode", ct.c_ulong), ("grfLocksSupported", ct.c_ulong), ("clsid", g.GUID),
                    ("grfStateBits", ct.c_ulong), ("reserved", ct.c_ulong)]

    st = STATSTG()
    stat = _vfunc(g, stream, 12, ct.c_long, ct.POINTER(STATSTG), ct.c_ulong)
    if stat(stream, ct.byref(st), 1) != 0:  # STATFLAG_NONAME: no name is allocated
        raise _Fail("Stat")
    size = st.cbSize
    if not 0 < size <= limits.IMAGE_BYTES:
        raise _Fail("size")
    seek = _vfunc(g, stream, 5, ct.c_long, ct.c_longlong, ct.c_ulong, ct.POINTER(ct.c_ulonglong))
    if seek(stream, 0, 0, None) != 0:  # STREAM_SEEK_SET
        raise _Fail("Seek")
    buf = ct.create_string_buffer(size)
    got = ct.c_ulong(0)
    read = _vfunc(g, stream, 3, ct.c_long, ct.c_void_p, ct.c_ulong, ct.POINTER(ct.c_ulong))
    if read(stream, buf, size, ct.byref(got)) < 0 or got.value != size:
        raise _Fail("Read")
    return buf.raw[:size]


def _image_size(g, img) -> tuple[int, int]:
    ct = g.ct
    w, h = ct.c_uint(0), ct.c_uint(0)
    _ok(g.GdipGetImageWidth(img, ct.byref(w)))
    _ok(g.GdipGetImageHeight(img, ct.byref(h)))
    return w.value, h.value


def _draw(g, img, tw: int, th: int, argb_out: bool = False):
    """img drawn into a white tw x th 32-bit ARGB bitmap: the PNG bytes, or with argb_out the BGRA rows top-down."""
    ct = g.ct
    bmp = ct.c_void_p()
    _ok(g.GdipCreateBitmapFromScan0(tw, th, 0, _ARGB32, None, ct.byref(bmp)))
    try:
        gr = ct.c_void_p()
        _ok(g.GdipGetImageGraphicsContext(bmp, ct.byref(gr)))
        try:
            _ok(g.GdipGraphicsClear(gr, 0xFFFFFFFF))
            _ok(g.GdipSetSmoothingMode(gr, 4))       # SmoothingModeAntiAlias
            _ok(g.GdipSetTextRenderingHint(gr, 4))   # TextRenderingHintAntiAlias
            _ok(g.GdipSetInterpolationMode(gr, 7))   # InterpolationModeHighQualityBicubic
            _ok(g.GdipDrawImageRectI(gr, img, 0, 0, tw, th))
        finally:
            g.GdipDeleteGraphics(gr)
        if argb_out:
            return _bits(g, bmp, tw, th)
        out = _stream(g, b"")
        try:
            _ok(g.GdipSaveImageToStream(bmp, out, ct.byref(g.png_clsid), None))
            png = _stream_bytes(g, out)
        finally:
            _release(g, out)
        if png[:8] != b"\x89PNG\r\n\x1a\n":
            raise _Fail("png")
        return png
    finally:
        if bmp:
            g.GdipDisposeImage(bmp)


def _bits(g, bmp, w: int, h: int) -> bytes:
    ct = g.ct
    rect = g.Rect(0, 0, w, h)
    bd = g.BitmapData()
    _ok(g.GdipBitmapLockBits(bmp, ct.byref(rect), 1, _ARGB32, ct.byref(bd)))  # ImageLockModeRead
    try:
        if not bd.Scan0:
            raise _Fail("Scan0")
        if bd.Stride == 4 * w:
            return ct.string_at(bd.Scan0, 4 * w * h)
        return b"".join(ct.string_at(bd.Scan0 + y * bd.Stride, 4 * w) for y in range(h))
    finally:
        g.GdipBitmapUnlockBits(bmp, ct.byref(bd))


def _with_image(g, data: bytes, create, use):
    """use(img) over the image `create` makes from a memory stream of `data`; the image and stream are freed."""
    ct = g.ct
    s = _stream(g, data)
    try:
        img = ct.c_void_p()
        _ok(create(s, ct.byref(img)))
        try:
            return use(img)
        finally:
            if img:
                g.GdipDisposeImage(img)
    finally:
        _release(g, s)


def _metafile_png(g, data: bytes, size) -> bytes:
    def use(img):
        tw, th = size if size is not None else target_size(*_image_size(g, img))
        return _draw(g, img, tw, th)
    return _with_image(g, data, g.GdipCreateMetafileFromStream, use)


def _decoder(g):
    """decode(data, kind) for emf_write: a PNG or JPEG flattened on white as BGRA rows top-down, or None."""
    def decode(data, kind):
        if images.sniff(data) != kind:
            return None

        def use(img):
            w, h = _image_size(g, img)
            if not 0 < w * h <= images._MAX_PIXELS:
                return None
            return w, h, _draw(g, img, w, h, argb_out=True)
        try:
            return _with_image(g, bytes(data), g.GdipCreateBitmapFromStream, use)
        except Exception:  # noqa: BLE001
            return None
    return decode


def _gdip_frames(data: bytes) -> int | None:
    g = _gdip()
    if g is None:
        return None
    ct = g.ct

    def use(img):
        n = ct.c_uint(0)
        _ok(g.GdipImageGetFrameCount(img, ct.byref(g.page_dim), ct.byref(n)))
        return n.value
    return _with_image(g, data, g.GdipCreateBitmapFromStream, use)


def _tiff_png(g, data: bytes, page: int) -> bytes | None:
    ct = g.ct
    if images.sniff(data) != "tiff" or not 0 <= page < limits.TIFF_PAGES:
        return None

    def use(img):
        n = ct.c_uint(0)
        _ok(g.GdipImageGetFrameCount(img, ct.byref(g.page_dim), ct.byref(n)))
        if page >= n.value:
            return None
        _ok(g.GdipImageSelectActiveFrame(img, ct.byref(g.page_dim), page))
        w, h = _image_size(g, img)
        if not 0 < w * h <= images._MAX_PIXELS:
            return None
        return _draw(g, img, *target_size(w, h))
    return _with_image(g, data, g.GdipCreateBitmapFromStream, use)


def _render_gdiplus(kind: str, data: bytes, d, page: int) -> bytes | None:
    g = _gdip()
    if g is None:
        return None
    if kind == "tiff":
        return _tiff_png(g, data, page)
    if kind in ("pict", "wpg"):
        if d is None:
            return None
        return _metafile_png(g, emf_write.write(d, _decoder(g)), target_size(*d.size_px()))
    mk = _meta_kind(data)
    if mk is None:
        return None
    size = target_size(*d.size_px()) if d is not None else None
    if mk == "wmf":
        data = _placeable(data, d.frame if d is not None else None)
        if size is None:
            size = target_size(_BARE_EDGE, _BARE_EDGE)
    return _metafile_png(g, data, size)
