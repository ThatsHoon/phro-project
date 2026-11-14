"""pdf content streams: text fragments with their positions, rule segments, figure paths and
image draws, from one page's operators.

Port of dochan 1.21.0 pdf/content.py (MIT), Python path only, without line assembly (that is
in the layout step). The text matrix (Tm/Td/TD/T*), the glyph widths and the CTM give each
text piece its real x and y. A TJ number moves the glyphs after it (`char_offsets`, and `char_gaps` for the extra
displacement before each glyph) and never
adds a space: the gap rule decides spaces from the geometry. A Form XObject runs with its
Matrix and its BBox clip; its text and figure paths join the page, its rule segments do not
(art inside forms would build empty grids in unrelated drawings).

Text is `hidden = "ocg"` inside `/OC /x BDC` of an off group and inside a form whose /OC is
off; render mode 3 or 7 sets `invisible`. Caps, each counted once per dropped thing on the
page's `cut` under `page_part`: a form nested past MAX_FORM_DEPTH or referring to itself,
MAX_FORM_OPERATORS operators in forms per page, MAX_FORM_EXPANDED_BYTES of form data per page,
and `pdf.PDF_PAGE_OBJECTS` page objects (text objects, painted paths, images and forms). Rule
segments stop at PDF_SEGMENTS, ignored and not counted.
"""
import math
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import List, Optional, Sequence, Tuple

from ..formats.pdf_layout import Fragment
from ..formats.pdf_tables import PathCollector, Segment
from .fonts import FontInfo, VerticalMetrics, default_byte_decoder, font_infos
from .objects import DELIMITERS, WHITESPACE, PDFLexer, PDFName, PDFStream, PDFSyntaxError
from .ocg import OCState
from .show_table import font_table
from .structure import PDFFile
from .widths import WidthMap

MAX_FORM_DEPTH = 16
MAX_FORM_CACHE_BYTES = 16 * 1024 * 1024      # one form's decoded data
MAX_FORM_CACHE_TOTAL = 64 * 1024 * 1024      # decoded forms kept for later pages
MAX_FORM_EXPANDED_BYTES = 32 * 1024 * 1024   # form data run on one page, each use counted
MAX_FORM_OPERATORS = 32768                   # operators run in forms on one page
MAX_OPERAND_STACK = 64
MAX_STATE_STACK = 256
MAX_TRACKED_RUN = 50000                      # glyph positions kept for one fragment
MAX_TRACKED_PAGE = 200000                    # glyph positions kept for one page
_COORD = 1e9

_OPERAND_START = b"(</[0123456789+-."
_PATH_OPS = frozenset((b"m", b"l", b"re", b"h", b"c", b"v", b"y"))
_PAINT_OPS = frozenset((b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"n"))
_CONTENT_TOKEN_RE = re.compile(
    rb"[\x00\t\n\x0c\r ]+|%[^\r\n]*|[^\x00\t\n\x0c\r ()<>\[\]{}/%]+|.", re.DOTALL)
_CONTENT_GAP_RE = re.compile(rb"[\x00\t\n\x0c\r ]*(?:%[^\r\n]*[\x00\t\n\x0c\r ]*)*")
_SUBSET_TAG_RE = re.compile(r"^[A-Z]{6}\+")
_IDENTITY = (1, 0, 0, 1, 0, 0)


