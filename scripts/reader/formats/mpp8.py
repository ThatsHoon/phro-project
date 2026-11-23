# Ported from MPXJ (https://www.mpxj.org), copyright (c) Packwood Software, under the GNU Lesser General Public
# License 2.1 or later. This module is distributed under the same license: see https://www.gnu.org/licenses/lgpl-2.1.
"""Microsoft Project 98 (MPP8): a port of MPXJ's MPP8Reader with Props8, FixFix, FixDeferFix and ExtendedData,
reading into mpp_read's Project. Under `   1`: the project Props, then the resources, tasks, relations and
assignments, each a FixFix of fixed-size items whose strings and extended data sit in the FixDeferFix beside it.

Every extended data value MPXJ reads is read here too, the custom fields included, so a value whose bytes do not fit
fails the file as it fails MPXJ (mpp_read turns it into `damaged`). Calendars, views, tables, recurring task data and
hyperlinks are not read.
"""
from datetime import datetime

from . import mpp_blocks as B
from .mpp_read import (END_NA, RELATION_TYPES, START_NA, Assignment, Project, Resource, get_byte, get_int,
                       get_long, get_short, max_units, properties)

DIR = "   1/"
TASK_SIZE, TASK_LONG = 316, 366
RESOURCE_SIZE, ASSIGNMENT_SIZE, ASSIGNMENT_LONG, RELATION_SIZE = 196, 204, 238, 36
TASK_WBS, TASK_NOTES, TASK_HYPERLINK = 104, 247, 236
RESOURCE_NOTES = 169
_TASK_READS = {"unicode": (105, *range(106, 116), *range(184, 204)), "int": tuple(range(147, 167, 2)),
               "short": tuple(range(148, 167, 2)), "long": tuple(range(237, 247)),
               "timestamp": (*range(116, 136), *range(174, 184)), "double": (*range(137, 147), *range(204, 214))}
_RESOURCE_READS = {"unicode": tuple(range(61, 94)), "int": tuple(range(134, 144)), "short": tuple(range(144, 154)),
                   "long": tuple(range(170, 180)), "timestamp": (*range(94, 114), *range(157, 167)),
                   "double": tuple(range(114, 134))}


def props8(data: bytes) -> dict:
    """Props8: {key: bytes} after a 16-byte header (u16 count at 12); each item an i32 size, four bytes whose i32 is
    its key, an i32 length and its bytes (padded to two), sized by the first when the key's third byte is 64 and 4 when
    the length is 65536. A size below 1 ends the items; IndexError when the bytes run out."""
    pos, out = 16, {}
    for _ in range(get_short(data, 12)):
        a1, key, a3, a5 = get_int(data, pos), get_int(data, pos + 4), get_byte(data, pos + 6), get_int(data, pos + 8)
        pos += 12
        size = 4 if a5 == 65536 else a1 if a3 == 64 else a5
        if size <= 0:
            break
        if pos + size > len(data):
            raise IndexError(pos)
        out[key] = data[pos:pos + size]
        pos += size + (size & 1)
    return out


def fixfix(data: bytes, size: int) -> list:
    """FixFix: the stream's whole items of `size` bytes."""
    return [data[k:k + size] for k in range(0, len(data) - size + 1, size)]


def _copy(src: bytes, at: int, n: int, room: int) -> bytes:
    """System.arraycopy of `n` bytes from `at` into a buffer with `room` bytes left."""
    if at < 0 or n < 0 or at + n > len(src) or n > room:
        raise IndexError(at)
    return src[at:at + n]


class FixDeferFix:
    """FixDeferFix: items at an offset, each an i32 next block offset (-1: none), an i32 size and 28 bytes, then
    blocks of an i32 next offset and 32 bytes."""

    def __init__(self, data: bytes):
        self.data = data

    def bytes(self, off: int):
        d = self.data
        if not 0 < off < len(d):
            return None
        nxt, size = get_int(d, off), get_int(d, off + 4)
        off += 8
        if not 0 < size < len(d):
            return None
        block = 28
        if nxt == -1 and size > block:
            return None
        out, left = b"", size
        while nxt != -1:
            out += _copy(d, off, block, size - len(out))
            off += block
            left -= block
            if off != nxt:
                off = nxt
            nxt = get_int(d, off)
            off += 4
            block = 32
        return out + _copy(d, off, left, size - len(out))

    def unicode(self, off: int):
        b = self.bytes(off)
        return None if b is None else B.unicode(b, 0)

    def string(self, off: int):
        b = self.bytes(off)
        return None if b is None else b.decode("latin-1")


def _offset(data: bytes, at: int) -> int:
    return -1 - get_int(data, at)


class Extended:
    """ExtendedData: {type: bytes} from the FixDeferFix item at an offset, each an i32 size, an i32 type and its
    bytes; the getters read MPXJ's way, failing on an item too short for its value."""

    def __init__(self, var: FixDeferFix, off: int):
        self.var, self.items = var, {}
        data = var.bytes(off)
        at = 0
        while data is not None and at < len(data):
            size, kind = get_int(data, at), get_int(data, at + 4)
            at += 8
            if size < 0 or at + size > len(data):
                raise IndexError(at)
            self.items[kind] = data[at:at + size]
            at += size

    def get(self, how: str, kind: int):
        item = self.items.get(kind)
        if item is None:
            return None
        if how == "unicode":
            return self.var.unicode(_offset(item, 0))
        if how == "string":
            return self.var.string(_offset(item, 0))
        if how == "short":
            return get_short(item, 0)
        if how == "int":
            return get_int(item, 0)
        if how == "long":
            return int.from_bytes(_copy(item, 0, 6, 6), "little")
        if how == "double":
            return get_long(item, 0)
        get_short(item, 2)
        return B.timestamp(item, 0)

    def read_all(self, reads: dict):
        for how, kinds in reads.items():
            for kind in kinds:
                self.get(how, kind)


