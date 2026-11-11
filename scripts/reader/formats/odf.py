"""OpenDocument entry point: package checks, kind dispatch and the odt path. Shared walking lives in odf_base."""
from ..containers.zipx import Package, kids, local
from ..limits import ReadError
from ..model import Doc
from .odf_base import Body, Styles

_MIME = "application/vnd.oasis.opendocument."
_BODY_KINDS = {"text": "text", "spreadsheet": "spreadsheet", "presentation": "presentation"}


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
    for c in office:
        with ctx.guard("block"):
            body.doc.blocks.extend(body.block(c, 0))
    if styles_root is not None:
        with ctx.guard("header"):
            body.master(styles_root)
    return body.doc


def parse(path, ctx) -> Doc:
    with Package(path) as pkg:
        kind, content, styles_root = _open(pkg, ctx)
        if kind == "text":
            return _text(pkg, ctx, content, styles_root)
        raise ReadError("unsupported", "ods/odp arrive in Task 10")