class ContentScanner:
    """Content stream token scanner; structured operands keep PDFLexer semantics."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self._matches = _CONTENT_TOKEN_RE.finditer(data)
        self.lexer = PDFLexer(data)
        self.lexer.skip_array_operators = True

    def _may_be_reference(self, pos):
        data = self.data
        first = _CONTENT_GAP_RE.match(data, pos).end()
        if first == pos or first >= len(data) or not 0x30 <= data[first] <= 0x39:
            return False
        number = _CONTENT_TOKEN_RE.match(data, first)
        if not number.group().isdigit():
            return False
        second = _CONTENT_GAP_RE.match(data, number.end()).end()
        return (second > number.end() and data[second:second + 1] == b"R"
                and (second + 1 == len(data) or data[second + 1] in WHITESPACE + DELIMITERS))

    def next(self):
        data = self.data
        while True:
            try:
                match = next(self._matches)
            except StopIteration:
                return None
            if match.start() < self.pos:
                if self.pos < match.end():
                    self._matches = _CONTENT_TOKEN_RE.finditer(data, self.pos)
                continue
            token = match.group()
            self.pos = match.end()
            if token[0] in WHITESPACE or token[0] == 0x25:
                continue
            if token[0] in _OPERAND_START:
                if token[0] not in b"(</[":
                    try:
                        value = float(token) if b"." in token else int(token)
                    except ValueError:
                        pass
                    else:
                        if value < 0 or isinstance(value, float) or not self._may_be_reference(self.pos):
                            return "operand", value
                self.lexer.pos = match.start()
                try:
                    value = self.lexer.parse_object()
                except PDFSyntaxError:
                    self.pos = self.lexer.pos + 1
                    continue
                self.pos = self.lexer.pos
                return "operand", value
            if token[0] in DELIMITERS:
                continue
            return "operator", token


@dataclass
class PageContent:
    """What a page's operators gave: text, rules, figure paths and image draws."""
    fragments: List[Fragment]
    segments: Sequence[Segment]
    short: Sequence[Segment]
    figure: tuple  # (union box or None, count of painted paths that gave no rule)
    # (resource name, box, stream) in content order. The name is from the resources of the
    # stream that drew it (a form has its own), so the resolved image XObject `stream` is what
    # to read; it is None when the name does not resolve to a stream.
    image_draws: List[Tuple[str, tuple, Optional[PDFStream]]]
    objects: int  # page objects walked, forms included
    cut: Counter = field(default_factory=Counter)
    unmapped: int = 0  # text operators whose font could not be decoded (their text is gone)


class _Stop(Exception):
    """The page object cap: the walk ends, what was read stays."""


def _num(value) -> float:
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except OverflowError:
            return 0.0
    return 0.0


def _finite(value) -> float:
    """A number as a float; NaN for anything else, a number too big for a float included."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return float(value)
        except OverflowError:
            pass
    return float("nan")


def _matmul(m1, m2):
    """Product of two 2x3 affine matrices (a, b, c, d, e, f): m1 then m2."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def _sane(*values) -> bool:
    return all(math.isfinite(v) and abs(v) <= _COORD for v in values)


def _bbox_polygon(bbox, ctm):
    x0, y0, x1, y1 = bbox
    a, b, c, d, e, f = ctm
    return [(a * x + c * y + e, b * x + d * y + f)
            for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]


def _polygons_overlap(polygon, quad) -> bool:
    """Separating-axis test of two convex quadrilaterals."""
    if not all(math.isfinite(value) for point in quad for value in point):
        return False
    for shape in (polygon, quad):
        for edge, (px, py) in enumerate(shape):
            qx, qy = shape[(edge + 1) % 4]
            axis_length = math.hypot(qy - py, qx - px)
            if not axis_length:
                continue
            nx, ny = -(qy - py) / axis_length, (qx - px) / axis_length
            clip_projection = [nx * x + ny * y for x, y in polygon]
            glyph_projection = [nx * x + ny * y for x, y in quad]
            if (max(clip_projection) < min(glyph_projection) - 1e-7 or
                    max(glyph_projection) < min(clip_projection) - 1e-7):
                return False
    return True


