"""OOXML entry (Word and PowerPoint packages) and the helpers both body parsers share.

The main part is found through the package relationships, never through [Content_Types].xml,
and elements match by local name, so Transitional and Strict files take the same path.
ooxml_word imports this module at load time; this module imports ooxml_word only inside parse(),
so there is no import cycle.
"""
import re
from pathlib import Path

from ..charts import chart_table
from ..containers.zipx import Package, attr, local, parse_xml
from ..limits import ReadError
from ..model import Image

_CFB_MAGIC = bytes.fromhex("d0cf11e0")
_ENCRYPTED = "EncryptedPackage".encode("utf-16-le")
_CFB_SCAN = 64 * 1024
_XMLNS = re.compile(rb"""xmlns:([A-Za-z_][\w.\-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")

UNDERSTOOD = frozenset({"wps", "wpg", "wpc", "wpi", "wp14", "w14", "w15", "w16se", "a14", "c14", "v", "o", "w10"})


# ---- shared per-package caches (rels, parsed parts, member bytes, chart tables)
def _cache(pkg) -> dict:
    c = pkg.__dict__.get("_ooxml")
    if c is None:
        c = pkg.__dict__["_ooxml"] = {"rels": {}, "xml": {}, "bytes": {}, "charts": {}}
    return c


def rels(pkg, part, ctx=None) -> dict:
    """Relationships of `part`, cached; a damaged .rels file reads as empty (counted when ctx is given)."""
    c = _cache(pkg)["rels"]
    if part not in c:
        try:
            c[part] = pkg.rels(part)
        except ReadError as e:
            if e.kind != "damaged":
                raise
            c[part] = {}
            if ctx is not None:
                ctx.skipped["rels"] += 1
    return c[part]


def load(pkg, part):
    """(root element, {prefix: uri}) of an XML part, cached. Raises ReadError when missing or damaged."""
    c = _cache(pkg)["xml"]
    if part not in c:
        if not pkg.has(part):
            raise ReadError("damaged", f"missing part {part[:80]}")
        data = pkg.read(part)
        nsmap = {m.group(1).decode(): (m.group(2) or m.group(3) or b"").decode("utf-8", "replace")
                 for m in _XMLNS.finditer(data[:_CFB_SCAN])}
        c[part] = (parse_xml(data), nsmap)
    return c[part]


def _target(pkg, part, rid, suffix):
    """Member name an internal relationship of `part` points at (by type suffix), or None."""
    rel = rels(pkg, part).get(rid or "")
    if rel is None or rel.external or not rel.type.endswith(suffix) or not pkg.has(rel.target):
        return None
    return rel.target


def _member(pkg, name):
    c = _cache(pkg)["bytes"]
    if name not in c:
        try:
            c[name] = pkg.read(name)
        except ReadError as e:
            if e.kind != "damaged":
                raise
            c[name] = None
    return c[name]


def image(pkg, part, rid):
    """Image for relationship `rid` of `part`; None when missing, external, outside the package or damaged."""
    name = _target(pkg, part, rid, "/image")
    data = _member(pkg, name) if name else None
    return Image(data) if data else None


def chart(pkg, part, rid):
    """Table of the chart cache behind relationship `rid`; None when missing or unreadable. One object per part."""
    name = _target(pkg, part, rid, "/chart")
    if name is None:
        return None
    c = _cache(pkg)["charts"]
    if name not in c:
        data = _member(pkg, name)
        tbl = None
        if data:
            try:
                tbl = chart_table(parse_xml(data))
            except ReadError as e:
                if e.kind != "damaged":
                    raise
            except Exception:
                tbl = None
        c[name] = tbl
    return c[name]


def alternate(el, nsmap=None):
    """The mc:Choice whose every Requires prefix is understood (and declared, when nsmap is given), else mc:Fallback.

    None when neither exists.
    """
    fallback = None
    for c in el:
        n = local(c.tag)
        if n == "Choice":
            req = (attr(c, "Requires") or "").split()
            if all(p in UNDERSTOOD and (nsmap is None or p in nsmap) for p in req):
                return c
        elif n == "Fallback" and fallback is None:
            fallback = c
    return fallback


# ---- entry
def _check_cfb(path):
    """An OLE file is an encrypted package or not an OOXML package; anything else falls through."""
    with open(path, "rb") as f:
        head = f.read(8)
        if head[:4] != _CFB_MAGIC:
            return
        head += f.read(_CFB_SCAN - 8)
    if _ENCRYPTED in head:
        raise ReadError("encrypted")
    raise ReadError("damaged", "not an OOXML package (OLE file)")


def _main_part(pkg) -> str:
    for rel in rels(pkg, "").values():
        if rel.type.endswith("/officeDocument") and not rel.external:
            if pkg.has(rel.target):
                return rel.target
            break
    raise ReadError("damaged", "not an OOXML package")


def parse(path, ctx):
    _check_cfb(Path(path))
    with Package(path) as pkg:
        part = _main_part(pkg)
        root, _ = load(pkg, part)
        kind = local(root.tag)
        if kind == "document":
            from . import ooxml_word
            return ooxml_word.parse_word(pkg, part, ctx)
        if kind == "presentation":
            # Task 12: from . import ooxml_slides; return ooxml_slides.parse_slides(pkg, part, ctx)
            raise ReadError("unsupported", "pptm/ppsx arrive in Task 12")
        raise ReadError("damaged", "not a Word or PowerPoint package")
