"""Apple iWork containers (Pages, Numbers, Keynote) to the document model.

Ported from docling (MIT) `docling/backend/iwork_backend.py` (`_open_container`, `_is_nested_index`,
`_readable_members`, the nested `Index.zip` read), `docling/backend/iwork/iwa.py` (`is_encrypted`) and
`docling/backend/iwork/legacy.py` (the '09 gzip ceiling). The checks run in order:
1. a `.iwpv2` or `.iwph` member at any depth is `encrypted` (an encrypted .iwa may start with 0x00 by chance);
2. iWork '09: a root `index.xml`, `index.xml.gz`, `index.apxl` or `index.apxl.gz`, stored or deflated (any other
   zip method is Pages' password marker, `encrypted`), `.gz` inflated within `IWORK_XML` and flate's ratio rule,
   parsed to at most `IWORK_NODES` elements (past either: too_large), goes to `iwork_xml`;
3. IWA: root `Index/*.iwa`, or an `Index.zip` at the root or one folder down, opened in memory; an `Index.zip`
   inside that one counts `document` and is not opened. The app comes from the extension (`iwork_iwa.walk`);
4. nothing readable, or a body with no block: `preview.jpg`, else `QuickLook/Thumbnail.jpg`, is the only block,
   counted `document` unless the lost part was counted already (a member, an Index.zip inside the index, a walk or
   a body). With neither, `damaged`. An IWA walk that found its root, lost nothing and read no block is a blank
   document: its empty Doc is the answer, without the preview.
macOS bundle folders are not read: the gate takes files only.
"""
import posixpath
from pathlib import Path

from .. import limits
from ..containers import flate
from ..containers.zipx import Package, parse_xml_ns
from ..limits import ReadError
from ..model import Doc, Image
from . import iwork_iwa

IWORK_XML = 100 * 2**20  # bytes of an inflated '09 index (docling's ceiling)
# elements of an '09 index (`zipx.parse_xml_ns`); past it the index is too_large. The gunzip also keeps flate's ratio
# rule (at most ZIP_RATIO x the .gz size, ZIP_RATIO_FLOOR at least: a 100 KB .gz stops near 20 MB), and a deflated
# index.xml zipx's, but even 20 MB of `<a/>` builds 5 million elements. Measured 2026-11-16 on the development machine
# (`test_reader_iwork.py measure nodes`, under tracemalloc): 2,000,000 bare elements (7.6 MiB) parse in 2.18 s and
# hold 154 MiB; a real index of 100 MiB at 52 bytes or more an element fits.
IWORK_NODES = 2_000_000
APPS = {".pages": "pages", ".numbers": "numbers", ".key": "key"}
LOCKS = (".iwpv2", ".iwph")
XML_INDEXES = ("index.xml", "index.xml.gz", "index.apxl", "index.apxl.gz")
NESTED = "index.zip"
THUMBNAIL = "QuickLook/Thumbnail.jpg"


def _open(src) -> Package:
    """`Package(src)`, with what zipfile raises past BadZipFile for a broken directory (an unknown zip version is a
    NotImplementedError, a bad offset a ValueError) turned into ReadError("damaged")."""
    try:
        return Package(src)
    except (ValueError, NotImplementedError, EOFError) as e:
        raise ReadError("damaged", f"not a readable zip: {type(e).__name__}")


def _locked(names) -> bool:
    return any(n.lower().endswith(LOCKS) for n in names)


def _members(pkg, names, ctx):
    """(name, bytes) of each member; one zip cannot read (damaged) counts `document` and is left out."""
    for name in names:
        try:
            data = iwork_iwa.read_member(pkg, name)
        except ReadError as e:
            if e.kind != "damaged":
                raise
            ctx.skipped["document"] += 1
            continue
        yield name, data


def _xml(pkg, name: str, app: str, ctx) -> Doc | None:
    method = pkg.method(name)
    if method not in (0, 8):
        raise ReadError("encrypted", f"{name}: zip method {method}")
    gz = name.lower().endswith(".gz")
    if not gz and pkg.size(name) > IWORK_XML:
        raise ReadError("too_large", f"{limits.BIG_PART}: {name}")
    data = iwork_iwa.read_member(pkg, name)
    if gz:
        try:
            data = flate.gunzip(data, flate.Budget(), IWORK_XML)
        except ValueError as e:  # flate.Truncated is one
            raise ReadError("damaged", f"{name}: {e}")
    root, _ = parse_xml_ns(data, IWORK_NODES)
    del data
    from . import iwork_xml  # the '09 reader; imported here so the IWA path never needs it
    return iwork_xml.parse(root, app, pkg, ctx)