def _clip_form_fragments(fragments, bbox, ctm):
    """Keep the glyphs that overlap a form's transformed BBox, rotations included."""
    polygon = _bbox_polygon(bbox, ctm)
    if not all(math.isfinite(value) for point in polygon for value in point):
        return []
    signed_area = sum(polygon[i][0] * polygon[(i + 1) % 4][1] -
                      polygon[(i + 1) % 4][0] * polygon[i][1] for i in range(4))
    sign = 1 if signed_area >= 0 else -1

    def inside(x, y):
        for index, (px, py) in enumerate(polygon):
            qx, qy = polygon[(index + 1) % 4]
            if sign * ((qx - px) * (y - py) - (qy - py) * (x - px)) < -1e-7:
                return False
        return True

    def overlaps(quad):
        return _polygons_overlap(polygon, quad)

    kept = []
    for frag in fragments:
        count = len(frag.text)
        if not count:
            continue
        direction = math.hypot(frag.dir_x, frag.dir_y)
        upward = math.hypot(frag.up_x, frag.up_y)
        if not direction or not upward:
            continue
        ux, uy = frag.dir_x / direction, frag.dir_y / direction
        vx, vy = frag.up_x / upward, frag.up_y / upward
        if len(frag.char_offsets) != count + 1:
            # No glyph positions: keep a run that overlaps as a whole. This also covers
            # vertical runs, whose origin is shifted by v1y.
            quad = [(frag.x + ux * distance + vx * height,
                     frag.y + uy * distance + vy * height)
                    for distance, height in ((0, 0), (frag.width, 0),
                                             (frag.width, frag.size), (0, frag.size))]
            if overlaps(quad):
                kept.append(frag)
            continue
        offsets = frag.char_offsets
        accepted = []
        for index in range(count):
            first, last = offsets[index:index + 2]
            middle = (first + last) / 2
            if inside(frag.x + ux * middle + vx * frag.size / 2,
                      frag.y + uy * middle + vy * frag.size / 2):
                accepted.append(True)
                continue
            # a glyph partly inside still belongs to the page
            quad = [(frag.x + ux * distance + vx * height,
                     frag.y + uy * distance + vy * height)
                    for distance, height in ((first, 0), (last, 0),
                                             (last, frag.size), (first, frag.size))]
            accepted.append(overlaps(quad))
        if all(accepted):
            kept.append(frag)
            continue
        start = 0
        while start < count:
            if not accepted[start]:
                start += 1
                continue
            end = start + 1
            while end < count and accepted[end]:
                end += 1
            shift = offsets[start]
            kept.append(replace(
                frag, text=frag.text[start:end], x=frag.x + ux * shift,
                y=frag.y + uy * shift, width=offsets[end] - shift,
                char_offsets=tuple(value - shift for value in offsets[start:end + 1]),
                char_gaps=((0.0,) + frag.char_gaps[start + 1:end]) if frag.char_gaps else ()))
            start = end
    return kept


class _State:
    """The graphics and text state that q and Q save and a form inherits."""
    __slots__ = ("ctm", "font", "fname", "fs", "tc", "tw", "th", "tl", "tr", "tm", "tlm")

    def __init__(self, ctm, inherited=None):
        self.ctm = ctm
        self.font, self.fname, self.fs, self.tc, self.tw, self.th, self.tl, self.tr = (
            inherited or (None, "", 0.0, 0.0, 0.0, 1.0, 0.0, 0))
        self.tm = self.tlm = _IDENTITY

    def text_state(self):
        return (self.font, self.fname, self.fs, self.tc, self.tw, self.th, self.tl, self.tr)

    def saved(self):
        return (self.ctm,) + self.text_state()

    def restore(self, saved):
        (self.ctm, self.font, self.fname, self.fs, self.tc, self.tw, self.th, self.tl,
         self.tr) = saved


