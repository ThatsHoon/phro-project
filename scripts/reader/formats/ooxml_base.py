"""Helpers shared by the OOXML body parsers (Word now, slides later): cached rels and parts, image, chart, alternate content."""
from ..charts import chart_table
from ..containers.zipx import attr, local, parse_xml, parse_xml_ns
from ..limits import ReadError
from ..model import Image

def int_of(s, default=0):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


def is_on(el) -> bool:
    """A toggle element (w:b, w:vanish ...): on unless its val says off."""
    return attr(el, "val") not in ("0", "false", "off")


def val(el):
    return attr(el, "val") if el is not None else None


UNDERSTOOD = frozenset({"wps", "wpg", "wpc", "wpi", "wp14", "w14", "w15", "w16se", "a14", "c14", "v", "o", "w10"})
_MS = "http://schemas.microsoft.com/office/"
_URIS = {  # the one namespace each understood prefix may stand for
    "wps": _MS + "word/2010/wordprocessingShape", "wpg": _MS + "word/2010/wordprocessingGroup",
    "wpc": _MS + "word/2010/wordprocessingCanvas", "wpi": _MS + "word/2010/wordprocessingInk",
    "wp14": _MS + "word/2010/wordprocessingDrawing", "w14": _MS + "word/2010/wordml",
    "w15": _MS + "word/2012/wordml", "w16se": _MS + "word/2015/wordml/symex",
    "a14": _MS + "drawing/2010/main", "c14": _MS + "drawing/2007/8/2/chart",
    "v": "urn:schemas-microsoft-com:vml", "o": "urn:schemas-microsoft-com:office:office",
    "w10": "urn:schemas-microsoft-com:office:word",
}


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
    """(root element, {prefix: {uri, ...}}) of an XML part, cached. Raises ReadError when missing or damaged."""
    c = _cache(pkg)["xml"]
    if part not in c:
        if not pkg.has(part):
            raise ReadError("damaged", f"missing part {part[:80]}")
        c[part] = parse_xml_ns(pkg.read(part))
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


def _understood(prefix, nsmap) -> bool:
    if prefix not in UNDERSTOOD:
        return False
    if nsmap is None:
        return True
    return bool(nsmap.get(prefix)) and nsmap[prefix] <= {_URIS[prefix]}


def alternate(el, nsmap=None):
    """The mc:Choice whose every Requires prefix is understood, else mc:Fallback; None when neither exists.

    A prefix is understood when it is declared somewhere in the part, only ever for the namespace we know
    under that name (nsmap from `load`; None skips the declaration check).
    """
    fallback = None
    for c in el:
        n = local(c.tag)
        if n == "Choice":
            if all(_understood(p, nsmap) for p in (attr(c, "Requires") or "").split()):
                return c
        elif n == "Fallback" and fallback is None:
            fallback = c
    return fallback