def _nested(pkg) -> str | None:
    """The `Index.zip` at the root or one folder down (docling's rule), first by name."""
    found = sorted(n for n in pkg.names if posixpath.basename(n).lower() == NESTED and n.count("/") <= 1)
    return found[0] if found else None


def _iwa(pkg, names: list, nested: str | None, prefix: str, app: str, ctx) -> tuple[Doc | None, bool]:
    """(the walked document or None, whether a part was lost and already counted `document`: a member that could not
    be read or unpacked, an Index.zip inside the index, or a walk or body that failed)."""
    budget = flate.Budget(iwork_iwa.IWA_BYTES)  # one budget for every member's Snappy output
    before = ctx.skipped["document"]
    if names:
        store = iwork_iwa.load(_members(pkg, names, ctx), ctx, budget, pkg)
        doc = iwork_iwa.walk(store, app, ctx)
        return doc, store.failed > 0 or ctx.skipped["document"] > before
    if nested is None:
        return None, False
    with _open(iwork_iwa.read_member(pkg, nested)) as inner:
        if _locked(inner.names):
            raise ReadError("encrypted", "iWork password")
        deeper = sum(1 for n in inner.names if posixpath.basename(n).lower() == NESTED)
        if deeper:
            ctx.skipped["document"] += deeper  # an Index.zip inside the index is never opened
        names = [n for n in inner.names if n.lower().endswith(".iwa")]
        store = iwork_iwa.load(_members(inner, names, ctx), ctx, budget, pkg, prefix=prefix)
    doc = iwork_iwa.walk(store, app, ctx)
    return doc, store.failed > 0 or ctx.skipped["document"] > before


def _preview(pkg, prefix: str, doc: Doc | None, failure: ReadError | None, counted: bool, ctx) -> Doc:
    """The preview picture as the only block, counted `document` unless a failed walk already counted the body;
    `damaged` when there is none."""
    names = ["preview.jpg"] + ([prefix + "preview.jpg"] if prefix else []) + [THUMBNAIL]
    for name in names:
        if not pkg.has(name) or pkg.size(name) > limits.IMAGE_BYTES:
            continue
        try:
            data = iwork_iwa.read_member(pkg, name)
        except ReadError as e:
            if e.kind != "damaged":
                raise
            continue
        if not data:
            continue
        doc = doc if doc is not None else Doc()
        doc.blocks = [Image(data)]
        if not counted:
            ctx.skipped["document"] += 1
        return doc
    raise failure or ReadError("damaged", "no readable iWork part and no preview")


def _read(pkg, ctx, app: str) -> Doc:
    if _locked(pkg.names):
        raise ReadError("encrypted", "iWork password")
    index = next((n for n in XML_INDEXES if pkg.has(n)), None)
    iwas = [] if index else [n for n in pkg.names if n.lower().startswith("index/") and n.lower().endswith(".iwa")]
    nested = None if index or iwas else _nested(pkg)
    prefix = nested[:-len(NESTED)] if nested else ""
    doc, failure, counted = None, None, False
    lost = sum(ctx.skipped.values())
    try:
        if index:
            doc = _xml(pkg, index, app, ctx)
        else:
            doc, counted = _iwa(pkg, iwas, nested, prefix, app, ctx)
    except ReadError as e:
        if e.kind != "damaged":
            raise
        failure = e
    if doc is not None and doc.blocks:
        return doc
    if doc is not None and not index and sum(ctx.skipped.values()) == lost:
        return doc  # a blank IWA document: its walk read everything and lost nothing, so no preview stands in
    return _preview(pkg, prefix, doc, failure, counted, ctx)


def parse_bytes(data: bytes, ctx, app: str) -> Doc:
    if app not in iwork_iwa.APPS:
        raise ReadError("unsupported", f"not an iWork app: {app}")
    with _open(data) as pkg:
        return _read(pkg, ctx, app)


def parse(path, ctx) -> Doc:
    app = APPS.get(Path(path).suffix.lower())
    if app is None:
        raise ReadError("unsupported", f"not an iWork extension: {Path(path).suffix}")
    with _open(Path(path)) as pkg:
        return _read(pkg, ctx, app)
