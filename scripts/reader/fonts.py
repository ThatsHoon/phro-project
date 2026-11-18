"""System fonts: find a face by name and map glyph numbers back to text.

EMF and EMF+ figures often draw text as glyph numbers, not code points. The font the run names is found in the
system's font folders (an index of file, size, mtime and names is kept in the reader cache), and its `cmap`
(formats 4 and 12) is inverted: each glyph maps to the lowest code point that reaches it.

Font files are local, but their bytes are treated as untrusted: every offset and count is checked against the file
size, at most limits.FONT_BYTES of one file are read, and a font that fails is skipped.
"""
import json
import os
import struct
import sys
import tempfile
from pathlib import Path

from . import cache, limits

UNMAPPED = False  # set by resolve when glyphs were dropped or no font matched; the caller reads and resets it

_SUFFIXES = {".ttf", ".otf", ".ttc", ".otc"}
_SFNT = {b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"}
_NAME_IDS = (4, 1, 16)  # the order names are listed in: a full name outranks a family
_MAX_FACES = 1024        # faces read from one collection
_MAX_TABLES = 512        # tables in one table directory
_MAX_SUBTABLES = 16      # cmap subtables looked at
_MAX_CODEPOINTS = 0x220000  # code points visited while inverting one cmap

_maps: dict = {}   # (path, index) -> {glyph: code point}, per process
_index: dict = {}  # font_dirs() as a tuple -> {casefolded name: (Path, face index)}


class _Bad(Exception):
    """The font's bytes do not hold together."""


def reset():
    """Forget the per-process memos (tests; a changed font folder)."""
    _maps.clear()
    _index.clear()


def font_dirs() -> list[Path]:
    env = os.environ.get("PHRO_READER_FONTS")
    if env is not None:
        return [Path(p) for p in env.split(os.pathsep) if p]
    home = Path.home()
    if sys.platform == "win32":
        win = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
        local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return [Path(win) / "Fonts", Path(local) / "Microsoft" / "Windows" / "Fonts"]
    if sys.platform == "darwin":
        return [Path("/System/Library/Fonts"), Path("/Library/Fonts"), home / "Library" / "Fonts"]
    return [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), home / ".local" / "share" / "fonts",
            home / ".fonts"]


class _File:
    """Bounds-checked reads from one open font file."""

    def __init__(self, fh, size: int):
        self.fh = fh
        self.size = size

    def get(self, off: int, n: int) -> bytes:
        if off < 0 or n < 0 or off + n > self.size or n > limits.FONT_BYTES:
            raise _Bad("range")
        self.fh.seek(off)
        data = self.fh.read(n)
        if len(data) != n:
            raise _Bad("short read")
        return data


def _open(path):
    """The open file and its size; _Bad when it is not a regular file or is over FONT_BYTES."""
    fh = open(path, "rb")
    try:
        st = os.fstat(fh.fileno())
        if not (st.st_mode & 0o170000) == 0o100000 or st.st_size > limits.FONT_BYTES:
            raise _Bad("size")
        return fh, _File(fh, st.st_size)
    except BaseException:
        fh.close()
        raise


def _faces(f: _File) -> list[int]:
    head = f.get(0, 12)
    if head[:4] == b"ttcf":
        n = struct.unpack(">I", head[8:12])[0]
        if n > _MAX_FACES or 12 + 4 * n > f.size:
            raise _Bad("faces")
        return list(struct.unpack(">%dI" % n, f.get(12, 4 * n))) if n else []
    if head[:4] in _SFNT:
        return [0]
    raise _Bad("not a font")


def _tables(f: _File, off: int) -> dict[bytes, tuple[int, int]]:
    head = f.get(off, 12)
    if head[:4] not in _SFNT:
        raise _Bad("face")
    n = struct.unpack(">H", head[4:6])[0]
    if n > _MAX_TABLES:
        raise _Bad("tables")
    out: dict = {}
    raw = f.get(off + 12, 16 * n)
    for i in range(n):
        tag, _sum, o, ln = struct.unpack(">4sIII", raw[16 * i:16 * i + 16])
        out.setdefault(tag, (o, ln))
    return out


