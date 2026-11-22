"""pdf document front end: one `PageData` per page, built from the pdfnative modules.

The part walk of a page's /Contents follows dochan 1.21.0 pdf/reader.py `_page_content_parts`
(lines 768-793, MIT) and the image pass its `_page_images` (1184-1220); the page loop itself is
`formats/pdf.py`'s. `NativeDoc(data)` opens the file (the structure and crypto `ReadError`s
reach the caller; a decrypt budget spent while opening is `damaged: decrypt budget`) and
`.page(i)` interprets page `i`.

Counts. `.cut` is the document total of what a cap dropped: `page_part`, `image`, `annotation`,
`field`, the content module's own counts and `glyphs`. Each `PageData.cut` holds the part of
that total that page produced, so a caller takes the total from `NativeDoc.cut` alone. `glyphs`
counts an unmapped font once per document: a font is an unmapped entry of the shared font
cache (keyed by its object reference), counted the first time a page whose text operators
met an unmapped font finds it there.
"""
import math
from collections import Counter
from typing import Dict, List, Tuple

from ..formats import pdf as caps
from ..formats.pdf_layout import PageData, PageImage, text_marks, text_under
from ..limits import ReadError
from . import annotations, images
from .content import interpret
from .fonts import font_infos
from .objects import PDFStream
from .ocg import OCState
from .structure import PDFFile

MAX_CONTENT_PARTS = 256
MAX_PAGE_CONTENT_BYTES = 64 * 2**20

_LETTER = (0.0, 0.0, 612.0, 792.0)
_COORD = 1e9


def _budget(error: ReadError) -> bool:
    return error.kind == "too_large" and "decrypt budget" in error.detail


def _part_grew(before: Counter, now: Counter) -> bool:
    """Did a structure cap fire between the two counts, other than by refusing an image
    stream? A refused image (`decrypt_image`, the image part of `decrypt_budget`) comes out
    without bytes and is counted `image` by the reader, so it is not `page_part` as well."""
    for key in now:
        if key == "decrypt_image":
            continue
        grown = now[key] - before[key]
        if key == "decrypt_budget":
            grown -= now["decrypt_image"] - before["decrypt_image"]
        if grown > 0:
            return True
    return False


def _box(value):
    """(x0, y0, x1, y1) of a rectangle with finite coordinates and an area, or None."""
    if not isinstance(value, list) or len(value) != 4:
        return None
    nums = []
    for v in value:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        try:
            v = float(v)
        except OverflowError:
            return None
        if not math.isfinite(v) or abs(v) > _COORD:
            return None
        nums.append(v)
    x0, x1 = sorted((nums[0], nums[2]))
    y0, y1 = sorted((nums[1], nums[3]))
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


