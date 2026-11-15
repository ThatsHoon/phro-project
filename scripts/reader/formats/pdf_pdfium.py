"""pdf pages read by pypdfium2: one `PageData` per page, the same the native front end gives, so
`formats/pdf.py`'s page loop serves both.

This is the one module of the reader that imports a package outside the standard library. It is
imported only in the pdf venv's interpreter, and `pdf.py` imports it inside a function.
pypdfium2's pdfium has no V8 or XFA, so no JavaScript runs.

What the spike learned is kept. `init_forms()` is called before any page is loaded, with the
"pypdfium2" logger raised to ERROR so its XFA install hint never reaches read.py's stderr. A page
stays open until the next page() (pdf.py may still call its render) and `close()` closes the
last page and the document; pdf.py calls it in a finally.
- Chars come from the `PdfTextPage`. Surrogate pairs are joined; pdfium's line-end hyphen (0x02,
  0xFFFE) becomes `-` for the hyphen rule; generated chars are dropped (the spaces come from the
  char boxes, through `char_gaps`) and a generated line break ends the run. Consecutive chars of
  one text object on one line make one `Fragment`. Its size is native's: the font size
  (`FPDFText_GetFontSize`, the Tf operand) times the length of the char matrix's second row,
  which Tz leaves alone; the loose box height stands in only when there is no usable size. Its
  `char_offsets` and `char_gaps` come from the loose boxes along the text direction. The loose
  box holds the glyph's ink too: where the ink reaches the box's far edge (an f or an italic
  letter overhanging its advance), or where the text runs at an angle (the box then stands
  upright around the turned glyph), a char's advance ends at the font's width for it
  (`FPDFFont_GetGlyphWidth` times the char matrix's x scale) when that width fits in the box.
  The chars of one ligature glyph ("fi" from one code) share its origin: they stay in the run,
  with no gap between them.
  The text object's render mode (3 or 7) sets `invisible`. A text object whose second char sits
  below its first along the glyph's up is a vertical font's column: it takes native's vertical
  axes (direction -up, up the char matrix's x axis). When native opened the file, such a page's
  fragments are native's instead (below).
  With native None, a text object with an /OC content mark is hidden "ocg" (the group's state is
  native's to know; without native the marked text is taken as off).
  A char pdfium has no Unicode for (`FPDFText_HasUnicodeMapError`), whose char pdfium gives as the
  raw code (a glyph id read as a code point, for a CID font), is dropped, never shown: when its
  font is one native reads by guess or not at all where no ToUnicode entry maps a code
  (`_guessed_cid_fonts`, by base font name), and with native None in any font, which then counts
  `glyphs` once per document, by base font name. Elsewhere (a simple font's code with no glyph
  name) it stays as pdfium gives it.
- Page objects are walked iteratively in content order, form XObjects included to FORM_DEPTH
  levels, each object's matrix composed with its forms' (pdfium gives a nested object's matrix
  in its form's space). Path points go through that matrix into `pdf_tables.PathCollector`, with
  its thin-fill and straight-curve rules. Images keep their box and pixel size.
- An image's bytes: DCT data as it is; over `images._MAX_PIXELS` pixels, a CROP_DPI render of
  its box; anything else a PNG of `FPDFImageObj_GetBitmap` through `images._png`.
- `render(box, dpi)` renders the page (box None) or a page-space box, long side capped at
  RENDER_LONG pixels.
- Decode budget: every image object's pixel size is summed first (no decode). A render decodes
  every image on the page, so a page whose images total more than `pdf.PDF_PAGE_DECODE_PIXELS`
  (or whose walk was cut short) has no render: `render` raises and pdf.py puts its scan or figure
  on `unrendered`. Images are decoded (a bitmap, or a render of an image's box) in order while
  the running sum stays within the budget; the rest get no data, and pdf.py counts each `image`.
- Links are `FPDFLink_*` URI actions of `annotations.SCHEMES`; comments come from the page's
  annotations, through `annotations.order_comments`. Fields are native's (`/AcroForm /Fields`, by
  page, a field with no widget on page 1); only when native could not open the file are they read
  from the page's widgets, through `annotations.field_entry`, a field then hidden when each of its
  widgets on the page is Hidden or NoView (as a comment with either flag is).
- On a page whose resources name an off optional-content group (decision 3), pdfium cannot tell
  which marked text the group covers: that page's fragments come from the native interpreter.
  So do they on a page with vertical text (a column pdfium saw, or a page font whose Encoding is a
  vertical CMap): native's vertical fragments are the reference. And on a page where pdfium
  dropped such a char: native reads its font by reversing its embedded cmap, or drops its text and
  counts `glyphs`.

Counts, as in `pdfnative.document`: `.cut` is the document total, each `PageData.cut` the part
its page added (a page is added once). The object walk past `pdf.PDF_PAGE_OBJECTS`, a form past
FORM_DEPTH, and a char scan stopped at CHAR_SCAN before the char cap, count `page_part` (the
char cap itself is pdf.py's `text`); images past `pdf.PDF_PAGE_IMAGES` count `image` once;
annotations past `annotations.PDF_ANNOTS` and a comment cut at ANNOT_CHARS count `annotation`;
native's field cuts, or a widget's value cut by `field_entry`, count `field`. Path points past
PATH_POINTS are ignored and not counted, like rules past `pdf_tables.PDF_SEGMENTS`.
"""
import ctypes
import logging
import math
from collections import Counter

import pypdfium2 as pdfium
import pypdfium2.raw as raw

from .. import images as reader_images
from .. import limits
from ..limits import ReadError
from ..pdfnative import annotations
from ..pdfnative.cmap import encoding_wmode
from ..pdfnative.objects import PDFStream
from . import pdf as caps
from .pdf_layout import LINE_TOLERANCE, Comment, Fragment, PageData, PageImage
from .pdf_tables import PDF_SEGMENTS, PathCollector

FORM_DEPTH = 16                  # form XObject levels walked (pypdfium2 get_objects max_depth)
PATH_POINTS = 8 * PDF_SEGMENTS   # path points read per page; the rest are ignored
CHAR_SCAN = 4                    # chars scanned per page: CHAR_SCAN x pdf.PDF_PAGE_CHARS
IMAGE_FILTERS = 8                # filters read per image
OPTION_LABELS = annotations.FIELD_KIDS  # list-box options read per field
OUTLINE_SCAN = 4 * annotations.OUTLINE_ITEMS  # bookmarks visited for the outline
PAGE_FONTS = 256                 # fonts of a page's /Resources looked at for a vertical CMap
_ADOBE_ORDERINGS = ("Japan1", "GB1", "CNS1", "Korea1", "KR")  # the CID tables fonts.cid_fallback_decoder has
OBJECT_MARKS = 16                # content marks read per text object (for /OC, native None)
# a font width under this share of the loose box is not taken for an upright glyph's advance: an
# overhang is a fraction of the glyph (the bench's f is 0.87 of its box, an italic h 0.95), and
# a width that small is more likely the wrong glyph's
ADVANCE_FLOOR = 0.5