def _names(f: _File, tables) -> list[str]:
    if b"name" not in tables:
        return []
    o, ln = tables[b"name"]
    t = f.get(o, ln)
    if len(t) < 6:
        raise _Bad("name")
    _fmt, count, sto = struct.unpack(">HHH", t[:6])
    if 6 + 12 * count > len(t) or sto > len(t):
        raise _Bad("name records")
    found: dict[int, list[str]] = {i: [] for i in _NAME_IDS}
    for i in range(count):
        plat, enc, _lang, nid, ln2, so = struct.unpack(">6H", t[6 + 12 * i:18 + 12 * i])
        if nid not in _NAME_IDS:
            continue
        a = sto + so
        if a + ln2 > len(t):
            continue
        raw = t[a:a + ln2]
        try:
            if plat in (0, 3):
                s = raw.decode("utf-16-be")
            elif plat == 1 and enc == 0:
                s = raw.decode("mac_roman")
            else:
                continue
        except UnicodeDecodeError:
            continue
        s = " ".join(s.replace("\0", " ").split()).casefold()
        if s and s not in found[nid]:
            found[nid].append(s)
    out: list[str] = []
    for nid in _NAME_IDS:
        out += [s for s in found[nid] if s not in out]
    return out


def _cmap4(t: bytes, o: int, inv: dict, left: list):
    if o + 16 > len(t):
        raise _Bad("cmap4")
    seg = struct.unpack(">H", t[o + 6:o + 8])[0] // 2
    ends, starts, deltas, ros = o + 14, o + 16 + 2 * seg, o + 16 + 4 * seg, o + 16 + 6 * seg
    if ros + 2 * seg > len(t):
        raise _Bad("cmap4 segments")
    for i in range(seg):
        end = struct.unpack_from(">H", t, ends + 2 * i)[0]
        start = struct.unpack_from(">H", t, starts + 2 * i)[0]
        delta = struct.unpack_from(">H", t, deltas + 2 * i)[0]
        ro = struct.unpack_from(">H", t, ros + 2 * i)[0]
        if end < start:
            continue
        left[0] -= end - start + 1
        if left[0] < 0:
            raise _Bad("cmap work")
        for c in range(start, end + 1):
            if ro == 0:
                g = (c + delta) & 0xFFFF
            else:
                a = ros + 2 * i + ro + 2 * (c - start)
                if a + 2 > len(t):
                    continue
                g = struct.unpack_from(">H", t, a)[0]
                if g:
                    g = (g + delta) & 0xFFFF
            if g and not 0xD800 <= c <= 0xDFFF and (g not in inv or c < inv[g]):
                inv[g] = c


def _cmap12(t: bytes, o: int, inv: dict, left: list):
    if o + 16 > len(t):
        raise _Bad("cmap12")
    n = struct.unpack(">I", t[o + 12:o + 16])[0]
    if n > (len(t) - o - 16) // 12:
        raise _Bad("cmap12 groups")
    for i in range(n):
        start, end, g0 = struct.unpack_from(">III", t, o + 16 + 12 * i)
        end = min(end, 0x10FFFF)
        if end < start:
            continue
        left[0] -= end - start + 1
        if left[0] < 0:
            raise _Bad("cmap work")
        for c in range(start, end + 1):
            g = g0 + (c - start)
            if g and not 0xD800 <= c <= 0xDFFF and (g not in inv or c < inv[g]):
                inv[g] = c


def _cmap(f: _File, tables) -> dict[int, int]:
    if b"cmap" not in tables:
        raise _Bad("no cmap")
    o, ln = tables[b"cmap"]
    t = f.get(o, ln)
    if len(t) < 4:
        raise _Bad("cmap")
    n = struct.unpack(">H", t[2:4])[0]
    if 4 + 8 * n > len(t):
        raise _Bad("cmap records")
    subs = []
    for i in range(min(n, _MAX_SUBTABLES)):
        plat, enc, off = struct.unpack(">HHI", t[4 + 8 * i:12 + 8 * i])
        if (plat == 0 or (plat == 3 and enc in (1, 10))) and off + 2 <= len(t) and off not in subs:
            subs.append(off)
    inv: dict[int, int] = {}
    left = [_MAX_CODEPOINTS]
    for off in subs:
        fmt = struct.unpack(">H", t[off:off + 2])[0]
        if fmt == 4:
            _cmap4(t, off, inv, left)
        elif fmt == 12:
            _cmap12(t, off, inv, left)
    return inv


