"""Safe zip package access and namespace-agnostic XML helpers."""
import contextlib
import io
import posixpath
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
import zlib
from urllib.parse import unquote

from .. import limits
from ..limits import ReadError


_BOMS = ((bytes([255, 254, 0, 0]), "utf-32"), (bytes([0, 0, 254, 255]), "utf-32"),
         (bytes([255, 254]), "utf-16"), (bytes([254, 255]), "utf-16"))


def _decode_guess(data: bytes) -> str:
    """Best-effort text of UTF-16/32 data (BOM or NULs in the first 4 bytes); empty otherwise."""
    head = data[:4]
    enc = next((e for bom, e in _BOMS if head.startswith(bom)), None)
    if enc is None:
        if 0 not in head:
            return ""
        if head[:3] == bytes(3):
            enc = "utf-32-be"
        elif head[1:4] == bytes(3):
            enc = "utf-32-le"
        elif head[0] == 0:
            enc = "utf-16-be"
        else:
            enc = "utf-16-le"
    return data.decode(enc, errors="replace")


def _declares_entities(data: bytes) -> bool:
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        return True
    text = _decode_guess(data)
    return "<!DOCTYPE" in text or "<!ENTITY" in text


# what expat raises for XML it cannot read: a ParseError, a LookupError for an unknown encoding name, a ValueError
# (UnicodeError among them) for a multi-byte or unusable one
_XML_ERRORS = (ET.ParseError, LookupError, ValueError)


_held = None
_late = None
_PIECE = 1 << 20  # bytes of a big XML part fed to the parser between two looks at the read's clock


@contextlib.contextmanager
def clocked(late):
    """While this is open an XML part longer than _PIECE is parsed a piece at a time and asks `late` (the read's
    Context.late, or None for no limit) after each: once it says yes the parse stops and gives the tree built so far,
    its open elements as far as they were read, and the format stops at its own next look."""
    global _late
    before, _late = _late, late
    try:
        yield
    finally:
        _late = before


@contextlib.contextmanager
def charged(objects):
    """While this is open an embedded object of the document is being read: every Package opened and every XML
    tree built is charged to `objects`, the document's embedded.Objects (limits.EMBED_TOTAL, limits.EMBED_NODES)."""
    global _held
    before, _held = _held, objects
    try:
        yield
    finally:
        _held = before


def parse_xml(data: bytes):
    if _held is not None or _late is not None and len(data) > _PIECE:
        return parse_xml_ns(data)[0]
    if _declares_entities(data):
        raise ReadError("damaged", "XML declares a DOCTYPE or ENTITY")
    try:
        return ET.fromstring(data)
    except _XML_ERRORS as e:
        raise ReadError("damaged", f"XML parse error: {e}")


class _NsBuilder(ET.TreeBuilder):
    """TreeBuilder that also records every namespace declaration: prefix -> set of URIs. With `max_nodes`, the
    element past it raises too_large before it is built."""

    def __init__(self, max_nodes=None):
        super().__init__()
        self.ns = {}
        self.left = max_nodes
        self.top = None

    def start_ns(self, prefix, uri):
        self.ns.setdefault(prefix, set()).add(uri)

    def start(self, tag, attrs):
        if self.left is not None:
            self.left -= 1
            if self.left < 0:
                raise _TooMany()
        el = super().start(tag, attrs)
        if self.top is None:
            self.top = el
        return el


class _TooMany(Exception):
    pass


def parse_xml_ns(data: bytes, max_nodes: int | None = None):
    """(root, {prefix: {uri, ...}}) with every declaration in the document; same refusals as parse_xml. With
    `max_nodes`, a document of more elements is too_large (part), refused before the one past it is built, since
    ElementTree holds about 80 bytes an element however small its text."""
    if _declares_entities(data):
        raise ReadError("damaged", "XML declares a DOCTYPE or ENTITY")
    held = _held
    if held is not None:
        left = max(limits.EMBED_NODES - held.nodes, 0)
        max_nodes = left if max_nodes is None else min(max_nodes, left)
    builder = _NsBuilder(max_nodes)
    late = _late if len(data) > _PIECE else None
    try:
        parser = ET.XMLParser(target=builder)
        if late is None:
            parser.feed(data)
        else:
            for at in range(0, len(data), _PIECE):
                parser.feed(data[at:at + _PIECE])
                if at + _PIECE < len(data) and late():
                    if builder.top is None:
                        raise ReadError("damaged", "XML not read: the time ran out before its first element")
                    return builder.top, builder.ns
        root = parser.close()
    except _TooMany:
        raise ReadError("too_large", f"{limits.BIG_PART}: XML of more than {max_nodes} elements") from None
    except _XML_ERRORS as e:
        raise ReadError("damaged", f"XML parse error: {e}")
    finally:
        if held is not None:
            held.nodes += max_nodes - max(builder.left, 0)
    return root, builder.ns


_LOCAL: dict = {}
_LOCALS = 4096  # tag names `local` remembers


def local(tag: str) -> str:
    name = _LOCAL.get(tag)
    if name is None:
        name = tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""
        if len(_LOCAL) < _LOCALS:
            _LOCAL[tag] = name
    return name


def attr(el, name: str, default=None):
    n = -len(name)
    for k, v in el.attrib.items():
        if k.endswith(name) and (len(k) == -n or k[n - 1] == "}"):
            return v
    return default


def kids(el, name) -> list:
    return [c for c in el if local(c.tag) == name]


def walk(el, name):
    return [e for e in el.iter() if e is not el and local(e.tag) == name]


def first(el, name):
    for e in el.iter():
        if e is not el and local(e.tag) == name:
            return e
    return None