_COORD = 1e9
_ROUNDING = 1e-3  # pdfium's float32 widths against its boxes; also the ink test's slack
_LETTER = (0.0, 0.0, 612.0, 792.0)
_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_HYPHENS = (0x02, 0xFFFE)
_UNSEEN = raw.FPDF_ANNOT_FLAG_HIDDEN | raw.FPDF_ANNOT_FLAG_NOVIEW  # annotations.UNSEEN
_DCT = ("DCTDecode", "DCT")
_SIMPLE = ("ASCIIHexDecode", "AHx", "ASCII85Decode", "A85", "RunLengthDecode", "RL",
           "FlateDecode", "Fl", "LZWDecode", "LZW")
_MARKUP = {getattr(raw, "FPDF_ANNOT_" + name) for name in (
    "TEXT", "FREETEXT", "HIGHLIGHT", "UNDERLINE", "SQUIGGLY", "STRIKEOUT", "CARET", "STAMP", "INK",
    "SQUARE", "CIRCLE", "LINE", "POLYGON", "POLYLINE", "FILEATTACHMENT", "SOUND", "REDACT")}
_FIELD_KINDS = {raw.FPDF_FORMFIELD_PUSHBUTTON: "push", raw.FPDF_FORMFIELD_CHECKBOX: "check",
                raw.FPDF_FORMFIELD_RADIOBUTTON: "radio", raw.FPDF_FORMFIELD_COMBOBOX: "choice",
                raw.FPDF_FORMFIELD_LISTBOX: "choice", raw.FPDF_FORMFIELD_TEXTFIELD: "text",
                raw.FPDF_FORMFIELD_SIGNATURE: "sig"}


class PdfiumOpenError(Exception):
    """pdfium could not open the file, for a reason other than a password."""


class _Refused(Exception):
    """An image the page's decode budget leaves undecoded."""


# ---- small helpers

def _vertical_fonts(native, resources) -> bool:
    """Whether the page's own /Resources /Font names a Type0 font whose Encoding is a vertical
    CMap, by name or by its stream's /WMode entry (an embedded CMap's body is not decoded here;
    pdfium's geometry finds those columns). At most PAGE_FONTS fonts."""
    pdf = native.pdf
    fonts = pdf.resolve(resources.get("Font"))
    if not isinstance(fonts, dict):
        return False
    for _name, ref in zip(range(PAGE_FONTS), fonts.values()):
        font = pdf.resolve(ref)
        if not isinstance(font, dict) or str(font.get("Subtype", "")) != "Type0":
            continue
        encoding = pdf.resolve(font.get("Encoding"))
        if isinstance(encoding, PDFStream):
            mode = pdf.resolve(encoding.dictionary.get("WMode"))
            if mode == 1 and not isinstance(mode, bool):
                return True
        elif encoding is not None and encoding_wmode(str(encoding)) == 1:
            return True
    return False


def _system_name(pdf, value) -> str:
    value = pdf.resolve(value)
    if isinstance(value, bytes) and len(value) <= 32:
        return value.decode("ascii", "ignore")
    return str(value) if isinstance(value, str) else ""


def _base_name(pdf, value) -> str:
    """A BaseFont without its subset tag, as PdfiumDoc._font gives it."""
    name = pdf.resolve(value)
    name = str(name) if isinstance(name, str) else ""
    tag, plus, rest = name.partition("+")
    return rest if plus and len(tag) == 6 and tag.isupper() else name


def _guessed_cid_fonts(native, resources) -> frozenset:
    """The base font names (Type0's and its CIDFont's) of the Type0 fonts in the page's /Resources
    /Font, or a form XObject's the page holds (FORM_DEPTH levels), whose CIDs native cannot look up
    in an Adobe ordering's table: where no ToUnicode entry maps a code, native reverses the font's
    embedded TrueType cmap (as fonts.cid_fallback_decoder does for an embedded Identity-GID font
    even of an Adobe ordering), or its text is lost. At most PAGE_FONTS fonts and PAGE_FONTS forms."""
    pdf = native.pdf
    found = set()
    stack, seen, fonts_left, forms_left = [(resources, 0)], {id(resources)}, PAGE_FONTS, PAGE_FONTS
    while stack:
        res, depth = stack.pop()
        table = pdf.resolve(res.get("Font"))
        for ref in (table.values() if isinstance(table, dict) else ()):
            fonts_left -= 1
            if fonts_left < 0:
                return frozenset(found)
            font = pdf.resolve(ref)
            if not isinstance(font, dict) or str(font.get("Subtype", "")) != "Type0":
                continue
            kids = pdf.resolve(font.get("DescendantFonts"))
            cid = pdf.resolve(kids[0]) if isinstance(kids, list) and kids else None
            cid = cid if isinstance(cid, dict) else {}
            system = pdf.resolve(cid.get("CIDSystemInfo"))
            system = system if isinstance(system, dict) else {}
            descriptor = pdf.resolve(cid.get("FontDescriptor"))
            embedded_gid = (str(pdf.resolve(cid.get("Subtype"))) == "CIDFontType2"
                            and isinstance(descriptor, dict)
                            and isinstance(pdf.resolve(descriptor.get("FontFile2")), PDFStream)
                            and not isinstance(pdf.resolve(cid.get("CIDToGIDMap")), PDFStream))
            if (embedded_gid or _system_name(pdf, system.get("Registry")) != "Adobe"
                    or _system_name(pdf, system.get("Ordering")) not in _ADOBE_ORDERINGS):
                found.update(n for n in (_base_name(pdf, font.get("BaseFont")),
                                         _base_name(pdf, cid.get("BaseFont"))) if n)
        if depth >= FORM_DEPTH:
            continue
        forms = pdf.resolve(res.get("XObject"))
        for ref in (forms.values() if isinstance(forms, dict) else ()):
            form = pdf.resolve(ref)
            inner = pdf.resolve(form.dictionary.get("Resources")) if isinstance(form, PDFStream) else None
            if isinstance(inner, dict) and id(inner) not in seen:
                forms_left -= 1
                if forms_left < 0:
                    return frozenset(found)
                seen.add(id(inner))
                stack.append((inner, depth + 1))
    return frozenset(found)


def _finite(*values) -> bool:
    return all(isinstance(v, (int, float)) and math.isfinite(v) and abs(v) <= _COORD for v in values)