class _Page:
    """One page's walk: its budgets, its output and the form stack."""

    def __init__(self, pdf: PDFFile, fonts_cache: dict, oc: OCState, object_cap: int):
        self.pdf = pdf
        self.fonts_cache = fonts_cache
        self.oc = oc
        self.object_cap = object_cap
        self.cut: Counter = Counter()
        self.objects = 0
        self.ops = 0
        self.ops_cut = False
        self.clips: list = []  # the transformed BBox polygons of the forms being run
        self.expanded = 0
        self.active = set()
        self.frags: List[Fragment] = []
        self.image_draws: List[Tuple[str, tuple, Optional[PDFStream]]] = []
        self.text_object = -1
        self.positions_left = MAX_TRACKED_PAGE
        self.positions_cut = False
        self.unmapped = 0
        self.link_reliable = True
        self._fonts = {}   # id(resources) -> (resources, {name: FontInfo})
        self._labels = {}  # (id(resources), name) -> BaseFont
        forms = getattr(pdf, "_content_forms", None)
        if forms is None:
            forms = pdf._content_forms = {"bytes": 0, "data": {}}
        self._forms = forms

    # -- bookkeeping --

    def _drop(self) -> None:
        self.cut["page_part"] += 1

    def _drop_ops(self) -> None:
        """The form operator cap, counted once for the page."""
        if not self.ops_cut:
            self.ops_cut = True
            self._drop()

    def _in_clips(self, a, b, c, d, e, f) -> bool:
        """Does the unit square under (a..f) overlap every form BBox being run?"""
        quad = [(e, f), (a + e, b + f), (a + c + e, b + d + f), (c + e, d + f)]
        return all(_polygons_overlap(polygon, quad) for polygon in self.clips)

    def _object(self) -> None:
        if self.objects >= self.object_cap:
            self._drop()
            raise _Stop
        self.objects += 1

    def _fonts_for(self, resources):
        known = self._fonts.get(id(resources))
        if known is None:
            known = (resources, font_infos(self.pdf, resources, self.fonts_cache))
            self._fonts[id(resources)] = known
        return known[1]

    def _label(self, resources, name: str) -> str:
        key = (id(resources), name)
        label = self._labels.get(key)
        if label is None:
            label = name
            table = self.pdf.resolve(resources.get("Font")) if isinstance(resources, dict) else None
            font = self.pdf.resolve(table.get(name)) if isinstance(table, dict) else None
            if isinstance(font, dict):
                base = self.pdf.resolve(font.get("BaseFont"))
                if isinstance(base, str) and base:
                    label = _SUBSET_TAG_RE.sub("", str(base))
            self._labels[key] = label
        return label

    def _property(self, resources, name):
        props = self.pdf.resolve(resources.get("Properties")) if isinstance(resources, dict) else None
        return props.get(name) if isinstance(props, dict) else None

    # -- the operator loop --

    def run(self, data, resources, ctm, inherited, depth, off, frags, paths: PathCollector) -> None:
        pdf = self.pdf
        st = _State(ctm, inherited)
        infos = self._fonts_for(resources) if isinstance(resources, dict) else {}
        scanner = ContentScanner(data)
        stack: list = []
        overflow = 0
        marks: list = []  # one bool per open marked-content sequence: is its layer off
        off_marks = 0
        operands: list = []
        while True:
            event = scanner.next()
            if event is None:
                break
            kind, value = event
            if kind == "operand":
                operands.append(value)
                if len(operands) > MAX_OPERAND_STACK:
                    del operands[:-8]
                continue
            op = value
            if depth:
                self.ops += 1
                if self.ops > MAX_FORM_OPERATORS:
                    self._drop_ops()
                    return
            if op in (b"Tj", b"TJ", b"'", b'"'):
                self._text_operator(op, operands, st, off or off_marks > 0, frags)
            elif op in (b"Td", b"TD", b"Tm", b"T*"):
                self.link_reliable = True
                if op == b"Tm" and len(operands) >= 6:
                    st.tm = st.tlm = tuple(_num(o) for o in operands[-6:])
                elif op == b"T*":
                    st.tlm = st.tm = _matmul((1, 0, 0, 1, 0, -st.tl), st.tlm)
                elif op != b"Tm" and len(operands) >= 2:
                    tx, ty = _num(operands[-2]), _num(operands[-1])
                    if op == b"TD":
                        st.tl = -ty
                    st.tlm = st.tm = _matmul((1, 0, 0, 1, tx, ty), st.tlm)
            elif op == b"BT":
                self._object()
                self.text_object += 1
                self.link_reliable = True
                st.tm = st.tlm = _IDENTITY
            elif op == b"Tf":
                if len(operands) >= 2 and isinstance(operands[-2], PDFName):
                    name = str(operands[-2])
                    st.font = infos.get(name)
                    st.fname = self._label(resources, name) if st.font is not None else ""
                if operands and isinstance(operands[-1], (int, float)):
                    st.fs = _num(operands[-1])
            elif op == b"Tc" and operands and isinstance(operands[-1], (int, float)):
                st.tc = _num(operands[-1])
            elif op == b"Tw" and operands and isinstance(operands[-1], (int, float)):
                st.tw = _num(operands[-1])
            elif op == b"Tz" and operands and isinstance(operands[-1], (int, float)):
                st.th = _num(operands[-1]) / 100.0
            elif op == b"TL" and operands and isinstance(operands[-1], (int, float)):
                st.tl = _num(operands[-1])
            elif op == b"Tr" and operands and isinstance(operands[-1], (int, float)):
                number = _num(operands[-1])
                if number.is_integer():
                    st.tr = int(number)
            elif op == b"q":
                if len(stack) < MAX_STATE_STACK:
                    stack.append(st.saved())
                else:
                    overflow += 1
            elif op == b"Q":
                if overflow:
                    overflow -= 1
                elif stack:
                    st.restore(stack.pop())
            elif op == b"cm" and len(operands) >= 6:
                st.ctm = _matmul(tuple(_num(o) for o in operands[-6:]), st.ctm)
            elif op in _PATH_OPS or op in _PAINT_OPS:
                if op in _PAINT_OPS and op != b"n":
                    self._object()
                    if off or off_marks:
                        op = b"n"  # a path in an off layer is not drawn
                paths.operate(op, [_num(o) for o in operands], st.ctm)
            elif op == b"Do" and operands and isinstance(operands[-1], PDFName):
                self._do(str(operands[-1]), resources, st, depth, off or off_marks > 0, frags, paths)
            elif op == b"BDC":
                hidden = False
                if len(operands) >= 2 and str(operands[-2]) == "OC":
                    props = operands[-1]
                    if isinstance(props, PDFName):
                        props = self._property(resources, str(props))
                    hidden = props is not None and self.oc.hidden(props)
                marks.append(hidden)
                off_marks += hidden
            elif op == b"BMC":
                marks.append(False)
            elif op == b"EMC":
                if marks:
                    off_marks -= marks.pop()
            elif op == b"BI":
                self._object()
                scanner.pos = self._skip_inline_image(data, scanner.pos)
            operands = []

    # -- text --

    def _text_operator(self, op, operands, st: _State, off: bool, frags) -> None:
        if op == b"Tj":
            if operands and isinstance(operands[-1], bytes):
                self._show([operands[-1]], st, off, frags)
        elif op in (b"'", b'"'):
            self.link_reliable = True
            st.tlm = st.tm = _matmul((1, 0, 0, 1, 0, -st.tl), st.tlm)
            if op == b'"' and len(operands) >= 3:
                st.tw = _num(operands[-3])
                st.tc = _num(operands[-2])
            if operands and isinstance(operands[-1], bytes):
                self._show([operands[-1]], st, off, frags)
        elif operands and isinstance(operands[-1], list):
            self._show([item for item in operands[-1]
                        if isinstance(item, (bytes, int, float)) and not isinstance(item, bool)],
                       st, off, frags)

    def _show(self, items, st: _State, off: bool, frags) -> None:
        """Show a string, or the strings and numbers of a TJ array, as one fragment."""
        font = st.font
        if font is None:
            # no usable Tf: read the bytes plainly rather than lose the text
            font = FontInfo(decode=default_byte_decoder, widths=WidthMap({}, 500.0), code_bytes=1)
        if font.unmapped and any(isinstance(item, bytes) and item for item in items):
            self.unmapped += 1
        if font.wmode == 1:
            self._show_vertical(items, st, font, off, frags)
            return
        table = font_table(font)
        fs, tc, tw, th, ctm = st.fs, st.tc, st.tw, st.th, st.ctm
        tm = st.tm
        origin = None  # the text matrix where the first glyph starts
        pos = 0.0      # advance since then, in text space (Th included)
        pending = 0.0  # a TJ number not yet followed by a glyph
        starts: list = []
        gaps: list = []  # the displacement before each character beyond its glyph's advance
        extra = 0.0      # Tc and Tw of the glyph before, which move the next glyph
        pieces: list = []
        whole: list = []
        reliable = self.link_reliable and font.link_metrics_reliable
        for item in items:
            if not isinstance(item, bytes):
                adjust = -_num(item) / 1000.0 * fs * th
                if origin is None:
                    tm = _matmul((1, 0, 0, 1, adjust, 0), tm)
                else:
                    pending += adjust
                continue
            if not item:
                continue
            whole.append(font.decode(item))
            for chunk, code in table.chunks(item):
                piece, w0, is_space, explicit = table.glyph(chunk, code)
                reliable = reliable and explicit
                if origin is None:
                    origin = tm
                pos += pending
                gaps.append(extra + pending)
                gaps.extend([0.0] * (len(piece) - 1))
                pending = 0.0
                extra = (tc + (tw if is_space else 0.0)) * th
                disp = (w0 * fs + tc + (tw if is_space else 0.0)) * th
                length = len(piece)
                for index in range(length):
                    starts.append(pos + disp * index / length)
                pieces.append(piece)
                pos += disp
        if origin is None:
            st.tm = tm
            return
        self.link_reliable = reliable
        st.tm = _matmul((1, 0, 0, 1, pos + pending, 0), origin)
        text = "".join(whole)
        if not text.strip():
            return
        start_tm = _matmul(origin, ctm)
        xscale, yscale = math.hypot(*start_tm[:2]), math.hypot(*start_tm[2:4])
        reliable = (reliable and xscale > 0 and yscale > 0
                    # Hancom's fixed-point matrices differ by about 5.5e-4
                    and math.isclose(xscale, yscale, rel_tol=1e-3)
                    and abs(start_tm[0] * start_tm[2] + start_tm[1] * start_tm[3])
                    <= 1e-6 * xscale * yscale)
        scale = xscale or 1.0
        space_w = font.widths.advance(font.space_code) / 1000.0 * fs * th * scale
        if space_w <= 0:
            space_w = 0.25 * fs * scale
        size = fs * (abs(start_tm[0] * start_tm[3] - start_tm[1] * start_tm[2]) ** 0.5 or 1.0)
        offsets = ()
        char_gaps = ()
        if "".join(pieces) == text:
            if len(text) <= MAX_TRACKED_RUN and len(text) <= self.positions_left:
                self.positions_left -= len(text)
                starts.append(pos)
                offsets = tuple(abs(v) * scale for v in starts)
                char_gaps = tuple(v * scale for v in gaps)
            elif not self.positions_cut:
                self.positions_cut = True
                self._drop_positions()
        frags.append(Fragment(
            x=start_tm[4], y=start_tm[5], width=abs(pos) * scale, size=size,
            text=text, space_width=space_w, order=len(frags),
            dir_x=start_tm[0], dir_y=start_tm[1], up_x=start_tm[2], up_y=start_tm[3],
            char_offsets=offsets, char_gaps=char_gaps, link_geometry_reliable=reliable,
            invisible=st.tr in (3, 7), font=st.fname, obj=max(self.text_object, 0),
            hidden="ocg" if off else ""))

    def _drop_positions(self) -> None:
        self.cut["char_positions"] += 1

    def _show_vertical(self, items, st: _State, font: FontInfo, off: bool, frags) -> None:
        """A vertical font advances along y; Th scales only the origin x."""
        table = font_table(font)
        metrics = font.vertical_metrics or VerticalMetrics(None, None, font.widths)
        fs, tc, tw, th, ctm = st.fs, st.tc, st.tw, st.th, st.ctm
        tm = st.tm
        first_metrics = None
        origin_tm = None
        pos = 0.0
        pending = 0.0
        whole: list = []
        for item in items:
            if not isinstance(item, bytes):
                adjust = -_num(item) / 1000.0 * fs
                if origin_tm is None:
                    tm = _matmul((1, 0, 0, 1, 0, adjust), tm)
                else:
                    pending += adjust
                continue
            if not item:
                continue
            whole.append(font.decode(item))
            for chunk, code in table.chunks(item):
                _piece, _w0, is_space, _explicit = table.glyph(chunk, code)
                w1y, v1x, v1y = metrics.metrics(code)
                if origin_tm is None:
                    origin_tm = tm
                    first_metrics = (v1x, v1y)
                pos += pending
                pending = 0.0
                pos += w1y / 1000.0 * fs + tc + (tw if is_space else 0.0)
        if origin_tm is None:
            st.tm = tm
            return
        st.tm = _matmul((1, 0, 0, 1, 0, pos + pending), origin_tm)
        text = "".join(whole)
        if not text.strip():
            return
        v1x, v1y = first_metrics
        origin = _matmul((1, 0, 0, 1, -v1x / 1000.0 * fs * th, -v1y / 1000.0 * fs), origin_tm)
        start_tm = _matmul(origin, ctm)
        scale = math.hypot(start_tm[2], start_tm[3]) or 1.0
        size = fs * (abs(start_tm[0] * start_tm[3] - start_tm[1] * start_tm[2]) ** 0.5 or 1.0)
        frags.append(Fragment(
            x=start_tm[4], y=start_tm[5], width=abs(pos) * scale, size=size, text=text,
            space_width=abs(metrics.metrics(32)[0]) / 1000.0 * fs * scale, order=len(frags),
            dir_x=-start_tm[2], dir_y=-start_tm[3], up_x=start_tm[0], up_y=start_tm[1],
            invisible=st.tr in (3, 7), font=st.fname, obj=max(self.text_object, 0),
            hidden="ocg" if off else "", vertical=True))

    @staticmethod
    def _skip_inline_image(data: bytes, pos: int) -> int:
        """Find the EI that ends inline image data: set off by whitespace and followed by
        whitespace, a delimiter or the end, so an EI inside the binary data is passed over."""
        n = len(data)
        while True:
            end = data.find(b"EI", pos)
            if end < 0:
                return n
            before_ok = end == 0 or data[end - 1] in WHITESPACE
            after = data[end + 2:end + 3]
            after_ok = not after or after[0] in WHITESPACE or after[0] in DELIMITERS
            if before_ok and after_ok:
                return end + 2
            pos = end + 1

    # -- XObjects --

    def _do(self, name, resources, st: _State, depth, off, frags, paths) -> None:
        pdf = self.pdf
        table = pdf.resolve(resources.get("XObject")) if isinstance(resources, dict) else None
        stream = pdf.resolve(table.get(name)) if isinstance(table, dict) else None
        if not isinstance(stream, PDFStream):
            return
        subtype = str(pdf.resolve(stream.dictionary.get("Subtype")) or "")
        if subtype == "Image":
            self._object()
            if off or self._oc_off(stream):
                return
            a, b, c, d, e, f = st.ctm
            corners = [(e, f), (a + e, b + f), (c + e, d + f), (a + c + e, b + d + f)]
            xs, ys = [p[0] for p in corners], [p[1] for p in corners]
            box = (min(xs), min(ys), max(xs), max(ys))
            if _sane(*box) and box[2] > box[0] and box[3] > box[1]:
                if self._in_clips(a, b, c, d, e, f):
                    self.image_draws.append((name, box, stream))
        elif subtype == "Form":
            self._object()
            self._form(stream, resources, st, depth, off, frags, paths)

    def _oc_off(self, stream: PDFStream) -> bool:
        oc = stream.dictionary.get("OC")
        return oc is not None and self.oc.hidden(oc)

    def _form_bytes(self, stream: PDFStream) -> Optional[bytes]:
        """A form's decoded data, once for the document. None when the document's decode
        budget cut it (the budget counts that)."""
        forms = self._forms
        known = forms["data"].get(id(stream))
        if known is not None and known[0] is stream:
            return known[1]
        data = self.pdf.decode_form_bytes(stream)
        if forms["bytes"] + len(data or b"") > MAX_FORM_CACHE_TOTAL:
            forms["data"].clear()
            forms["bytes"] = 0
        forms["data"][id(stream)] = (stream, data)
        forms["bytes"] += len(data or b"")
        return data

    def _form(self, stream: PDFStream, resources, st: _State, depth, off, frags, paths) -> None:
        pdf = self.pdf
        dictionary = stream.dictionary
        key = id(stream)
        if depth >= MAX_FORM_DEPTH or key in self.active:
            self._drop()
            return
        if self.ops >= MAX_FORM_OPERATORS:
            self._drop_ops()
            return
        if self.expanded >= MAX_FORM_EXPANDED_BYTES:
            self._drop()
            return
        data = self._form_bytes(stream)
        if data is None:
            return
        if len(data) > MAX_FORM_CACHE_BYTES or self.expanded + len(data) > MAX_FORM_EXPANDED_BYTES:
            self._drop()
            return
        matrix = pdf.resolve(dictionary.get("Matrix"))
        if not isinstance(matrix, list) or len(matrix) != 6:
            matrix = list(_IDENTITY)
        matrix = tuple(_finite(pdf.resolve(v)) for v in matrix)
        form_ctm = _matmul(matrix, st.ctm)
        if (not all(math.isfinite(v) and abs(v) <= 1e150 for v in form_ctm)
                or not all(math.isfinite(v) for v in matrix)
                or matrix[0] * matrix[3] - matrix[1] * matrix[2] == 0):
            self._drop()
            return
        form_resources = pdf.resolve(dictionary.get("Resources"))
        if not isinstance(form_resources, dict):
            form_resources = resources
        bbox = pdf.resolve(dictionary.get("BBox"))
        if isinstance(bbox, list) and len(bbox) == 4:
            bbox = tuple(_finite(pdf.resolve(v)) for v in bbox)
            if not all(math.isfinite(v) for v in bbox):
                bbox = None
        else:
            bbox = None
        form_off = off or self._oc_off(stream)
        self.expanded += len(data)
        target = [] if bbox is not None else frags
        child_paths = PathCollector()
        self.active.add(key)
        if bbox is not None:
            self.clips.append(_bbox_polygon(bbox, form_ctm))
        try:
            self.run(data, form_resources, form_ctm, st.text_state(), depth + 1, form_off,
                     target, child_paths)
        finally:
            self.active.discard(key)
            if bbox is not None:
                self.clips.pop()
                for frag in _clip_form_fragments(target, bbox, form_ctm):
                    frag.order = len(frags)
                    frags.append(frag)
            paths.add_figure(*child_paths.figure)


