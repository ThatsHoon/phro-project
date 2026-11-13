"""Files named .xls that are not BIFF workbooks: HTML tables, delimited text and xlsx zips.

`kind` sniffs the first 64 KiB. HTML goes through `html.tree` and `html.blocks` into one sheet section; delimited
text (tab before comma, by the first line) goes through `csv` and a `sheet_grid.Grid`, bounded by the document's
`Budget` like any sheet. Nothing is ported.
"""
import csv
import io

from .. import html as xhtml
from .. import limits
from ..containers.cfb import is_cfb
from ..limits import ReadError
from ..model import Doc, Section
from . import sheet_grid

HEAD = 64 * 1024
_HTML_MARKS = (b"<html", b"<table", b"<!doctype html")
_BOMS = (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")


def kind(head: bytes) -> str:
    """cfb, zip, html, text or other, from the first 64 KiB."""
    if is_cfb(head[:16]):
        return "cfb"
    if head[:2] == b"PK":
        return "zip"
    body = head
    for bom in _BOMS:
        if body.startswith(bom):
            body = body[len(bom):]
            break
    body = body.lstrip()
    if body[:1] == b"<" and any(m in head.lower() for m in _HTML_MARKS):
        return "html"
    if b"\0" not in head:
        first = head.split(b"\n", 1)[0]
        if b"\t" in first or b"," in first:
            return "text"
    return "other"


def parse_html(path, ctx) -> Doc:
    with open(path, "rb") as f:
        data = f.read(xhtml.MAX_DOC)
    root = xhtml.tree(data)
    blocks = xhtml.blocks(root, css=xhtml.Css([]), load=lambda name: None, notes={}, ctx=ctx)
    title = xhtml.find(root, "title")
    name = " ".join(xhtml.text_of(title).split()) if title is not None else ""
    return Doc(blocks=[Section("sheet", name or "Sheet1", blocks=blocks)])


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("cp1252", errors="replace")


def parse_text(path, ctx) -> Doc:
    budget = sheet_grid.Budget()
    with open(path, "rb") as f:
        data = f.read(budget.cap_chars * 4 + 4)  # a text longer than the character budget is cut by the grid anyway
    text = _decode(data)
    first = text.split("\n", 1)[0]
    delim = "\t" if "\t" in first else ","
    grid = sheet_grid.Grid("Sheet1", budget, ctx)
    try:
        for r, row in enumerate(csv.reader(io.StringIO(text, newline=""), delimiter=delim)):
            for c, cell in enumerate(row):
                if not grid.put(r, c, cell.strip()):
                    break
            if grid.cut:
                break
    except csv.Error:
        ctx.skipped["record"] += 1
    return Doc(blocks=[Section("sheet", "Sheet1", blocks=grid.blocks())])
