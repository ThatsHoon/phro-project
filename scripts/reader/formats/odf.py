"""OpenDocument entry point: package checks, kind dispatch and the odt path. Shared walking lives in odf_base.

An odt body that holds a connector is read as one diagram scope (`Body.scope`, `Body.place`); one without reads as
before. An odt's meta.xml gives its title (Doc.title) and its properties line (`_meta`). Every kind's macros, its
Basic libraries and scripts, go to the document's scripts (`macros`)."""
import posixpath
import re

from .. import diagram
from ..containers.zipx import Package, attr, first, kids, local, parse_xml, walk
from ..limits import ReadError
from ..model import Doc, Para
from ..text import source
from . import odf_sheets, odf_slides
from .odf_base import Body, Styles
from .word_fields import properties, when

_MIME = "application/vnd.oasis.opendocument."
_BODY_KINDS = {"text": "text", "spreadsheet": "spreadsheet", "presentation": "presentation"}
_META = {"subject": "subject", "initial-creator": "author", "creator": "last author", "creation-date": "created",
         "date": "modified"}
MAX_TITLE = 200
# what LibreOffice writes into a new Basic module, whitespace runs as one space: a module that is only this was not
# written
_BASIC_NEW = "REM ***** BASIC ***** Sub Main End Sub"
_LANGUAGES = {"python": ("Python", ".py"), "javascript": ("JavaScript", ".js"), "beanshell": ("BeanShell", ".bsh")}
# a DOCTYPE naming only an external DTD, as LibreOffice opens its Basic files with: the parser refuses any DOCTYPE
_EXTERNAL_DTD = re.compile(rb'\A(\s*<\?xml[^>]*\?>)?\s*<!DOCTYPE\s+[\w:.-]+\s+PUBLIC\s+"[^"<>\[]*"\s+"[^"<>\[]*"\s*>')


# ---- package
def locked(pkg, ctx) -> frozenset:
    """The members META-INF/manifest.xml gives encryption data. A password-protected Basic library's modules are
    encrypted with its password, the rest of the document not; any other member makes the document encrypted."""
    if not pkg.has("META-INF/manifest.xml"):
        return frozenset()
    try:
        man = pkg.read("META-INF/manifest.xml")
    except ReadError as e:
        if e.kind != "damaged":
            raise
        ctx.skipped["manifest"] += 1
        return frozenset()
    if b"encryption-data" not in man.lower():
        return frozenset()
    try:
        root = parse_xml(man)
    except ReadError as e:
        if e.kind != "damaged":
            raise
        raise ReadError("encrypted")
    paths = {attr(f, "full-path") or "" for f in walk(root, "file-entry") if first(f, "encryption-data") is not None}
    if not paths or any(posixpath.normpath(p) != p or not p.startswith("Basic/") for p in paths):
        raise ReadError("encrypted")
    return frozenset(paths)


def _open(pkg, ctx):
    """(kind, content root, styles root) after the encryption and package checks."""
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


def _xml(pkg, name):
    """The root of member `name`, a LibreOffice Basic file whose DOCTYPE names only its DTD; KeyError when absent."""
    if not pkg.has(name):
        raise KeyError(name)
    data = pkg.read(name)
    if m := _EXTERNAL_DTD.match(data):
        data = (m.group(1) or b"") + data[m.end():]
    return parse_xml(data)


def _module(pkg, ctx, path, lib, name):
    """The Basic module at `path` as a scripts entry, None when it is blank or what a new module holds; counted under
    script when it is missing, broken or past the document's room for scripts."""
    try:
        if not ctx.script_room(pkg.size(path)):
            ctx.skipped["script"] += 1
            return None
        root = _xml(pkg, path)
    except KeyError:
        ctx.skipped["script"] += 1
        return None
    except ReadError as e:
        if e.kind not in ("damaged", "too_large"):
            raise
        ctx.skipped["script"] += 1
        return None
    text = source("".join(root.itertext()))
    if not text or " ".join(text.split()) == _BASIC_NEW:
        return None
    kind = attr(root, "moduleType") or "normal"
    return name, f"Basic {'' if kind == 'normal' else kind + ' '}module, library {lib}", text, "bas"