def resolve(base_part: str, target: str) -> str:
    target = unquote(target)
    if target.startswith("/"):
        joined = target.lstrip("/")
    else:
        joined = posixpath.join(posixpath.dirname(base_part), target)
    return posixpath.normpath(joined).lstrip("/")


@dataclass(frozen=True)
class Rel:
    id: str
    type: str
    target: str
    external: bool


class _Stream:
    """A zip member's stream whose read turns a damaged member into ReadError("damaged")."""

    def __init__(self, f, real):
        self._f, self._real = f, real

    def read(self, n=-1):
        try:
            return self._f.read(n)
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, UnicodeDecodeError) as e:
            raise ReadError("damaged", f"{self._real}: {e}")

    def close(self):
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def iterparse(stream, events=("end",), chunk=65536):
    """(event, element) pairs from the binary `stream`, parsed incrementally; the caller removes the elements it is
    done with, nothing is kept here. Until the root starts, a DOCTYPE or ENTITY is refused before its chunk is fed."""
    want = set(events)
    parser = ET.XMLPullParser(tuple(want | {"start"}))
    tail, in_root = b"", False
    try:
        while True:
            data = stream.read(chunk)
            if not data:
                break
            if not in_root:
                window = tail + data
                if _declares_entities(window) or _declares_entities(window.replace(b"\x00", b"")):
                    raise ReadError("damaged", "XML declares a DOCTYPE or ENTITY")
                tail = window[-64:]
            parser.feed(data)
            for ev, el in parser.read_events():
                in_root = True
                if ev in want:
                    yield ev, el
        parser.close()
        for ev, el in parser.read_events():
            if ev in want:
                yield ev, el
    except _XML_ERRORS as e:
        raise ReadError("damaged", f"XML parse error: {e}")


class Package:
    def __init__(self, path: Path, objects=None):
        """`path` is a path, or the package's bytes (read in memory; every check below is the same). `objects` is
        the document's embedded.Objects when the package is one the document embeds, as is every package opened
        under `charged`: what its members inflate to is charged to the document's EMBED_TOTAL as well."""
        if isinstance(path, (bytes, bytearray, memoryview)):
            path = io.BytesIO(bytes(path))
        try:
            self._zip = zipfile.ZipFile(path)
        except zipfile.BadZipFile as e:
            raise ReadError("damaged", f"not a zip: {e}")
        except UnicodeDecodeError as e:  # a member name flagged UTF-8 that is not
            raise ReadError("damaged", f"zip member name: {e}")
        self.names = [i.filename for i in self._zip.infolist()]
        self._lower = {}
        for n in self.names:
            k = n.lower()
            if k in self._lower and self._lower[k] != n:
                raise ReadError("damaged", "two members differ only by case")
            self._lower[k] = n
        self._seen = set()
        self._total = 0
        self._objects = _held if objects is None else objects

    def close(self):
        self._zip.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _find(self, name):
        if name in self.names:
            return name
        return self._lower.get(name.lower())

    def has(self, name) -> bool:
        return self._find(name) is not None

    def size(self, name) -> int:
        """The uncompressed size the directory gives the member `name`, read without inflating it."""
        real = self._find(name)
        if real is None:
            raise KeyError(name)
        return self._zip.getinfo(real).file_size

    def method(self, name) -> int:
        """The compression method the directory gives the member `name` (0 stored, 8 deflated), read without
        inflating it and without charging the zip total."""
        real = self._find(name)
        if real is None:
            raise KeyError(name)
        return self._zip.getinfo(real).compress_type

    def _check(self, name) -> zipfile.ZipInfo:
        """The directory entry of member `name`, after the checks `read` and `open` share; KeyError if absent."""
        real = self._find(name)
        if real is None:
            raise KeyError(name)
        info = self._zip.getinfo(real)
        if info.flag_bits & 1:
            raise ReadError("encrypted", real)
        # a refused member is not charged, so a caller that skips it can go on reading the rest
        if (info.file_size > limits.ZIP_RATIO_FLOOR
                and info.file_size / max(info.compress_size, 1) > limits.ZIP_RATIO):
            raise ReadError("too_large", f"{limits.BIG_PART}: {real}")
        if real not in self._seen:
            held = self._objects
            if self._total + info.file_size > limits.ZIP_TOTAL or (
                    held is not None and held.inflated + info.file_size > limits.EMBED_TOTAL):
                raise ReadError("too_large", f"{limits.BIG_TOTAL}: zip uncompressed size")
            self._seen.add(real)
            self._total += info.file_size
            if held is not None:
                held.inflated += info.file_size
        return info

    def read(self, name) -> bytes:
        info = self._check(name)
        try:  # a UnicodeDecodeError is a local header whose name is flagged UTF-8 and is not
            return self._zip.read(info)
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, UnicodeDecodeError) as e:
            raise ReadError("damaged", f"{info.filename}: {e}")

    def open(self, name):
        """A context-managed binary stream of member `name`, after the checks `read` makes."""
        info = self._check(name)
        try:
            return _Stream(self._zip.open(info), info.filename)
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, UnicodeDecodeError) as e:
            raise ReadError("damaged", f"{info.filename}: {e}")

    def xml(self, name):
        if not self.has(name):
            return None
        return parse_xml(self.read(name))

    def rels(self, part: str) -> dict:
        d, f = posixpath.split(part)
        rel_name = posixpath.join(d, "_rels", f + ".rels") if f else "_rels/.rels"
        root = self.xml(rel_name)
        out = {}
        if root is None:
            return out
        for r in kids(root, "Relationship"):
            rid = r.get("Id", "")
            ext = r.get("TargetMode") == "External"
            t = r.get("Target", "")
            out[rid] = Rel(rid, r.get("Type", ""), t if ext else resolve(part, t), ext)
        return out