def interpret(pdf: PDFFile, content: bytes, resources, fonts_cache: dict, oc: OCState, *,
              box) -> PageContent:
    """Run one page's content bytes: its text fragments, rule segments, figure paths and
    image draws. `resources` is the page's /Resources dictionary, `fonts_cache` the dict
    font_infos fills and `oc` the document's layer state. `box` is the crop box in page space;
    the flags that depend on it ("offcrop", "tiny") are set when the page is assembled."""
    from ..formats import pdf as caps  # read at call time: a test patches the module
    page = _Page(pdf, fonts_cache, oc, caps.PDF_PAGE_OBJECTS)
    paths = PathCollector()
    try:
        page.run(content, resources if isinstance(resources, dict) else {}, _IDENTITY, None, 0,
                 False, page.frags, paths)
    except _Stop:
        pass
    fragments = []
    for frag in page.frags:
        if (_sane(frag.x, frag.y, frag.width, frag.size)
                and all(math.isfinite(v) for v in (frag.dir_x, frag.dir_y, frag.up_x, frag.up_y))):
            fragments.append(frag)
        else:
            page.cut["bad_coord"] += 1
    return PageContent(fragments, paths.segments, paths.short_segments, paths.figure,
                       page.image_draws, page.objects, page.cut, page.unmapped)