def _rect(x0, y0, x1, y1):
    """A normalised box with finite coordinates and an area, or None."""
    if not _finite(x0, y0, x1, y1):
        return None
    x0, x1 = sorted((float(x0), float(x1)))
    y0, y1 = sorted((float(y0), float(y1)))
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


def _compose(m, n):
    """m then n, in the pdf row-vector convention (a point is p x m x n)."""
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + b * c2, a * b2 + b * d2, c * a2 + d * c2, c * b2 + d * d2,
            e * a2 + f * c2 + e2, e * b2 + f * d2 + f2)


def _apply(m, x, y):
    return x * m[0] + y * m[2] + m[4], x * m[1] + y * m[3] + m[5]


def _matrix(obj):
    fs = raw.FS_MATRIX()
    if not raw.FPDFPageObj_GetMatrix(obj, fs):
        return _IDENTITY
    return (fs.a, fs.b, fs.c, fs.d, fs.e, fs.f)


def _unit(x, y, default):
    n = math.hypot(x, y)
    return (x / n, y / n) if n > 0 and math.isfinite(n) else default


def _oc_marked(handle) -> bool:
    """Whether a page object carries an /OC content mark (at most OBJECT_MARKS marks read)."""
    name = (raw.FPDF_WCHAR * 8)()
    size = ctypes.c_ulong()
    for i in range(min(max(raw.FPDFPageObj_CountMarks(handle), 0), OBJECT_MARKS)):
        mark = raw.FPDFPageObj_GetMark(handle, i)
        if (mark and raw.FPDFPageObjMark_GetName(mark, name, ctypes.sizeof(name), size)
                and size.value == 6 and bytes(name)[:4] == b"O\x00C\x00"):
            return True
    return False


def _address(handle) -> int:
    return ctypes.cast(handle, ctypes.c_void_p).value or 0