def read(cf) -> Project:
    """The Project of an MPP8 compound file."""
    p = Project()
    properties(p, props8(cf.read(DIR + "Props")), cf)
    resources(cf, p)
    tasks(cf, p)
    relations(cf, p)
    assignments(cf, p)
    return p


def _var(cf, d: str) -> FixDeferFix:
    return FixDeferFix(cf.read(DIR + d + "FixDeferFix   0"))


def resources(cf, p: Project):
    d = "TBkndRsc/"
    var, now = None, datetime.now()
    for data in fixfix(cf.read(DIR + d + "FixFix   0"), RESOURCE_SIZE):
        uid = get_int(data, 0)
        if uid < 1 or data[8] & 0x01 or get_short(data, 164) != 0:
            continue
        var = var or _var(cf, d)
        ext = Extended(var, _offset(data, 192))
        ext.read_all(_RESOURCE_READS)
        r = Resource()
        p.resources.append(r)
        r.f["id"] = get_int(data, 4)
        var.unicode(_offset(data, 160))
        r.f["name"] = var.unicode(_offset(data, 156))
        p.set_uid(r, uid, p.resource_uids)
        ext.get("string", RESOURCE_NOTES)
        r.f["available_from"] = B.timestamp(data, 28) or START_NA
        r.f["available_to"] = B.timestamp(data, 32) or END_NA
        r.f["max_units"] = get_int(data, 52) / 100
        r.max_units = max_units(r, now)


def tasks(cf, p: Project):
    d = "TBkndTask/"
    raw = cf.read(DIR + d + "FixFix   0")
    items = fixfix(raw, TASK_SIZE if len(raw) % TASK_SIZE == 0 else TASK_LONG)
    var, auto_wbs = None, True
    for data in items:
        uid = get_int(data, 0)
        if uid < 1:
            continue
        deleted = get_short(data, 272)
        if not deleted & 0xC0 and deleted & 0x03 and deleted not in (0x0031, 0x203D):
            continue
        var = var or _var(cf, d)
        if data[8] & 0x01:
            continue
        ext = Extended(var, _offset(data, 312))
        tid = get_int(data, 4)
        t = p.add_task()
        f = t.f
        f["actual_finish"] = B.timestamp(data, 108)
        f["actual_start"] = B.timestamp(data, 104)
        ext.read_all(_TASK_READS)
        f["created"] = B.timestamp(data, 138)
        f["duration"] = B.adjusted(p, get_int(data, 68), B.duration_units(get_short(data, 72), p.duration_units))
        f["finish"] = B.timestamp(data, 20)
        hyperlink = ext.get("int", TASK_HYPERLINK)
        var.bytes(-1 - (hyperlink or 0))
        p.set_id(t, tid)
        f["milestone"] = bool(data[12] & 0x01)
        f["name"] = var.unicode(_offset(data, 264))
        f["outline_level"] = get_short(data, 48)
        f["percent"] = B.percentage(data, 130)
        f["start"] = B.timestamp(data, 96)
        p.set_uid(t, uid)
        f["wbs"] = ext.get("unicode", TASK_WBS)
        f["notes"] = _notes(data, ext, var)
        if f["wbs"] is not None:
            auto_wbs = False
    p.auto_wbs = auto_wbs


def _notes(data: bytes, ext: Extended, var: FixDeferFix):
    """setTaskNotes: the notes in the extended data; else, in a long item, the text at an offset in the item its
    last field points to when that text holds a "{"."""
    notes = ext.get("string", TASK_NOTES)
    if notes is None and len(data) == TASK_LONG:
        at = var.bytes(_offset(data, 362))
        if at is not None and len(at) >= 12:
            notes = var.string(_offset(at, 8))
            if notes is not None and "{" not in notes:
                notes = None
    return notes


def relations(cf, p: Project):
    name = DIR + "TBkndCons/FixFix   0"
    if not cf.exists(name):
        return
    for data in fixfix(cf.read(name), RELATION_SIZE):
        if get_int(data, 28) != 0:
            continue
        a, b = get_int(data, 12), get_int(data, 16)
        if a == b:
            continue
        pred, succ = p.uids.get(a), p.uids.get(b)
        if pred is None or succ is None:
            continue
        kind = get_short(data, 20)
        lag = B.duration(get_int(data, 24), B.duration_units(get_short(data, 22)))
        p.add_predecessor(succ, pred, RELATION_TYPES[kind] if kind < 4 else "FINISH_START", lag)


def assignments(cf, p: Project):
    d = "TBkndAssn/"
    raw = cf.read(DIR + d + "FixFix   0")
    items = fixfix(raw, ASSIGNMENT_SIZE)
    if len(raw) % ASSIGNMENT_SIZE or (len(raw) % ASSIGNMENT_LONG == 0 and not _found(p, items)):
        items = fixfix(raw, ASSIGNMENT_LONG)
    if items:
        _var(cf, d)
    for data in items:
        if data[168] == 0x02:
            continue
        t, r = p.uids.get(get_int(data, 16)), p.resource_uids.get(get_int(data, 20))
        if t is None or r is None:
            continue
        have = p.assigned.setdefault(t.uid, [])
        a = next((x for x in have if x.resource_uid == r.uid), None)
        if a is None:
            a = Assignment(r.uid)
            have.append(a)
        a.units = get_short(data, 80) / 100


def _found(p: Project, items: list) -> bool:
    """testAssignmentTasks: no item names neither a known task nor a known resource."""
    return not any(p.uids.get(get_int(x, 16)) is None and p.resource_uids.get(get_int(x, 20)) is None for x in items)
