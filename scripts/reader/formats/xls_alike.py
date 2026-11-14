"""Files named .xls that are not BIFF workbooks: HTML tables, delimited text and zips.

`kind` sniffs the first 64 KiB; a zip it names goes to the xlsx, xlsb or OpenDocument reader (`xls.parse`). A UTF-16 head (a BOM, or every other byte NUL at its start) is decoded as UTF-16
before the tests, and the HTML and text readers decode the file with that same codec. HTML goes through `html.tree`
and `html.blocks` into one sheet section; delimited text (tab before comma, by the first line) goes through `csv` and
a `sheet_grid.Grid`, bounded by the document's `Budget` like any sheet. A head that decodes cleanly as text and holds a
line break is text too, read as one column when its first line has no delimiter. Nothing is ported.
"""
import codecs
import csv
import io
import re

from .. import html as xhtml
from .. import limits
from ..containers.cfb import is_cfb
from ..limits import ReadError
from ..model import Doc, Section
from . import sheet_grid

HEAD = 64 * 1024
_HTML_MARKS = (b"<html", b"<table", b"<!doctype html")
_CONTROLS = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")  # characters plain text does not hold
_LINE_END = re.compile(rb"\r\n|\r|\n")  # an old Mac file ends its lines with CR alone


def utf16(head: bytes) -> str | None:
    """"utf-16-le" or "utf-16-be" when the bytes are UTF-16 (a BOM, or every other byte of the first 32 NUL)."""
    if head[:4] == b"\xff\xfe\0\0":  # a UTF-32LE BOM
        return None
    if head[:2] == b"\xff\xfe":
        return "utf-16-le"
    if head[:2] == b"\xfe\xff":
        return "utf-16-be"
    w = head[:32]
    w = w[:len(w) & ~1]
    if len(w) >= 4:
        if not any(w[1::2]) and all(w[0::2]):
            return "utf-16-le"
        if not any(w[0::2]) and all(w[1::2]):
            return "utf-16-be"
    return None


def _decode16(data: bytes, enc: str, cut: bool) -> str:
    """UTF-16 `data` as text, its BOM dropped; with `cut`, an incomplete trailing unit or pair is dropped."""
    text = codecs.getincrementaldecoder(enc)(errors="replace").decode(data, final=not cut)
    return text[1:] if text.startswith("﻿") else text


def _plain(head: bytes) -> bool:
    """The head decodes as UTF-8 or cp949 (a cut trailing sequence allowed) and holds no control characters."""
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = codecs.getincrementaldecoder(enc)().decode(head, final=False)
        except UnicodeDecodeError:
            continue
        return not _CONTROLS.search(text)
    return False


def kind(head: bytes) -> str:
    """cfb, zip, rtf, html, text or other, from the first 64 KiB."""
    if is_cfb(head[:16]):
        return "cfb"
    if head[:2] == b"PK":
        return "zip"
    if head.removeprefix(codecs.BOM_UTF8).lstrip(b" \t\r\n\x0b\x0c\x00").startswith(b"{\\rtf"):  # as doc.py
        return "rtf"
    enc = utf16(head)
    if enc is not None:
        head = _decode16(head, enc, cut=True).encode("utf-8")
    body = head[3:] if head.startswith(codecs.BOM_UTF8) else head
    body = body.lstrip()
    if body[:1] == b"<" and any(m in head.lower() for m in _HTML_MARKS):
        return "html"
    if b"\0" not in head:
        first = _LINE_END.split(head, 1)[0]
        if b"\t" in first or b"," in first:
            return "text"
        if (b"\n" in head or b"\r" in head) and _plain(head):
            return "text"
    return "other"


def parse_html(path, ctx) -> Doc:
    with open(path, "rb") as f:
        data = f.read(xhtml.MAX_DOC)
    enc = utf16(data)
    if enc is not None:  # handed on as UTF-8 behind a BOM, so html.decode reads the codec kind() used
        data = codecs.BOM_UTF8 + _decode16(data, enc, len(data) >= xhtml.MAX_DOC).encode("utf-8")
    root = xhtml.tree(data)
    blocks = xhtml.blocks(root, css=xhtml.Css([]), load=lambda name: None, notes={}, ctx=ctx)
    title = xhtml.find(root, "title")
    name = " ".join(xhtml.text_of(title).split()) if title is not None else ""
    return Doc(blocks=[Section("sheet", name or "Sheet1", blocks=blocks)])


def _decode(data: bytes, cut: bool) -> str:
    """`cut`: the read stopped at its cap, so an incomplete trailing sequence is dropped, not taken as bad bytes."""
    enc = utf16(data)
    if enc is not None:
        return _decode16(data, enc, cut)
    for enc in ("utf-8-sig", "cp949"):
        try:
            return codecs.getincrementaldecoder(enc)().decode(data, final=not cut)
        except UnicodeDecodeError:
            pass
    return data.decode("cp1252", errors="replace")


def parse_text(path, ctx) -> Doc:
    budget = sheet_grid.Budget()
    cap = budget.cap_chars * 4 + 4  # a text longer than the character budget is cut by the grid anyway
    with open(path, "rb") as f:
        data = f.read(cap + 1)  # one byte more: a file of exactly `cap` bytes was not cut
    text = _decode(data[:cap], len(data) > cap)
    first = re.split(r"\r\n|\r|\n", text, maxsplit=1)[0]
    delim = chr(9) if chr(9) in first else ","
    grid = sheet_grid.Grid("Sheet1", budget, ctx)
    # A field may be as long as the whole character budget, so the csv module's process-wide field limit is raised
    # for the read and put back after. Nothing in the reader runs conversions in threads (the gate runs each in a
    # subprocess, and queue.py's server never calls the reader), so nothing else can see the raised limit.
    # cache.py's _lock (cache.py:105-106) does not cover this: it guards only the counter that names temp folders,
    # and cache.store calls convert outside it, so a caller that ran store in threads would need its own lock here.
    old = csv.field_size_limit(max(csv.field_size_limit(), budget.cap_chars + 1))
    try:
        for r, row in enumerate(csv.reader(io.StringIO(text, newline=""), delimiter=delim)):
            for c, cell in enumerate(row):
                if not grid.put(r, c, cell.strip()):
                    break
            if grid.cut:
                break
    except csv.Error as e:
        # the csv module raises a bare csv.Error for every fault and keeps no record of the field it was reading,
        # so its message text is the only signal that the field was past the limit
        if str(e).startswith("field larger than field limit"):  # a field past the whole budget: characters, as put()
            grid.stop("characters", budget.cap_chars)
        else:
            ctx.skipped["record"] += 1
    finally:
        csv.field_size_limit(old)
    return Doc(blocks=[Section("sheet", "Sheet1", blocks=grid.blocks())])
