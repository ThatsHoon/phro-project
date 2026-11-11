"""Safe zip package access and namespace-agnostic XML helpers."""
import posixpath
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
import zlib
from urllib.parse import unquote

from .. import limits
from ..limits import ReadError


def parse_xml(data: bytes):
    head = data[:4096]
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        raise ReadError("damaged", "XML declares a DOCTYPE or ENTITY")
    try:
        return ET.fromstring(data)
    except ET.ParseError as e:
        raise ReadError("damaged", f"XML parse error: {e}")


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


class Package:
    def __init__(self, path: Path):
        try:
            self._zip = zipfile.ZipFile(path)
        except zipfile.BadZipFile as e:
            raise ReadError("damaged", f"not a zip: {e}")
        self.names = [i.filename for i in self._zip.infolist()]
        self._lower = {}
        for n in self.names:
            k = n.lower()
            if k in self._lower and self._lower[k] != n:
                raise ReadError("damaged", "two members differ only by case")
            self._lower[k] = n
        self._total = 0

    def _find(self, name):
        if name in self.names:
            return name
        return self._lower.get(name.lower())

    def has(self, name) -> bool:
        return self._find(name) is not None

    def read(self, name) -> bytes:
        real = self._find(name)
        if real is None:
            raise KeyError(name)
        info = self._zip.getinfo(real)
        if info.flag_bits & 1:
            raise ReadError("encrypted", real)
        self._total += info.file_size
        if self._total > limits.ZIP_TOTAL:
            raise ReadError("too_large", "zip total uncompressed size")
        if (info.file_size > limits.ZIP_RATIO_FLOOR
                and info.file_size / max(info.compress_size, 1) > limits.ZIP_RATIO):
            raise ReadError("too_large", f"compression ratio of {real}")
        try:
            return self._zip.read(info)
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as e:
            raise ReadError("damaged", f"{real}: {e}")

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
