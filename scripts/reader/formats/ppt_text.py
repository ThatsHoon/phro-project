"""ppt text atoms to blocks: paragraphs, bullets, line breaks, bold runs and clicked ranges.

The StyleTextPropAtom layout (paragraph runs: count u32, indent u16 and a TextPFException, its mask u32 and the
optional fields the mask names; character runs: count u32 and a TextCFException, its mask u32 and the optional fields)
follows dochan office_binary/ppt_styles.py and anydoc ppt/styletext.rs. Both kinds of run cover len(text) + 1
characters; counts past the text are clamped. A run list that cannot be read (a zero count, more than MAX_RUNS runs, a
field past the atom) leaves the text plain and counts a `record`. The runs index the raw text; control characters are
dropped from each piece afterwards, except the paragraph end, the line break and the tab.
A paragraph run whose mask has no hasBullet bit takes the bullet of its indent level from the master's
TextMasterStyleAtom of its text type ([MS-PPT] 2.9.35: cLevels u16, then per level, after a u16 level from text type 5
on, a TextPFException and a TextCFException), read by `master_levels`.
"""
import bisect
import struct

from ..model import Bold, Break, Link, ListItem, Para, Text

MAX_RUNS = 100_000
MASTER_LEVELS = 5
_CONTROL = {c: None for c in range(32) if c not in (9, 11, 13)}
# paragraph mask bit -> size of the field it adds, in the order the fields follow the mask
_PF = ((0x000F, 2), (0x0080, 2), (0x0010, 2), (0x0040, 2), (0x0020, 4), (0x0800, 2), (0x1000, 2), (0x2000, 2),
       (0x4000, 2), (0x0100, 2), (0x0400, 2), (0x8000, 2))
_PF_AFTER = ((0x10000, 2), (0x20000 | 0x40000 | 0x80000, 2), (0x200000, 2))
_CF = ((0xFFFF, 2), (0x10000, 2), (0x200000, 2), (0x400000, 2), (0x800000, 2), (0x20000, 2), (0x40000, 4),
       (0x80000, 2))


class _Bad(Exception):
    pass


def _pf(style: bytes, pos: int):
    """(bullet: True, False or None when the mask leaves it unset, position after) of a TextPFException at `pos`."""
    if pos + 4 > len(style):
        raise _Bad
    mask = struct.unpack_from("<I", style, pos)[0]
    pos += 4
    flags = 0
    for bit, size in _PF:
        if mask & bit:
            if pos + size > len(style):
                raise _Bad
            if bit == 0x000F:
                flags = struct.unpack_from("<H", style, pos)[0]
            pos += size
    if mask & 0x100000:
        if pos + 2 > len(style):
            raise _Bad
        pos += 2 + 4 * struct.unpack_from("<H", style, pos)[0]
    for bit, size in _PF_AFTER:
        if mask & bit:
            pos += size
    if pos > len(style):
        raise _Bad
    return (bool(flags & 1) if mask & 1 else None), pos


def _cf(style: bytes, pos: int):
    """(bold, position after) of a TextCFException at `pos`."""
    if pos + 4 > len(style):
        raise _Bad
    mask = struct.unpack_from("<I", style, pos)[0]
    pos += 4
    bold = False
    for bit, size in _CF:
        if mask & bit:
            if pos + size > len(style):
                raise _Bad
            if bit == 0xFFFF:
                bold = bool(mask & 1 and struct.unpack_from("<H", style, pos)[0] & 1)
            pos += size
    return bold, pos


def _paragraph_runs(style: bytes, pos: int, length: int):
    """([start offsets], [(bullet or None, indent)]) of the paragraph runs from `pos`, plus the position after them."""
    starts, props, at, runs = [], [], 0, 0
    while at < length:
        if pos + 6 > len(style):
            raise _Bad
        count, indent = struct.unpack_from("<IH", style, pos)
        runs += 1
        if count == 0 or runs > MAX_RUNS:
            raise _Bad
        bullet, pos = _pf(style, pos + 6)
        starts.append(at)
        props.append((bullet, indent))
        at += count
    return starts, props, pos


