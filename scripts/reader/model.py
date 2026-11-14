"""The document model every format parser emits and the Markdown renderer reads.

Parsers never write Markdown. Every block and inline accepts a keyword-only
`hidden`: empty, or the reason it is hidden ("hidden" or "deleted").
"""
import collections
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Union

from .limits import ReadError


def _hid():
    return field(default="", kw_only=True)


# inlines
@dataclass
class Text:
    text: str
    hidden: str = _hid()


@dataclass
class Bold:
    inlines: list
    hidden: str = _hid()


@dataclass
class Link:
    url: str
    inlines: list
    hidden: str = _hid()


@dataclass
class NoteRef:
    id: str
    hidden: str = _hid()


@dataclass
class Math:
    tex: str
    hidden: str = _hid()


@dataclass
class Break:
    hidden: str = _hid()


@dataclass
class Hidden:
    inlines: list
    kind: str = "hidden"
    hidden: str = _hid()


Inline = Union[Text, Bold, Link, NoteRef, Math, Break, Hidden]


# blocks
@dataclass
class Heading:
    level: int
    inlines: list
    hidden: str = _hid()


@dataclass
class Para:
    inlines: list
    hidden: str = _hid()


@dataclass
class ListItem:
    level: int
    marker: str
    inlines: list
    hidden: str = _hid()


@dataclass
class Cell:
    blocks: list
    rowspan: int = 1
    colspan: int = 1
    header: bool = False


@dataclass
class Table:
    rows: list  # list[list[Cell]], origin cells only
    caption: str = ""
    hidden_rows: frozenset = frozenset()
    hidden_cols: frozenset = frozenset()
    hidden: str = _hid()


@dataclass
class Image:
    data: bytes
    alt: str = ""
    hidden: str = _hid()


@dataclass
class Equation:
    tex: str
    display: bool = True
    hidden: str = _hid()


@dataclass
class Quote:
    blocks: list
    hidden: str = _hid()


@dataclass
class Section:
    kind: str  # slide | sheet | chapter | page
    title: str = ""
    number: int | None = None
    blocks: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    hidden: str = _hid()
    comments: list = field(default_factory=list)  # Para, one per comment, shown after the notes


Block = Union[Heading, Para, ListItem, Table, Image, Equation, Quote, Section]


@dataclass
class Doc:
    title: str = ""
    meta: dict = field(default_factory=dict)
    blocks: list = field(default_factory=list)
    notes: dict = field(default_factory=dict)
    headers: list = field(default_factory=list)
    footers: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


class Context:
    """Passed to every parse(): collects warnings and isolates per-part failures."""

    def __init__(self):
        self.warnings: list[str] = []
        self.skipped: collections.Counter = collections.Counter()

    @contextmanager
    def guard(self, kind: str):
        try:
            yield
        except ReadError:
            raise
        except Exception:
            self.skipped[kind] += 1
            if self.skipped[kind] == 1:
                self.warnings.append(f"{kind}: skipped damaged part")