def _library(pkg, ctx, lib, closed):
    """The modules Basic/<lib>/script-lb.xml lists, read in its order; a password-protected library's, or a module
    the manifest encrypts (in `closed`), named under script_locked."""
    try:
        root = _xml(pkg, f"Basic/{lib}/script-lb.xml")
    except KeyError:
        ctx.skipped["script"] += 1
        return
    except ReadError as e:
        if e.kind not in ("damaged", "too_large"):
            raise
        ctx.skipped["script"] += 1
        return
    protected = attr(root, "passwordprotected") == "true"
    for el in kids(root, "element"):
        if ctx.late():
            return
        name = attr(el, "name") or ""
        path = f"Basic/{lib}/{name}.xml"
        if protected or path in closed:
            ctx.skipped["script_locked"] = 1
        elif got := _module(pkg, ctx, path, lib, name):
            ctx.scripts.append(got)


def _script(pkg, ctx, name):
    """A file under Scripts/ as a scripts entry: Scripts/<language>/<folders>/<file> of a language LibreOffice runs
    from a document, its parcel descriptor left out; anything else, or a file past the room for scripts or broken,
    counted under script."""
    lang, _, rest = name[len("Scripts/"):].partition("/")
    folder, file = posixpath.split(rest)
    if not file or file == "parcel-descriptor.xml":
        return None
    stem, ext = posixpath.splitext(file)
    if lang not in _LANGUAGES or ext.lower() != _LANGUAGES[lang][1]:
        ctx.skipped["script"] += 1
        return None
    try:
        if not ctx.script_room(pkg.size(name)):
            ctx.skipped["script"] += 1
            return None
        data = pkg.read(name)
    except ReadError as e:
        if e.kind not in ("damaged", "too_large"):
            raise
        ctx.skipped["script"] += 1
        return None
    text = source(data.decode("utf-8", "replace"))
    if not text:
        return None
    return stem, f"{_LANGUAGES[lang][0]} script" + (f", in {folder}" if folder else ""), text, ext[1:].lower()


def macros(pkg, ctx, closed):
    """The package's macros into ctx.scripts (LibreOffice's XML Basic and script provider layouts): each library
    Basic/script-lc.xml lists, its modules as Basic/<library>/script-lb.xml lists them (`_library`), then each source
    under Scripts/ (`_script`). A library linked from another file is named under linked_basic, the dialogs under
    Dialogs/ are counted under dialog, and Basic files without a readable index under script. `closed` is what
    `locked` gave."""
    try:
        index = _xml(pkg, "Basic/script-lc.xml")
    except KeyError:
        index = None
        if any(n.startswith("Basic/") and not n.endswith("/") for n in pkg.names):
            ctx.skipped["script"] += 1
    except ReadError as e:
        if e.kind not in ("damaged", "too_large"):
            raise
        index = None
        ctx.skipped["script"] += 1
    for lib in kids(index, "library") if index is not None else ():
        if attr(lib, "link") == "true":
            ctx.skipped["linked_basic"] = 1
        else:
            _library(pkg, ctx, attr(lib, "name") or "", closed)
    for name in pkg.names:
        if ctx.late():
            return
        if name.startswith("Scripts/"):
            if got := _script(pkg, ctx, name):
                ctx.scripts.append(got)
        elif (name.startswith("Dialogs/") and name.endswith(".xml")
              and posixpath.basename(name) not in ("dialog-lc.xml", "dialog-lb.xml")):
            ctx.skipped["dialog"] += 1


def read_package(pkg, ctx) -> Doc:
    """The Doc of the open package `pkg`: its text, spreadsheet or presentation, and its macros."""
    closed = locked(pkg, ctx)
    kind, content, styles_root = _open(pkg, ctx)
    if kind == "text":
        doc = _text(pkg, ctx, content, styles_root)
    elif kind == "spreadsheet":
        doc = odf_sheets.sheets(pkg, ctx, content, styles_root)
    else:
        doc = odf_slides.slides(pkg, ctx, content, styles_root)
    with ctx.guard("script"):
        macros(pkg, ctx, closed)
    return doc


def parse(path, ctx) -> Doc:
    with Package(path) as pkg:
        return read_package(pkg, ctx)