def glyph_map(path, index: int = 0) -> dict[int, int]:
    """Glyph number -> lowest code point, from the face `index` of the font file; {} when it cannot be read."""
    key = (str(path), index)
    got = _maps.get(key)
    if got is not None:
        return got
    try:
        fh, f = _open(path)
        with fh:
            offs = _faces(f)
            if not 0 <= index < len(offs):
                raise _Bad("face index")
            out = _cmap(f, _tables(f, offs[index]))
    except (_Bad, OSError, struct.error, ValueError, MemoryError, OverflowError):
        out = {}
    _maps[key] = out
    return out


def _parse_file(path) -> list[list[str]]:
    """The names of each face of one file; _Bad or OSError when it cannot be read."""
    fh, f = _open(path)
    with fh:
        return [_names(f, _tables(f, off)) for off in _faces(f)]


def _load_rows() -> list:
    try:
        rows = json.loads((cache.root() / "fonts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    ok = (isinstance(rows, list) and all(
        isinstance(r, list) and len(r) == 4 and isinstance(r[0], str) and isinstance(r[1], int)
        and isinstance(r[2], int) and isinstance(r[3], list)
        and all(isinstance(face, list) and all(isinstance(s, str) for s in face) for face in r[3])
        for r in rows))
    return rows if ok else []


def _save_rows(rows: list):
    tmp = None
    try:
        root = cache.root()
        fd, tmp = tempfile.mkstemp(prefix=".fonts-", suffix=".tmp", dir=root)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False)
        os.replace(tmp, root / "fonts.json")
        tmp = None
    except OSError:
        pass
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _scan(dirs) -> list:
    old = {r[0]: r for r in _load_rows()}
    rows: list = []
    cap = limits.FONT_FILES
    for d in dirs:
        for top, subs, files in os.walk(d, followlinks=False):
            subs.sort()
            for name in sorted(files):
                if len(rows) >= cap:
                    return rows
                if os.path.splitext(name)[1].lower() not in _SUFFIXES:
                    continue
                p = os.path.join(top, name)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                prev = old.get(p)
                if prev is not None and prev[1] == st.st_size and prev[2] == st.st_mtime_ns:
                    rows.append(prev)
                    continue
                try:
                    names = _parse_file(p)
                except _Bad as e:
                    if str(e) == "size":
                        continue  # over FONT_BYTES (or not a regular file): not indexed
                    names = []
                except (OSError, struct.error, ValueError, MemoryError, OverflowError):
                    names = []
                rows.append([p, st.st_size, st.st_mtime_ns, names])
    return rows


def _names_index() -> dict:
    dirs = tuple(font_dirs())
    got = _index.get(dirs)
    if got is None:
        before = _load_rows()
        rows = _scan(dirs)
        if rows != before:
            _save_rows(rows)
        got = {}
        for first in (True, False):  # a face's first name (its full name) outranks other faces' family names
            for p, _size, _mt, faces in rows:
                for i, names in enumerate(faces):
                    for n in names[:1] if first else names:
                        got.setdefault(n, (Path(p), i))
        _index[dirs] = got
    return got


def locate(face: str) -> tuple[Path, int] | None:
    """The font file and face index whose family or full name is `face` (any language, ignoring case)."""
    if not isinstance(face, str):
        return None
    key = " ".join(face.replace("\0", " ").split()).casefold()
    if not key:
        return None
    return _names_index().get(key)


def find(face: str) -> Path | None:
    got = locate(face)
    return got[0] if got else None


def resolve(run) -> str | None:
    """The text of a glyph-number run, for vector.text_lines. Unmapped glyphs are dropped; None when no font
    matches or nothing maps. UNMAPPED is set when anything was dropped or unmatched. Never raises."""
    global UNMAPPED
    try:
        got = locate(run.face)
        if got is None:
            UNMAPPED = True
            return None
        inv = glyph_map(got[0], got[1])
        out = []
        for g in run.glyphs or ():
            c = inv.get(g)
            if c is None:
                UNMAPPED = True
            else:
                out.append(chr(c))
        return "".join(out) or None
    except Exception:  # noqa: BLE001 - a picture never makes a document fail
        UNMAPPED = True
        return None