def _character_runs(style: bytes, pos: int, length: int):
    """The bold intervals [(start, end)] of the character runs from `pos`, adjacent ones merged."""
    out, at, runs = [], 0, 0
    while at < length:
        if pos + 4 > len(style):
            raise _Bad
        count = struct.unpack_from("<I", style, pos)[0]
        runs += 1
        if count == 0 or runs > MAX_RUNS:
            raise _Bad
        bold, pos = _cf(style, pos + 4)
        if bold:
            end = min(at + count, length)
            if out and out[-1][1] == at:
                out[-1] = (out[-1][0], end)
            else:
                out.append((at, end))
        at += count
    return out


def master_levels(body: bytes, instance: int) -> list:
    """[bullet: True, False or None per indent level, MASTER_LEVELS at most] of a TextMasterStyleAtom's body of
    record instance (text type) `instance`; [] when it cannot be read."""
    out = [None] * MASTER_LEVELS
    try:
        count, pos = struct.unpack_from("<H", body, 0)[0], 2
        for k in range(min(count, MASTER_LEVELS)):
            level = k
            if instance >= 5:
                level = struct.unpack_from("<H", body, pos)[0]
                pos += 2
            bullet, pos = _pf(body, pos)
            _, pos = _cf(body, pos)
            if level < MASTER_LEVELS:
                out[level] = bullet
    except (_Bad, struct.error):
        return []
    return out


def _inlines(text: str, lo: int, hi: int, bold: list) -> list:
    """The inlines of text[lo:hi]; `bold` is a sorted list of (start, end) intervals."""
    out, at = [], lo
    i = max(bisect.bisect_right(bold, (lo, 1 << 60)) - 1, 0)
    while at < hi:
        while i < len(bold) and bold[i][1] <= at:
            i += 1
        if i < len(bold) and bold[i][0] <= at:
            stop, is_bold = min(bold[i][1], hi), True
        else:
            stop, is_bold = (min(bold[i][0], hi) if i < len(bold) else hi), False
        run = []
        for k, piece in enumerate(text[at:stop].split("\x0b")):
            if k:
                run.append(Break())
            piece = piece.translate(_CONTROL)
            if piece:
                run.append(Text(piece))
        if run:
            out.append(Bold(run) if is_bold else run)
        at = stop
    flat = []
    for item in out:
        flat.extend(item if isinstance(item, list) else [item])
    return flat


def _clicked(text: str, lo: int, hi: int, bold: list, links) -> list:
    """The inlines of text[lo:hi] with each clicked range (begin, end, url, jump) of `links` inside its Link and its
    jump after it, `(→ next slide)`."""
    cuts = sorted({lo, hi} | {p for b, e, _, _ in links for p in (b, e) if lo < p < hi})
    out = []
    for a, b in zip(cuts, cuts[1:]):
        inl = _inlines(text, a, b, bold)
        hit = next((k for k in links if k[0] <= a and b <= k[1]), None)
        if not inl or hit is None:
            out.extend(inl)
            continue
        if hit[2]:
            out.append(Link(hit[2], inl))
        else:
            out.extend(inl)
        if hit[3] and b == min(hit[1], hi):
            out.append(Text(f" (→ {hit[3]})"))
    return out


def blocks(text: str, style: bytes | None, ctx=None, levels=(), links=()) -> list:
    """Paragraphs and list items of one text atom: `levels` are the master's bullets of its text type by indent level,
    `links` its clicked ranges as (begin, end, url, jump). The layout of the style runs does not depend on the atom's
    TextHeaderAtom type."""
    starts, props, bold = [0], [(None, 0)], []
    if style is not None:
        try:
            length = len(text) + 1
            starts, props, pos = _paragraph_runs(style, 0, length)
            bold = _character_runs(style, pos, length)
        except (_Bad, struct.error):
            starts, props, bold = [0], [(None, 0)], []
            if ctx is not None:
                ctx.skipped["record"] += 1
    links = [k for k in links if k[2] or k[3]]
    out, at = [], 0
    for para in text.split("\r"):
        end = at + len(para)
        inl = _clicked(text, at, end, bold, links) if links else _inlines(text, at, end, bold)
        if inl:
            bullet, indent = props[max(bisect.bisect_right(starts, at) - 1, 0)]
            if bullet is None:
                bullet = bool(levels[indent]) if indent < len(levels) else False
            out.append(ListItem(min(indent, 8), "-", inl) if bullet else Para(inl))
        at = end + 1
    return out