def _wide(fn, *args) -> str:
    """A UTF-16LE string from a pdfium getter that takes (buffer, length in bytes), the NUL
    stripped."""
    n = fn(*args, None, 0)
    if n <= 2:
        return ""
    buf = (raw.FPDF_WCHAR * ((n + 1) // 2))()
    fn(*args, buf, n)
    return bytes(buf)[:n].decode("utf-16-le", "replace").rstrip("\x00")


def _rgb_rows(data, width, height, stride, fmt):
    """(rows, alpha) of a pdfium bitmap buffer as RGB (or RGBA) rows, or None."""
    rows = []
    if fmt == raw.FPDFBitmap_Gray:
        for y in range(height):
            line = data[y * stride:y * stride + width]
            out = bytearray(width * 3)
            out[0::3] = out[1::3] = out[2::3] = line
            rows.append(bytes(out))
        return rows, False
    if fmt in (raw.FPDFBitmap_BGR, raw.FPDFBitmap_BGRx, raw.FPDFBitmap_BGRA, raw.FPDFBitmap_BGRA_Premul):
        step = 3 if fmt == raw.FPDFBitmap_BGR else 4
        alpha = fmt in (raw.FPDFBitmap_BGRA, raw.FPDFBitmap_BGRA_Premul)
        span = 4 if alpha else 3
        for y in range(height):
            line = data[y * stride:y * stride + width * step]
            out = bytearray(width * span)
            out[0::span] = line[2::step]
            out[1::span] = line[1::step]
            out[2::span] = line[0::step]
            if alpha:
                out[3::span] = line[3::step]
            rows.append(bytes(out))
        return rows, alpha
    return None


# ---- the document

class PdfiumDoc:
    """An open pdf read by pdfium. `native` is the NativeDoc of the same bytes, or None when the
    native parser could not open them."""

    def __init__(self, data: bytes, native=None):
        try:
            self._pdf = pdfium.PdfDocument(data)
        except pdfium.PdfiumError as e:
            if getattr(e, "err_code", None) == raw.FPDF_ERR_PASSWORD:
                raise ReadError("encrypted", "user password") from None
            raise PdfiumOpenError(str(e)) from None
        except Exception as e:  # noqa: BLE001 - any other open failure falls back to native
            raise PdfiumOpenError(type(e).__name__) from None
        # init_forms logs a pip install hint on an XFA file, which read.py would relay: pypdfium2's
        # warnings are not this reader's output (it reports XFA in its own words)
        logging.getLogger("pypdfium2").setLevel(logging.ERROR)
        try:
            self._pdf.init_forms()  # before any page is loaded, so widgets have a form handle
        except Exception:  # noqa: BLE001 - no form environment: fields are not read
            pass
        self.native = native
        self.cut: Counter = Counter()
        self._page = None   # the page last loaded: open while pdf.py may still call its render
        self.decoded = 0    # pixels decoded or rendered from page images, all pages together
        self.page_count = len(self._pdf)
        if native is not None:
            self.xfa, self.attachments = native.xfa, native.attachments
            # fields are native's, read from /AcroForm /Fields (a field with no widget too), and
            # what native's walk of them cut is counted once here
            if native.cut["field"]:
                self.cut["field"] += native.cut["field"]
        else:
            self.xfa = raw.FPDF_GetFormType(self._pdf.raw) in (raw.FORMTYPE_XFA_FULL,
                                                               raw.FORMTYPE_XFA_FOREGROUND)
            self.attachments = raw.FPDFDoc_GetAttachmentCount(self._pdf.raw) > 0
        self._read: set = set()
        self._unmapped_seen: set = set()  # base font names counted `glyphs` (native None)
        self._field_page: dict = {}  # full field name -> the page its entry goes on

    def close(self):
        """Close the open page, the form environment and the document (pdf.py calls it in a finally)."""
        self._close_page()
        if self._pdf is not None:
            try:
                self._pdf.close()  # closes the form environment and any page still tracked first
            finally:
                self._pdf = None

    def _close_page(self):
        if self._page is not None:
            try:
                self._page.close()
            finally:
                self._page = None

    def outline(self):
        """[(depth from 1, title, 0-based page)] from get_toc(): the depth is level + 1 and the page
        dest.get_index() (a GoTo action's destination when the item has no /Dest). At most
        OUTLINE_ITEMS items, from at most OUTLINE_SCAN bookmarks."""
        out = []
        doc = self._pdf.raw
        for visited, mark in enumerate(self._pdf.get_toc(max_depth=annotations.OUTLINE_DEPTH)):
            if len(out) >= annotations.OUTLINE_ITEMS or visited >= OUTLINE_SCAN:
                break
            title = (mark.get_title() or "").strip()
            if not title:
                continue
            dest = mark.get_dest()
            page = dest.get_index() if dest is not None else None
            if page is None:
                action = raw.FPDFBookmark_GetAction(mark.raw)
                if action and raw.FPDFAction_GetType(action) == raw.PDFACTION_GOTO:
                    target = raw.FPDFAction_GetDest(doc, action)
                    found = raw.FPDFDest_GetDestPageIndex(doc, target) if target else -1
                    page = found if found >= 0 else None
            if page is not None:
                out.append((mark.level + 1, title[:1_000], page))
        return out

    # -- one page

    def page(self, index: int) -> PageData:
        # pdf.py is done with the page before (its renders included) when it asks for the next one:
        # closing it frees the images pdfium cached for it
        self._close_page()
        page = self._page = self._pdf[index]
        try:
            return self._read_page(page, index)
        except BaseException:
            self._close_page()
            raise

    def _read_page(self, page, index):
        cut: Counter = Counter()
        try:
            box = _rect(*page.get_bbox()) or _LETTER
        except Exception:  # noqa: BLE001
            box = _LETTER
        try:
            turn = page.get_rotation()
        except Exception:  # noqa: BLE001
            turn = 0
        # with native, a char pdfium has no Unicode for is dropped (and its page native's) only in a
        # font native would guess: a simple font's code with no glyph name stays
        drop = None if self.native is None else self._guessed(index)
        textpage = page.get_textpage()
        try:
            fragments, vertical, (unmapped, lost) = self._chars(textpage, cut, drop)
        finally:
            textpage.close()
        native = self._native_fragments(index, cut, vertical, bool(lost))
        if native is not None:
            fragments, unmapped = native
        else:
            for name in lost - self._unmapped_seen:
                self._unmapped_seen.add(name)
                cut["glyphs"] += 1
        chars = sum(1 for f in fragments for ch in f.text if not ch.isspace())
        collector = PathCollector()
        drawn, whole = self._walk(page, collector, cut)
        # every image the page draws, by its pixel size (no decode): a render decodes all of them,
        # so past PDF_PAGE_DECODE_PIXELS (or with the walk cut short, the total unknown) the page
        # has no render at all, and its images are decoded one by one within the budget
        budget = caps.PDF_PAGE_DECODE_PIXELS
        total = sum(px[0] * px[1] for _obj, _m, px in drawn)
        renders = whole and total <= budget
        left = [budget]

        def render(target, dpi):
            if not renders:
                raise ReadError("too_large", "page images past the decode budget")
            return self._render(page, box, turn, target, dpi)

        # an image the budget refused has no data, so pdf.py counts it `image` as it does any
        # image it cannot take
        shown, capped = self._images(drawn, box, chars, render, renders, left)
        self.decoded += budget - left[0]
        if capped:
            cut["image"] += 1
        links, comments, fields = self._annots(page, index, cut)
        if index not in self._read:
            self._read.add(index)
            self.cut.update(cut)
        return PageData(number=index + 1, box=box, rotate=turn, fragments=fragments,
                        segments=collector.segments, short=collector.short_segments, images=shown,
                        figure=collector.figure, links=links, fields=fields, comments=comments,
                        chars=chars, cut=cut, render=render, unmapped=unmapped)

    def _guessed(self, index) -> frozenset:
        native = self.native
        if index >= native.page_count:
            return frozenset()
        try:
            return _guessed_cid_fonts(native, native.resources(index))
        except Exception:  # noqa: BLE001
            return frozenset()

    def _native_fragments(self, index, cut, vertical, unmapped):
        """(fragments, glyphs without Unicode) of the native interpreter, on three kinds of page;
        None elsewhere.
        - Decision 3: a page whose resources name an off group (their hidden="ocg" marks the off
          text). Should the native page fail, its text is left out (counted page_part) rather
          than shown with the off layer's text in it.
        - A page with vertical text: pdfium saw a column (`vertical`), or the page's fonts name a
          vertical CMap (`_vertical_fonts`, which also finds a one-glyph column). Native's
          vertical fragments are the reference; should its page fail, pdfium's own stay.
        - A page where pdfium dropped chars it has no Unicode for (`unmapped`, in a font of
          `_guessed_cid_fonts`): native reverses the font's embedded cmap, or drops the text and
          counts `glyphs`; should its page fail, pdfium's own stay, those chars dropped."""
        native = self.native
        if native is None or index >= native.page_count:
            return None
        try:
            resources = native.resources(index)
            off = native.oc.any_off(resources)
        except Exception:  # noqa: BLE001
            return None
        if not off and not vertical and not unmapped and not _vertical_fonts(native, resources):
            return None
        try:
            got = native.page(index)
        except ReadError as e:
            if e.kind == "timeout":
                raise
            if not off:
                return None
            cut["page_part"] += 1
            return [], 0
        except Exception:  # noqa: BLE001
            if not off:
                return None
            cut["page_part"] += 1
            return [], 0
        for kind, value in got.cut.items():
            if kind not in ("image", "annotation", "field"):  # pdfium reads those itself
                cut[kind] += value
        return list(got.fragments), got.unmapped

    # -- chars

    def _chars(self, textpage, cut, drop):
        """(fragments, whether a vertical column was seen, (the chars dropped for having no
        Unicode, the base font names they were in)); they are dropped in the fonts named in `drop`,
        or in any font when it is None. A text object's direction is its char
        matrix's x axis, unless its second char sits below its first along the glyph's up: a
        vertical font, whose glyphs advance down the up axis; the object then reads with native's
        vertical axes (direction -up, up the x axis), which pdf_layout.vertical_lines takes."""
        tp = textpage.raw
        count = raw.FPDFText_CountChars(tp)
        if count <= 0:
            return [], False, (0, set())
        limit = CHAR_SCAN * caps.PDF_PAGE_CHARS
        scan_cut = count > limit  # counted page_part only when the scan, not the char cap, stops
        if scan_cut:
            count = limit
        rect, fsm = raw.FS_RECTF(), raw.FS_MATRIX()
        ox, oy = ctypes.c_double(), ctypes.c_double()
        frags, objects, modes, fonts, axes, sizes, hidden = [], {}, {}, {}, {}, {}, {}
        # text object: (font handle, Tf, the char matrix's x scale, the widths of its font at Tf
        # by char (None where pdfium has none), whether its text runs along an axis), or None
        advances = {}
        # font address: (base font name, {Tf: {char: width or None}}); a page's fonts live while
        # it is open, so their addresses are theirs for the page
        fontinfo = {}
        tl, tr, tb, tt = (ctypes.c_double() for _ in range(4))
        glyph = ctypes.c_float()
        checked = set()  # text objects whose second char was looked at for a vertical advance
        marks = self.native is None  # with native, an off layer's page is native's (decision 3)
        vertical = False
        upright = set()  # text objects of a vertical font
        run = None  # the fragment being built: a dict
        seen = 0    # non-space chars kept
        bad = False
        capped = False
        lost = set()  # base font names of the chars dropped for having no Unicode
        names = {}    # font address: base font name, of the chars pdfium has no Unicode for
        dropped = 0

        def begin(px, py, start, end, base, axis, size):
            return {"addr": addr, "x": px, "y": py, "dx": axis[0], "dy": axis[1], "ux": axis[2],
                    "uy": axis[3], "base": base, "start": start, "end": end, "size": size,
                    "text": [], "offsets": [], "gaps": [], "first": None, "vertical": addr in upright,
                    "last": None}

        def flush():
            nonlocal run
            if run is not None and run["text"]:
                frags.append(self._fragment(run, len(frags)))
            run = None

        k = 0
        while k < count:
            c = raw.FPDFText_GetUnicode(tp, k)
            generated = raw.FPDFText_IsGenerated(tp, k) == 1
            at = k
            k += 1
            if generated:
                if c in (0x0D, 0x0A):
                    flush()
                continue
            if drop != frozenset() and raw.FPDFText_HasUnicodeMapError(tp, at) == 1:
                # no ToUnicode entry and no CID ordering for the code: pdfium's char is the raw
                # code (a glyph id read as a code point), never shown as text
                handle = raw.FPDFText_GetTextObject(tp, at)
                font = raw.FPDFTextObj_GetFont(handle) if handle else None
                key = _address(font)
                if key not in names:
                    names[key] = self._font(font)
                if drop is None or names[key] in drop:
                    lost.add(names[key])
                    dropped += 1
                    continue
            if 0xDC00 <= c <= 0xDFFF:
                continue  # a low surrogate with no high one before it
            if 0xD800 <= c <= 0xDBFF:
                low = raw.FPDFText_GetUnicode(tp, k) if k < count else 0
                if 0xDC00 <= low <= 0xDFFF:
                    c = 0x10000 + ((c - 0xD800) << 10) + (low - 0xDC00)
                    k += 1
                else:
                    c = 0xFFFD
            if c in _HYPHENS:
                ch = "-"
            elif c == 0:
                continue
            else:
                ch = chr(c)
            if ch in ("\r", "\n"):
                flush()
                continue
            if not (raw.FPDFText_GetLooseCharBox(tp, at, rect) and raw.FPDFText_GetCharOrigin(tp, at, ox, oy)):
                continue
            left, right, bottom, top, x, y = rect.left, rect.right, rect.bottom, rect.top, ox.value, oy.value
            # a NaN fails every comparison, so this also refuses NaN and infinities
            if not (-_COORD <= left <= _COORD and -_COORD <= right <= _COORD and -_COORD <= bottom <= _COORD
                    and -_COORD <= top <= _COORD and -_COORD <= x <= _COORD and -_COORD <= y <= _COORD):
                bad = True
                continue
            handle = raw.FPDFText_GetTextObject(tp, at)
            addr = _address(handle)
            if addr not in objects:
                # the text matrix, render mode and font are the text object's: read once
                objects[addr] = len(objects)
                modes[addr] = raw.FPDFTextObj_GetTextRenderMode(handle) in (3, 7) if handle else False
                font = raw.FPDFTextObj_GetFont(handle) if handle else None
                info = fontinfo.get(_address(font))
                if info is None:
                    info = fontinfo[_address(font)] = (self._font(font), {})
                fonts[addr] = info[0]
                hidden[addr] = "ocg" if marks and handle and _oc_marked(handle) else ""
                fs = raw.FPDFText_GetFontSize(tp, at)
                if raw.FPDFText_GetMatrix(tp, at, fsm) and _finite(fsm.a, fsm.b, fsm.c, fsm.d):
                    dx, dy = _unit(fsm.a, fsm.b, (1.0, 0.0))
                    axes[addr] = (dx, dy, *_unit(fsm.c, fsm.d, (-dy, dx)))
                    # GetFontSize is the Tf operand alone, and the char matrix is Th x Tm x CTM.
                    # Th scales only its first row, so the second row's length is the Tm x CTM
                    # scale native multiplies Tf by (equal to native's sqrt|det| for any matrix
                    # whose x and y scales agree, Tz or not)
                    scale = math.hypot(fsm.c, fsm.d)
                    advances[addr] = ((font, fs, math.hypot(fsm.a, fsm.b), info[1].setdefault(fs, {}),
                                       abs(dx) < 1e-6 or abs(dy) < 1e-6)
                                      if font and math.isfinite(fs) and fs > 0 else None)
                else:
                    axes[addr] = (1.0, 0.0, 0.0, 1.0)
                    scale = 1.0
                    advances[addr] = None
                size = fs * scale if _finite(fs) else 0.0
                if not size or abs(size) > _COORD:
                    # no usable font size: the loose box's extent across the line stands in
                    ux, uy = axes[addr][2:]
                    size = (right - left) * abs(ux) + (top - bottom) * abs(uy)
                sizes[addr] = size
            dx, dy, ux, uy = axes[addr]
            size = sizes[addr]
            # a char at the origin of the one before, in its text object: the next char of a
            # ligature glyph ("fi" from one code), which stays in the run
            last = run["last"] if run is not None and run["addr"] == addr else None
            ligature = (last is not None and -1e-3 <= x - last[0] <= 1e-3
                        and -1e-3 <= y - last[1] <= 1e-3)
            if (run is not None and run["addr"] == addr and len(run["text"]) == 1
                    and addr not in checked and not ligature):
                checked.add(addr)
                step_x, step_y = x - run["x"], y - run["y"]
                if (step_x * ux + step_y * uy < -0.5 * abs(size)
                        and abs(step_x * dx + step_y * dy) <= LINE_TOLERANCE * abs(size)):
                    # a vertical font: the run's first char is placed again on the vertical axes
                    vertical = True
                    upright.add(addr)
                    axes[addr] = dx, dy, ux, uy = -ux, -uy, dx, dy
                    fx, fy, fl, fr, fb, ft = run["first"]
                    first = run["text"][0]
                    s0 = fx * dx + fy * dy
                    e0 = (fr * dx if dx >= 0 else fl * dx) + (ft * dy if dy >= 0 else fb * dy)
                    run = begin(fx, fy, s0, e0, fx * ux + fy * uy, axes[addr], size)
                    run["gaps"].append(0.0)
                    run["offsets"].append(0.0)
                    run["text"].append(first)
                    run["end"] = max(e0, s0)
                    run["obj"], run["invisible"], run["font"] = objects[addr], modes[addr], fonts[addr]
                    run["hidden"] = hidden[addr]
            # the loose box gives the geometry: projected on the text direction, where the
            # advance ends. It also holds the glyph's ink, so where the ink reaches its far edge
            # (an f or an italic letter overhanging its advance), or the text runs at an angle
            # (the box is then upright around the turned glyph), the advance ends at the font's
            # width for the char instead, when pdfium has one that fits inside the box
            start = x * dx + y * dy
            end = (right * dx if dx >= 0 else left * dx) + (top * dy if dy >= 0 else bottom * dy)
            adv = advances[addr]
            if adv is not None and not ligature and end > start and addr not in upright:
                font_width = adv[3].get(c, False)
                if font_width is False:
                    font_width = (glyph.value if c <= 0xFFFF and c not in _HYPHENS
                                  and raw.FPDFFont_GetGlyphWidth(adv[0], c, adv[1], ctypes.byref(glyph))
                                  and math.isfinite(glyph.value) else None)
                    adv[3][c] = font_width
                if font_width is not None:
                    width = font_width * adv[2]
                    straight = adv[4]
                    # the tight box is read only for a width narrower than the box: most glyphs'
                    # loose box is their advance, which their font width fills to float rounding
                    if ((ADVANCE_FLOOR * (end - start) if straight else 0.0) < width
                            < end - start - _ROUNDING
                            and (not straight or (
                                raw.FPDFText_GetCharBox(tp, at, tl, tr, tb, tt)
                                and (tr.value * dx if dx >= 0 else tl.value * dx)
                                + (tt.value * dy if dy >= 0 else tb.value * dy)
                                >= end - _ROUNDING))):
                        end = start + width
            base = x * ux + y * uy
            if ligature:
                # no gap and the glyph's offset; the run ends where the whole glyph does
                run["gaps"].append(0.0)
                run["offsets"].append(run["offsets"][-1])
                run["text"].append(ch)
                run["end"] = max(run["end"], end, start)
            else:
                if run is not None:
                    tol = LINE_TOLERANCE * max(abs(size), abs(run["size"]))
                    if (run["addr"] != addr or abs(base - run["base"]) > tol
                            or start < run["end"] - tol):
                        flush()
                if run is None:
                    run = begin(x, y, start, end, base, axes[addr], size)
                    run["first"] = (x, y, left, right, bottom, top)
                run["gaps"].append(start - run["end"] if run["text"] else 0.0)
                run["offsets"].append(start - run["start"])
                run["text"].append(ch)
                run["end"] = max(end, start)
                run["last"] = (x, y)
                run["obj"], run["invisible"], run["font"] = objects[addr], modes[addr], fonts[addr]
                run["hidden"] = hidden[addr]
            if not ch.isspace():
                seen += 1
                if seen > caps.PDF_PAGE_CHARS:
                    capped = True
                    break  # pdf.py counts the rest as `text`
        flush()
        if bad:
            cut["bad_coord"] += 1
        if scan_cut and not capped:
            cut["page_part"] += 1
        return frags, vertical, (dropped, lost)

    @staticmethod
    def _fragment(run, order):
        text = "".join(run["text"])
        width = max(0.0, run["end"] - run["start"])
        return Fragment(x=run["x"], y=run["y"], width=width, size=run["size"], text=text,
                        space_width=0.25 * abs(run["size"]), order=order, dir_x=run["dx"],
                        dir_y=run["dy"], up_x=run["ux"], up_y=run["uy"],
                        char_offsets=tuple(run["offsets"]) + (width,), char_gaps=tuple(run["gaps"]),
                        invisible=run["invisible"], font=run["font"], obj=run["obj"],
                        hidden=run["hidden"], vertical=run["vertical"])

    @staticmethod
    def _font(font) -> str:
        """The base font name of a font handle, its subset tag stripped."""
        if not font:
            return ""
        n = raw.FPDFFont_GetBaseFontName(font, None, 0)
        if n <= 1 or n > 1_024:
            return ""
        buf = ctypes.create_string_buffer(n)
        raw.FPDFFont_GetBaseFontName(font, buf, n)
        name = buf.value.decode("latin-1", "replace")
        tag, plus, rest = name.partition("+")
        return rest if plus and len(tag) == 6 and tag.isupper() else name

    # -- page objects

    def _walk(self, page, collector, cut):
        """Paths into `collector`; ([(image handle, matrix, (width, height) pixels)] in content
        order, whether every object was visited). Iterative, to FORM_DEPTH form levels and
        pdf.PDF_PAGE_OBJECTS objects. The pixel size is read without decoding."""
        images = []
        whole = True
        w, h = ctypes.c_uint(), ctypes.c_uint()
        visited = 0
        points = [PATH_POINTS]
        deep = False
        frames = [[page.raw, False, _IDENTITY, 0, 0, raw.FPDFPage_CountObjects(page.raw)]]
        while frames:
            frame = frames[-1]
            parent, form, outer, depth, i, n = frame
            if i >= n:
                frames.pop()
                continue
            frame[4] += 1
            visited += 1
            if visited > caps.PDF_PAGE_OBJECTS:
                cut["page_part"] += 1
                whole = False
                break
            obj = raw.FPDFFormObj_GetObject(parent, i) if form else raw.FPDFPage_GetObject(parent, i)
            if not obj:
                continue
            kind = raw.FPDFPageObj_GetType(obj)
            if kind not in (raw.FPDF_PAGEOBJ_PATH, raw.FPDF_PAGEOBJ_IMAGE, raw.FPDF_PAGEOBJ_FORM):
                continue
            m = _compose(_matrix(obj), outer)
            if kind == raw.FPDF_PAGEOBJ_PATH:
                self._path(obj, m, collector, points)
            elif kind == raw.FPDF_PAGEOBJ_IMAGE:
                if not raw.FPDFImageObj_GetImagePixelSize(obj, w, h):
                    w.value = h.value = 0
                images.append((obj, m, (w.value, h.value)))
            elif depth + 1 < FORM_DEPTH:
                frames.append([obj, True, m, depth + 1, 0, raw.FPDFFormObj_CountObjects(obj)])
            elif not deep and raw.FPDFFormObj_CountObjects(obj) > 0:
                deep = True
                whole = False  # a render would still draw (and decode) what lies below
                cut["page_part"] += 1
        return images, whole

    @staticmethod
    def _path(obj, m, collector, points):
        n = raw.FPDFPath_CountSegments(obj)
        if n <= 0 or points[0] <= 0:
            return
        if n > points[0]:
            n = points[0]
        points[0] -= n
        x, y = ctypes.c_float(), ctypes.c_float()
        curve = []
        for k in range(n):
            seg = raw.FPDFPath_GetPathSegment(obj, k)
            if not seg or not raw.FPDFPathSegment_GetPoint(seg, x, y):
                continue
            kind = raw.FPDFPathSegment_GetType(seg)
            if kind == raw.FPDF_SEGMENT_MOVETO:
                curve = []
                collector.operate(b"m", [x.value, y.value], m)
            elif kind == raw.FPDF_SEGMENT_LINETO:
                curve = []
                collector.operate(b"l", [x.value, y.value], m)
            elif kind == raw.FPDF_SEGMENT_BEZIERTO:
                curve += [x.value, y.value]
                if len(curve) == 6:
                    collector.operate(b"c", curve, m)
                    curve = []
            if raw.FPDFPathSegment_GetClose(seg):
                collector.operate(b"h", [], m)
        fill, stroke = ctypes.c_int(), ctypes.c_int()
        if not raw.FPDFPath_GetDrawMode(obj, fill, stroke):
            collector.discard()
            return
        filled = fill.value != raw.FPDF_FILLMODE_NONE
        op = b"B" if filled and stroke.value else b"f" if filled else b"S" if stroke.value else b"n"
        collector.operate(op, [], m)

    def _images(self, drawn, crop, chars, render, renders, left):
        """(PageImages of the drawn images, whether past pdf.PDF_PAGE_IMAGES). At most PDF_PAGE_IMAGES images (the rest count `image`
        once). Bytes are not taken for images pdf.py leaves out anyway: under the size floor,
        or the page's scan, which pdf.py renders whole. A decode or a render takes its pixels
        from `left` (a one-item list, the page's budget): an image that would take it below
        zero, or that needs a render on a page with none (`renders` false), is not decoded."""
        placed = []
        capped = False
        for obj, m, px in drawn:
            if len(placed) >= caps.PDF_PAGE_IMAGES:
                capped = True
                break
            corners = [_apply(m, u, v) for u in (0.0, 1.0) for v in (0.0, 1.0)]
            box = _rect(min(p[0] for p in corners), min(p[1] for p in corners),
                        max(p[0] for p in corners), max(p[1] for p in corners))
            if box is None:
                continue
            placed.append((obj, box, px))
        scan = -1
        if chars == 0:
            covers = [(caps._cover(box, crop), i) for i, (_o, box, _px) in enumerate(placed)]
            best = max(covers, default=(0.0, -1))
            if best[0] >= caps.SCAN_COVER:
                scan = best[1]
        shown = []
        for i, (obj, box, px) in enumerate(placed):
            small = (min(px) < caps.IMAGE_MIN_PX
                     or min(box[2] - box[0], box[3] - box[1]) < caps.IMAGE_MIN_PT)
            if i == scan or small:
                shown.append(PageImage(box, None, px, "raw"))
                continue
            data, kind = None, "raw"
            try:
                data, kind = self._image_bytes(obj, box, px, render, renders, left)
            except _Refused:
                data, kind = None, "raw"
            except ReadError as e:
                if e.kind == "timeout":
                    raise
            except Exception:  # noqa: BLE001 - an image that cannot be taken counts `image` in pdf.py
                data, kind = None, "raw"
            shown.append(PageImage(box, data, px, kind))
        return shown, capped

    @staticmethod
    def _image_bytes(obj, box, px, render, renders, left):
        pixels = px[0] * px[1]
        if pixels > reader_images._MAX_PIXELS:
            if not renders or pixels > left[0]:
                raise _Refused
            left[0] -= pixels
            return render(box, caps.CROP_DPI), "png"
        filters = []
        for k in range(min(raw.FPDFImageObj_GetImageFilterCount(obj), IMAGE_FILTERS)):
            n = raw.FPDFImageObj_GetImageFilter(obj, k, None, 0)
            buf = ctypes.create_string_buffer(max(n, 1))
            raw.FPDFImageObj_GetImageFilter(obj, k, buf, n)
            filters.append(buf.value.decode("latin-1", "replace"))
        if filters and filters[-1] in _DCT and all(f in _SIMPLE for f in filters[:-1]):
            get = raw.FPDFImageObj_GetImageDataDecoded if len(filters) > 1 else raw.FPDFImageObj_GetImageDataRaw
            n = get(obj, None, 0)
            if 0 < n <= limits.IMAGE_BYTES:
                buf = ctypes.create_string_buffer(n)
                get(obj, buf, n)
                data = buf.raw[:n]
                if reader_images.sniff(data) == "jpg":
                    return data, "jpg"
        if pixels > left[0]:
            raise _Refused
        left[0] -= pixels
        bitmap = raw.FPDFImageObj_GetBitmap(obj)
        if not bitmap:
            return None, "raw"
        try:
            width, height = raw.FPDFBitmap_GetWidth(bitmap), raw.FPDFBitmap_GetHeight(bitmap)
            stride, fmt = raw.FPDFBitmap_GetStride(bitmap), raw.FPDFBitmap_GetFormat(bitmap)
            if width <= 0 or height <= 0 or width * height > reader_images._MAX_PIXELS or stride <= 0:
                return None, "raw"
            pointer = raw.FPDFBitmap_GetBuffer(bitmap)
            if not pointer:
                return None, "raw"
            data = ctypes.string_at(pointer, stride * height)
        finally:
            raw.FPDFBitmap_Destroy(bitmap)
        got = _rgb_rows(data, width, height, stride, fmt)
        if got is None:
            return None, "raw"
        rows, alpha = got
        return reader_images._png(width, height, rows, alpha), "png"

    # -- renders

    @staticmethod
    def _render(page, crop, turn, target, dpi):
        """PNG bytes of the page, or of `target` (a page-space box), at `dpi` lowered so the
        long side is at most RENDER_LONG pixels (a page over 200 inches renders at that cap)."""
        x0, y0, x1, y1 = crop
        if target is not None:
            target = _rect(max(target[0], x0), max(target[1], y0), min(target[2], x1), min(target[3], y1))
            if target is None:
                raise ValueError("render box outside the page")
        else:
            target = crop
        if turn in (90, 180, 270):
            vx0, vy0, vx1, vy1 = caps._view_box(target, turn, crop)
            vw, vh = caps._view_page(turn, crop)[2:]
        else:
            vx0, vy0, vx1, vy1 = target[0] - x0, target[1] - y0, target[2] - x0, target[3] - y0
            vw, vh = x1 - x0, y1 - y0
        long_side = max(vx1 - vx0, vy1 - vy0)
        if not dpi or dpi <= 0 or long_side <= 0:
            raise ValueError("nothing to render")
        # one pixel under the cap: pdfium rounds each edge up
        scale = min(dpi / 72.0, (caps.RENDER_LONG - 1) / long_side)
        bitmap = page.render(scale=scale, crop=(max(0.0, vx0), max(0.0, vy0), max(0.0, vw - vx1),
                                                max(0.0, vh - vy1)), rev_byteorder=True)
        width, height, stride = bitmap.width, bitmap.height, bitmap.stride
        if max(width, height) > caps.RENDER_LONG:
            raise ValueError("render past the long-side cap")
        try:
            data = bytes(bitmap.buffer)
            channels = bitmap.n_channels
        finally:
            bitmap.close()
        got = _rgb_rows_rev(data, width, height, stride, channels)
        # zlib level 1: the default level took 0.19 s of a 0.21 s render of a grained scan (Task 19)
        return reader_images._png(width, height, got, False, 1)

    # -- annotations

    def _annots(self, page, index, cut):
        """(links, comments, fields) of one page."""
        handle = page.raw
        links = self._links(handle)
        total = raw.FPDFPage_GetAnnotCount(handle)
        if total > annotations.PDF_ANNOTS:
            cut["annotation"] += 1
            total = annotations.PDF_ANNOTS
        # the widget walk reads fields only when native could not open the file
        native = self.native
        form = self._pdf.formenv.raw if self._pdf.formenv and native is None else None
        items, done, shown, walked = [], set(), set(), []
        fields = list(native.fields.get(index, [])) if native is not None else []
        for k in range(max(0, total)):
            annot = raw.FPDFPage_GetAnnot(handle, k)
            if not annot:
                continue
            try:
                subtype = raw.FPDFAnnot_GetSubtype(annot)
                if subtype in _MARKUP:
                    item = self._comment(handle, annot, k, cut)
                    if item is not None:
                        items.append(item)
                elif subtype == raw.FPDF_ANNOT_WIDGET and form:
                    self._field(form, annot, index, walked, done, shown, cut)
            finally:
                raw.FPDFPage_CloseAnnot(annot)
        fields += [(label, text, name not in shown) for label, text, name in walked]
        return links, annotations.order_comments(items), fields

    def _links(self, handle):
        links = []
        pos, link = ctypes.c_int(0), raw.FPDF_LINK()
        rect = raw.FS_RECTF()
        for _ in range(annotations.PDF_ANNOTS):  # past it the annotation count above counts
            if not raw.FPDFLink_Enumerate(handle, pos, link):
                break
            action = raw.FPDFLink_GetAction(link)
            if not action or raw.FPDFAction_GetType(action) != raw.PDFACTION_URI:
                continue
            n = raw.FPDFAction_GetURIPath(self._pdf.raw, action, None, 0)
            if n <= 1 or n > annotations.ANNOT_CHARS:
                continue
            buf = ctypes.create_string_buffer(n)
            raw.FPDFAction_GetURIPath(self._pdf.raw, action, buf, n)
            url = buf.value.decode("latin-1").strip()
            scheme, colon, _rest = url.partition(":")
            if not colon or scheme.lower() not in annotations.SCHEMES:
                continue
            if not raw.FPDFLink_GetAnnotRect(link, rect):
                continue
            box = _rect(rect.left, rect.bottom, rect.right, rect.top)
            if box is not None:
                links.append((box, url))
        return links

    @staticmethod
    def _comment(handle, annot, k, cut):
        text = _wide(raw.FPDFAnnot_GetStringValue, annot, b"Contents")
        if len(text) > annotations.ANNOT_CHARS:
            cut["annotation"] += 1
            text = text[:annotations.ANNOT_CHARS]
        text = text.strip()
        if not text:
            return None
        author = _wide(raw.FPDFAnnot_GetStringValue, annot, b"T")[:1_000].strip()
        hidden = bool(raw.FPDFAnnot_GetFlags(annot) & _UNSEEN)
        irt = None
        parent = raw.FPDFAnnot_GetLinkedAnnot(annot, b"IRT")
        if parent:
            try:
                found = raw.FPDFPage_GetAnnotIndex(handle, parent)
                irt = found if found >= 0 else None
            finally:
                raw.FPDFPage_CloseAnnot(parent)
        return k, irt, Comment(author, text, hidden)

    def _field(self, form, annot, index, fields, done, shown, cut):
        """Append (label, value, full name) of the widget's field, once per field; `shown` gets
        the names of the fields with a widget that is neither Hidden nor NoView."""
        kind = _FIELD_KINDS.get(raw.FPDFAnnot_GetFormFieldType(form, annot))
        if kind is None:
            return
        name = _wide(raw.FPDFAnnot_GetFormFieldName, form, annot)
        if not raw.FPDFAnnot_GetFlags(annot) & _UNSEEN:
            shown.add(name)
        if name in done or self._field_page.get(name, index) != index:
            return  # another widget of a field already read
        if kind in ("check", "radio"):
            if not raw.FPDFAnnot_IsChecked(form, annot):
                return  # a radio group's checked kid may come later
            value = (_wide(raw.FPDFAnnot_GetFormFieldExportValue, form, annot)
                     or _wide(raw.FPDFAnnot_GetFormFieldValue, form, annot))
        else:
            value = _wide(raw.FPDFAnnot_GetFormFieldValue, form, annot)
            if raw.FPDFAnnot_GetFormFieldType(form, annot) == raw.FPDF_FORMFIELD_LISTBOX:
                picked = [_wide(raw.FPDFAnnot_GetOptionLabel, form, annot, i)
                          for i in range(min(max(0, raw.FPDFAnnot_GetOptionCount(form, annot)), OPTION_LABELS))
                          if raw.FPDFAnnot_IsOptionSelected(form, annot, i)]
                if len(picked) > 1:
                    value = picked
        done.add(name)
        self._field_page.setdefault(name, index)
        tu = _wide(raw.FPDFAnnot_GetFormFieldAlternateName, form, annot).strip() or None
        entry = annotations.field_entry(tu, name, value, kind)
        if entry is None:
            return
        label, text, was_cut = entry
        if was_cut:
            cut["field"] += 1
        fields.append((label, text, name))


def _rgb_rows_rev(data, width, height, stride, channels):
    """Rows of a page render made with rev_byteorder (RGB, RGBx or gray) as RGB rows."""
    rows = []
    for y in range(height):
        line = data[y * stride:y * stride + width * channels]
        if channels == 3:
            rows.append(bytes(line))
            continue
        out = bytearray(width * 3)
        if channels == 1:
            out[0::3] = out[1::3] = out[2::3] = line
        else:
            out[0::3], out[1::3], out[2::3] = line[0::channels], line[1::channels], line[2::channels]
        rows.append(bytes(out))
    return rows