class NativeDoc:
    """An open pdf read by pdfnative."""

    def __init__(self, data: bytes, password: str = ""):
        self.cut: Counter = Counter()
        try:
            self._pdf = PDFFile(data, password)
            self.page_count = len(self._pdf.pages())
            self.oc = OCState.from_catalog(self._pdf)
            payload = annotations.encrypted_payload(self._pdf)
            if payload is not None:
                raise ReadError("drm", f"encrypted payload {payload}")
            self.fields: Dict[int, list] = annotations.fields(self._pdf, self.cut)
            self.xfa = annotations.has_xfa(self._pdf)
            self.attachments = annotations.attachments(self._pdf)
            self.labels = annotations.page_labels(self._pdf, self.page_count)
            if self._pdf.cuts["decrypt_budget"]:
                # structure.py swallows a refused piece and counts it; spent while opening
                # (an object stream or a string of the page tree, catalog or fields), the file
                # opens as a damaged one, not as one with missing pages
                raise ReadError("too_large", "part: decrypt budget")
        except ReadError as e:
            if _budget(e):
                raise ReadError("damaged", "decrypt budget") from e
            raise
        self._fonts: dict = {}
        self._unmapped_seen: set = set()
        self._direct_fonts: list = []  # keeps direct font dicts alive, so their ids stay theirs
        self._read: set = set()

    @property
    def pdf(self) -> PDFFile:
        return self._pdf

    def resources(self, index: int) -> dict:
        """The page's /Resources, inherited from the page tree."""
        found = self._pdf.pages()[index][1].get("Resources")
        return found if isinstance(found, dict) else {}

    def outline(self) -> List[tuple]:
        return annotations.outline(self._pdf, annotations.page_index(self._pdf))

    # -- pages --

    def _content(self, page: dict, cut: Counter) -> bytes:
        pdf = self._pdf
        contents = pdf.resolve(page.get("Contents"))
        streams = contents if isinstance(contents, list) else [contents]
        if len(streams) > MAX_CONTENT_PARTS:
            cut["page_part"] += 1
            streams = streams[:MAX_CONTENT_PARTS]
        parts: List[bytes] = []
        total = 0
        for item in streams:
            stream = pdf.resolve(item)
            if not isinstance(stream, PDFStream):
                continue
            decoded = pdf.decode_stream_bytes(stream)
            if not decoded:
                continue
            if total + len(decoded) > MAX_PAGE_CONTENT_BYTES:
                cut["page_part"] += 1
                break
            total += len(decoded)
            parts.append(decoded)
        return b"\n".join(parts)

    def _count_glyphs(self, cut: Counter, resources: dict) -> None:
        counted = 0
        for key, info in list(self._fonts.items()):
            if getattr(info, "unmapped", False) and key not in self._unmapped_seen:
                self._unmapped_seen.add(key)
                counted += 1
        # fonts given as direct dicts are not cached by reference: one per dict identity
        table = self._pdf.resolve(resources.get("Font"))
        for name, font in (table.items() if isinstance(table, dict) else ()):
            if isinstance(font, dict) and id(font) not in self._unmapped_seen:
                info = font_infos(self._pdf, {"Font": {name: font}}, {}).get(str(name))
                if info is not None and info.unmapped:
                    self._unmapped_seen.add(id(font))
                    self._direct_fonts.append(font)
                    counted += 1
        if not counted and not self._unmapped_seen:
            # the text came from a font no cache entry or page-level dict accounts for
            self._unmapped_seen.add("unknown")
            counted = 1
        cut["glyphs"] += counted

    def page(self, index: int) -> PageData:
        pdf = self._pdf
        page, inherited = pdf.pages()[index]
        cut: Counter = Counter()
        before = Counter(pdf.cuts)
        box = (_box(pdf.resolve(inherited.get("CropBox")))
               or _box(pdf.resolve(inherited.get("MediaBox"))) or _LETTER)
        rotate = pdf.resolve(inherited.get("Rotate"))
        turn = 0
        if isinstance(rotate, (int, float)) and not isinstance(rotate, bool) and math.isfinite(rotate):
            turn = int(round(rotate)) % 360
        resources = inherited.get("Resources")
        resources = resources if isinstance(resources, dict) else {}
        content = interpret(pdf, self._content(page, cut), resources, self._fonts, self.oc, box=box)
        cut.update(content.cut)
        if content.unmapped:
            self._count_glyphs(cut, resources)

        drawn = []
        marks = text_marks(content.fragments) if content.image_draws else []
        for _name, draw_box, stream, last in content.image_draws:
            if len(drawn) >= caps.PDF_PAGE_IMAGES:
                cut["image"] += 1
                break
            if isinstance(stream, PDFStream):
                drawn.append((draw_box, stream, text_under(draw_box, marks, lambda f: f.obj <= last,
                                                           caps.TEXT_UNDER)))
        under = {id(stream) for _box, stream, at in drawn if at}
        shown: List[PageImage] = []
        for draw_box, stream, _at in drawn:
            got = images.image(pdf, stream)
            if id(stream) in under:
                shown.append(PageImage(draw_box, None, got.px, got.kind, True))
            else:
                shown.append(PageImage(draw_box, got.data, got.px, got.kind, load=got.load))

        if _part_grew(before, pdf.cuts):
            cut["page_part"] += 1  # a structure cap fired while this page was read: read in part
        links, comments, annot_cut = annotations.page_annots(pdf, page)
        cut.update(annot_cut)
        chars = sum(1 for f in content.fragments for ch in f.text if not ch.isspace())
        if index not in self._read:
            self._read.add(index)
            self.cut.update(cut)
        return PageData(number=index + 1, box=box, rotate=turn, fragments=content.fragments,
                        segments=content.segments, short=content.short, images=shown,
                        marks=content.marks, links=links, fields=self.fields.get(index, []),
                        comments=comments, chars=chars, cut=cut, render=None,
                        unmapped=content.unmapped_glyphs)
