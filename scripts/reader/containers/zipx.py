"""Safe zip package access and namespace-agnostic XML helpers."""
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


def parse_xml(data: bytes):
    if _declares_entities(data):
        raise ReadError("damaged", "XML declares a DOCTYPE or ENTITY")
    try:
        return ET.fromstring(data)
    except _XML_ERRORS as e:
        raise ReadError("damaged", f"XML parse error: {e}")


class _NsBuilder(ET.TreeBuilder):
    """TreeBuilder that also records every namespace declaration: prefix -> set of URIs."""

    def __init__(self):
        super().__init__()
        self.ns = {}

    def start_ns(self, prefix, uri):
        self.ns.setdefault(prefix, set()).add(uri)


def parse_xml_ns(data: bytes):
    """(root, {prefix: {uri, ...}}) with every declaration in the document; same refusals as parse_xml."""
    if _declares_entities(data):
        raise ReadError("damaged", "XML declares a DOCTYPE or ENTITY")
    builder = _NsBuilder()
    try:
        parser = ET.XMLParser(target=builder)
        parser.feed(data)
        root = parser.close()
    except _XML_ERRORS as e:
        raise ReadError("damaged", f"XML parse error: {e}")
    return root, builder.ns


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def attr(el, name: str, default=None):
    for k, v in el.attrib.items():
        if local(k) == name:
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
    def __init__(self, path: Path):
        """`path` is a path, or the package's bytes (read in memory; every check below is the same)."""
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
            if self._total + info.file_size > limits.ZIP_TOTAL:
                raise ReadError("too_large", f"{limits.BIG_TOTAL}: zip uncompressed size")
            self._seen.add(real)
            self._total += info.file_size
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
