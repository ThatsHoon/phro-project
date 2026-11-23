"""OpenDocument entry point: package checks, kind dispatch and the odt path. Shared walking lives in odf_base.

An odt body that holds a connector is read as one diagram scope (`Body.scope`, `Body.place`); one without reads as
before. An odt's meta.xml gives its title (Doc.title) and its properties line (`_meta`)."""
from .. import diagram
from ..containers.zipx import Package, attr, first, kids, local
from ..limits import ReadError
from ..model import Doc, Para
from . import odf_sheets, odf_slides
from .odf_base import Body, Styles
from .word_fields import properties, when

_MIME = "application/vnd.oasis.opendocument."
_BODY_KINDS = {"text": "text", "spreadsheet": "spreadsheet", "presentation": "presentation"}
_META = {"subject": "subject", "initial-creator": "author", "creator": "last author", "creation-date": "created",
         "date": "modified"}
MAX_TITLE = 200


# ---- package
def _open(pkg, ctx):
    """(kind, content root, styles root) after the encryption and package checks."""
    if pkg.has("META-INF/manifest.xml"):
        try:
            man = pkg.read("META-INF/manifest.xml")
        except ReadError as e:
            if e.kind != "damaged":
                raise
            man = b""
            ctx.skipped["manifest"] += 1
        if b"encryption-data" in man.lower():
            raise ReadError("encrypted")
    if not pkg.has("content.xml"):
        raise ReadError("damaged", "not an OpenDocument package")
    try:
        content = pkg.xml("content.xml")
    except ReadError as e:
        if e.kind != "damaged":
            raise
        raise ReadError("damaged", "no readable content.xml")
    styles = None
    if pkg.has("styles.xml"):
        try:
            styles = pkg.xml("styles.xml")
        except ReadError as e:
            if e.kind != "damaged":
                raise
            ctx.skipped["styles"] += 1
    mime = ""
    if pkg.has("mimetype"):
        try:
            mime = pkg.read("mimetype").decode("utf-8", "replace").strip()
        except ReadError as e:
            if e.kind != "damaged":
                raise
    kind = _BODY_KINDS.get(mime[len(_MIME):].split("-")[0]) if mime.startswith(_MIME) else None
    if kind is None:
        kind = next((_BODY_KINDS[local(c.tag)] for b in kids(content, "body") for c in b
                     if local(c.tag) in _BODY_KINDS), None)
    if kind is None:
        raise ReadError("damaged", "not an OpenDocument package")
    return kind, content, styles


def _text(pkg, ctx, content, styles_root) -> Doc:
    body = Body(pkg, ctx, Styles(content, styles_root))
    office = next((c for b in kids(content, "body") for c in b if local(c.tag) == "text"), None)
    if office is None:
        raise ReadError("damaged", "no readable body")
    with ctx.guard("shape"):
        body.scope(office)
    for c in office:
        if ctx.late():
            break
        with ctx.guard("block"):
            body.doc.blocks.extend(body.block(c, 0))
    body.doc.blocks.extend(Para([got[1]]) for got in body.changes.values() if got[0] == "deletion")
    if body.glued is not None:
        with ctx.guard("shape"):
            body.place(diagram.ConnectionBudget(ctx))
        body.glued = None
    if styles_root is not None:
        with ctx.guard("header"):
            body.master(styles_root)
    with ctx.guard("properties"):
        _meta(pkg, ctx, body.doc)
    return body.doc


def _meta(pkg, ctx, doc):
    """meta.xml's dc:title as Doc.title; its subject, initial creator (the author), dc:creator (the last author),
    creation and save dates, keywords and user-defined fields as Doc.meta["properties"]. The generator, statistics and
    editing figures are left out. A meta.xml that cannot be read is counted under properties."""
    try:
        root = pkg.xml("meta.xml")
    except ReadError as e:
        if e.kind == "timeout":
            raise
        ctx.skipped["properties"] += 1
        return
    meta = first(root, "meta") if root is not None else None
    if meta is None:
        return
    pairs, words = [], []
    for el in meta:
        n, text = local(el.tag), "".join(el.itertext())
        if n == "title":
            doc.title = " ".join(text.split())[:MAX_TITLE]
        elif n in _META:
            pairs.append((_META[n], when(text) if n in ("creation-date", "date") else text))
        elif n == "keyword" and text.strip():
            words.append(text.strip())
        elif n == "user-defined":
            pairs.append((attr(el, "name"), text))
    if kept := properties(pairs + [("keywords", ", ".join(words))]):
        doc.meta["properties"] = kept


def read_package(pkg, ctx) -> Doc:
    """The Doc of the open package `pkg`: its text, spreadsheet or presentation."""
    kind, content, styles_root = _open(pkg, ctx)
    if kind == "text":
        return _text(pkg, ctx, content, styles_root)
    if kind == "spreadsheet":
        return odf_sheets.sheets(pkg, ctx, content, styles_root)
    return odf_slides.slides(pkg, ctx, content, styles_root)


def parse(path, ctx) -> Doc:
    with Package(path) as pkg:
        return read_package(pkg, ctx)
