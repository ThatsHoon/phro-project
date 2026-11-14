"""pdf page types: the text Fragment and the page data the pdf reader passes around.

Fragment follows dochan 1.21.0 pdf/content.py (MIT) without its bold, italic, note, comment,
marked-content and artifact fields, and adds the font name, the text object index and the
hidden kind. `writing_direction`, `along` and `across` are dochan's.
"""
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple


@dataclass
class Fragment:
    """The text of one Tj/TJ/'/\" operator and its start position."""
    x: float
    y: float
    width: float   # device width
    size: float    # effective font size
    text: str
    space_width: float  # device width of one space in this fragment's font
    order: int = 0
    dir_x: float = 1.0  # x part of the text x axis in device space; negative runs right to left
    dir_y: float = 0.0  # y part of the text x axis in device space
    up_x: float = 0.0   # the text y axis (the glyph's up) in device space
    up_y: float = 1.0
    # Where glyph k starts, along the text direction, from the fragment start; the last entry
    # is the end of the last glyph. A TJ number moves the glyphs after it, so it shows as a
    # larger step. Empty when positions were not tracked.
    char_offsets: tuple = ()
    link_spans: list = field(default_factory=list)
    link_geometry_reliable: bool = True
    # Drawn with render mode 3 or 7 (invisible text, an OCR layer).
    invisible: bool = False
    font: str = ""   # the BaseFont of the font, without its subset tag
    obj: int = 0     # index of its text object on the page
    hidden: str = ""  # "", "ocg", "invisible", "offcrop" or "tiny"


def writing_direction(frag: Fragment) -> str:
    """The text x axis in device space as one of four writing directions."""
    if abs(frag.dir_x) >= abs(frag.dir_y):
        return "ltr" if frag.dir_x >= 0 else "rtl"
    return "down" if frag.dir_y < 0 else "up"


def along(frag: Fragment) -> float:
    """The fragment start projected on the axis whose positive side is the reading direction."""
    direction = writing_direction(frag)
    if direction == "ltr":
        return frag.x
    if direction == "rtl":
        return -frag.x
    if direction == "down":
        return -frag.y
    return frag.y


def across(frag: Fragment) -> float:
    """The between-lines coordinate; it falls on the next line (text turned 180 degrees has
    the next line above)."""
    direction = writing_direction(frag)
    if direction == "ltr":
        return frag.y
    if direction == "rtl":
        return -frag.y
    return frag.x if direction == "down" else -frag.x


@dataclass
class PageImage:
    """One image on a page: its box, its viewable bytes (or None) and its pixel size.
    `kind` is "jpg", "png", "jpx", "ccitt", "jbig2" or "raw"."""
    box: Tuple[float, float, float, float]
    data: Optional[bytes]
    px: Tuple[int, int]
    kind: str


@dataclass
class Comment:
    author: str
    text: str
    hidden: bool


@dataclass
class PageData:
    """Everything the reader takes from one page.

    `box` is the crop box in page space; `short` the sub-1-unit rule pieces kept for the
    short-dash supplement; `figure` is (union box or None, non-rule path count); `links` is
    [(box, url)], `fields` [(label, value)]; `cut` counts what a cap dropped; `render` is
    (box, dpi) -> png bytes or None.
    """
    number: int
    box: Tuple[float, float, float, float]
    rotate: int
    fragments: List[Fragment] = field(default_factory=list)
    segments: list = field(default_factory=list)
    short: list = field(default_factory=list)
    images: List[PageImage] = field(default_factory=list)
    figure: tuple = (None, 0)
    links: list = field(default_factory=list)
    fields: list = field(default_factory=list)
    comments: List[Comment] = field(default_factory=list)
    chars: int = 0
    cut: Counter = field(default_factory=Counter)
    render: Optional[Callable] = None
